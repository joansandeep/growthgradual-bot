"""
POST /api/chat/report/pdf  — Generate branded PDF (ReportLab, pure Python)
Body: { report, title, charts, question, keyStats, summary }

Report structure rendered:
  Page 1  — Title Page (cover)
  Page 2  — Introduction
  Page 3  — Data Sources & Executive Summary
  Page 4+ — Data Analysis  (charts injected at [CHART_n] placeholders)
  ...     — Key Findings
  ...     — Conclusion
  ...     — References
"""
import base64
import io
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response

from utils.datawrapper import attach_datawrapper_charts, fetch_png
import asyncio
import httpx

router = APIRouter()
log = logging.getLogger("pdf")

# ─── Colour palette ───────────────────────────────────────────────────────────
# Matches the Growth Gradual house style seen in the sample "Market Currents"
# report: deep navy + gold as the primary brand pair, with a restrained set of
# muted, editorial accent colours (teal, slate blue, olive, burgundy) rather
# than bright/neon web-app colours — those clash with the cream-paper, navy-
# cover aesthetic the sample uses throughout.
NAVY    = (26/255,  31/255,  78/255)
GOLD    = (200/255, 134/255, 10/255)
GREEN   = (22/255,  128/255, 88/255)    # muted teal-green — positive deltas, best sector
RED     = (185/255, 60/255,  55/255)    # muted brick-red — negative deltas, worst sector
TEAL    = (33/255,  118/255, 122/255)   # secondary chart colour — cool teal
SLATE   = (74/255,  92/255,  138/255)   # secondary chart colour — muted slate blue
OLIVE   = (128/255, 110/255, 40/255)    # tertiary chart colour — muted olive/bronze
BURGUNDY = (110/255, 47/255, 58/255)    # tertiary chart colour — muted burgundy
AMBER   = (194/255, 140/255, 40/255)    # close to GOLD, used for chart series variety
# Kept as aliases so any pre-existing references elsewhere in this file still resolve.
BLUE    = SLATE
PURPLE  = BURGUNDY
CYAN    = TEAL
PINK    = BURGUNDY
WHITE   = (1.0, 1.0, 1.0)
LIGHT   = (240/255, 243/255, 255/255)
GREY    = (139/255, 147/255, 181/255)
BODY_TXT = (0.18, 0.21, 0.38)

# Order chosen so the first 2-3 series (the common case) land on colours that
# read cleanly against cream paper and don't fight the navy/gold brand pair.
CHART_COLORS = [NAVY, GOLD, TEAL, RED, SLATE, OLIVE, BURGUNDY, GREEN]

# Section accent colours (one per major section heading) — same restrained set.
SECTION_ACCENTS = [GOLD, TEAL, GREEN, OLIVE, RED, SLATE]


def fmt_inr(value_str: str) -> str:
    """Format a numeric string into Indian number format with Rs. prefix.
    E.g. '1224826.38' -> '12,24,826.38' (crore label added by caller).
    Leaves non-numeric strings untouched.
    """
    if not value_str:
        return value_str
    s = str(value_str).strip()
    # Strip known currency prefixes/symbols and thousands separators — but do
    # NOT touch the decimal point in the number itself. The previous version
    # used a single character-class regex `[Rs.₹$€£,\s]` which strips ANY "."
    # anywhere in the string, so "1,000.80" lost its decimal point entirely
    # and became "100080" — a 100x magnitude error (e.g. CMP ₹1,000.80
    # rendered as Rs. 1,00,080). Strip whole currency tokens first instead.
    cleaned = s
    for token in ("Rs.", "Rs", "₹", "$", "€", "£", "Cr", "cr"):
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.replace(",", "").replace(" ", "").strip()
    try:
        num = float(cleaned)
    except (ValueError, TypeError):
        return s  # not a number — return as-is
    is_negative = num < 0
    num = abs(num)
    # Preserve decimal precision whenever the source value actually had a
    # fractional part — dropping it isn't a style choice, it's data loss.
    # This matters most for sub-$10 figures (e.g. stock prices like
    # "1.592667" or "2.364667") where the bare integer part ("1", "2")
    # discards the entire meaningful number, not just its formatting.
    had_decimal = "." in cleaned
    num_rounded = round(num, 2) if had_decimal else num
    int_part = int(num_rounded)
    frac_cents = round((num_rounded - int_part) * 100)
    if frac_cents >= 100:  # rounding carried over (e.g. 4.999 -> 5.00)
        int_part += 1
        frac_cents = 0
    # Format integer part in Indian style
    s_int = str(int_part)
    if len(s_int) <= 3:
        formatted = s_int
    else:
        last3 = s_int[-3:]
        rest = s_int[:-3]
        groups = []
        while len(rest) > 2:
            groups.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.append(rest)
        formatted = ",".join(reversed(groups)) + "," + last3
    if had_decimal:
        formatted += f".{frac_cents:02d}"
    if is_negative:
        formatted = "-" + formatted
    return formatted


def _coerce_value(v) -> float:
    """Safely coerce a chart data value to float.
    Handles ints, floats, and strings like '-3%', '1,25,957', '₹72119', '$1820.5'.
    Returns 0.0 on anything unparseable.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        # Strip currency symbols, commas, spaces, percent signs
        cleaned = re.sub(r"[₹$€£,\s]", "", v).replace("%", "").strip()
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


# ─── Chart data validator ─────────────────────────────────────────────────────
def _is_valid_chart(spec: dict) -> bool:
    """Return False for charts with no data, all-zero, or all-identical values."""
    if spec.get("type") == "table":
        cols = spec.get("columns") or []
        rows = spec.get("rows") or []
        return len(cols) >= 2 and len(rows) >= 1

    series = spec.get("series") or []
    if not series:
        return False
    chart_type = spec.get("type", "bar")

    # For multi-series (comparison charts): validate each series individually.
    # Single-point series are allowed (e.g. a one-bar snapshot or a single
    # comparison value per company) — line charts still need at least 2
    # points per series since a single point can't be plotted as a line.
    for s in series:
        pts = s.get("data") or []
        min_pts = 2 if chart_type == "line" else 1
        if len(pts) < min_pts:
            return False

    # All values across all series
    all_data = [d for s in series for d in (s.get("data") or [])]
    values   = [_coerce_value(d.get("value", 0)) for d in all_data]
    if not values:
        return False
    # A single overall data point trivially has one "unique" value — only
    # reject for flat/identical values when there's more than one point.
    if len(values) > 1 and len(set(values)) <= 1:
        return False
    if max(abs(v) for v in values) == 0:
        return False

    # For single-series: labels must be unique within the series
    # For multi-series: labels within EACH series must be unique
    for s in series:
        labels = [d.get("label", "") for d in (s.get("data") or [])]
        if len(set(labels)) < len(labels):
            return False

    return True



# ─── Logo loader ──────────────────────────────────────────────────────────────
def _load_logo_bytes(logo_b64: str = "") -> bytes | None:
    """Load logo from: (1) request-supplied base64, (2) LOGO_B64 env var, (3) filesystem."""
    # 1. Logo passed directly in the PDF request body (most reliable)
    if logo_b64:
        try:
            return base64.b64decode(logo_b64)
        except Exception as e:
            log.debug("Logo: request-supplied base64 failed to decode (%s)", e)

    # 2. Environment variable (set once at deploy time)
    b64 = os.environ.get("LOGO_B64", "")
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception as e:
            log.debug("Logo: LOGO_B64 env var failed to decode (%s)", e)

    # 2b. Explicit path override via env var (most reliable for non-monorepo deploys)
    logo_path_env = os.environ.get("LOGO_PATH", "")
    if logo_path_env:
        p = Path(logo_path_env)
        if p.exists():
            try:
                return p.read_bytes()
            except Exception as e:
                log.debug("Logo: LOGO_PATH file failed to read (%s)", e)

    # 3. Filesystem search — covers local dev and various deploy layouts
    _here = Path(__file__).resolve().parent          # routes/
    _backend = _here.parent                          # backend/
    _project = _backend.parent                       # project root
    _frontend_pub = _project / "frontend" / "public"

    search_dirs = [
        _frontend_pub,
        _project / "public",
        _backend / "public",
        _backend / "static",
        _backend / "assets",
        _backend,
        _backend.parent / "frontend" / "public",
        _project,
        _project.parent / "frontend" / "public",
        Path(os.getcwd()),
        Path(os.getcwd()) / "public",
        Path(os.getcwd()) / "static",
        Path(os.getcwd()) / "frontend" / "public",
        Path(os.getcwd()).parent / "frontend" / "public",
        Path("/app"),
        Path("/app/public"),
        Path("/app/static"),
        Path("/app/frontend/public"),
        Path("/opt/render/project/src/frontend/public"),  # Render.com
        Path("/opt/render/project/src/backend/public"),
    ]
    logo_names = [
        "growth-gradual-logo.png",
        "growth-gradual-logo.jpg",
        "growth-gradual-logo.jpeg",
        "growth-gradual-logo-transparent.jpeg",
        "logo.png",
        "logo.jpg",
        "logo.jpeg",
    ]
    for d in search_dirs:
        for name in logo_names:
            p = d / name
            if p.exists():
                return p.read_bytes()
    return None

def _draw_logo(c, logo_bytes, x, y, width, height):
    """Draw logo, preserving alpha transparency for PNGs with transparent backgrounds."""
    from reportlab.lib.utils import ImageReader
    if not logo_bytes:
        return False
    try:
        img_reader = ImageReader(io.BytesIO(logo_bytes))
        pil_img = getattr(img_reader, "_image", None)
        has_alpha = pil_img is not None and pil_img.mode in ("RGBA", "LA", "PA")
        mask = "auto" if has_alpha else None
        c.drawImage(img_reader, x, y, width=width, height=height,
                    mask=mask, preserveAspectRatio=True)
        return True
    except Exception:
        return False


def _flatten_to_rgb(pil_img):
    """Safely convert any PIL image mode to opaque RGB for JPEG encoding.

    Two distinct bugs this fixes:
    1. Images with an alpha channel (RGBA/LA, or palette images carrying a
       "transparency" entry) raise `OSError: cannot write mode RGBA as JPEG`
       if saved as-is — JPEG has no alpha channel. Any uploaded screenshot
       or web-sourced PNG with transparency would silently fail to render
       (the caller's try/except swallows it) — the image placeholder would
       just vanish from the PDF with nothing in its place.
    2. Naively calling `.convert("RGB")` on an RGBA image *drops* the alpha
       channel without compositing — whatever RGB values were stored under
       the transparent pixels (commonly black) show through as solid black
       regions where the image should have been transparent/white.

    Compositing onto a white background first avoids both: the image
    always saves cleanly as JPEG, and transparent areas render as white
    instead of black.
    """
    from PIL import Image as _PILImage
    if pil_img.mode in ("RGBA", "LA") or (pil_img.mode == "P" and "transparency" in pil_img.info):
        rgba = pil_img.convert("RGBA")
        bg = _PILImage.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if pil_img.mode != "RGB":
        return pil_img.convert("RGB")
    return pil_img


def detect_domain(question: str) -> str:
    q = question.lower()
    if re.search(r"\b(stock|share|market|nse|bse|sensex|nifty|sebi|ipo|equity|mutual fund|etf|trading|portfolio|invest)\b", q):
        return "Markets & Equities"
    if re.search(r"\b(crypto|bitcoin|ethereum|blockchain|defi|web3|token)\b", q):
        return "Crypto & Blockchain"
    if re.search(r"\b(macro|gdp|inflation|rbi|fed|interest rate|cpi|wpi|economy|fiscal|monetary)\b", q):
        return "Macroeconomics"
    if re.search(r"\b(ai|artificial intelligence|machine learning|llm|gpt|neural|model|deep learning)\b", q):
        return "Artificial Intelligence"
    if re.search(r"\b(tech|technology|software|startup|saas|cloud|semiconductor|chip)\b", q):
        return "Technology"
    if re.search(r"\b(health|pharma|drug|clinical|fda|medical|biotech|cancer|vaccine|disease)\b", q):
        return "Healthcare & Pharma"
    if re.search(r"\b(sport|cricket|ipl|football|tennis|nba|fifa|athlete|league|match|tournament)\b", q):
        return "Sports"
    if re.search(r"\b(climate|energy|solar|wind|renewable|oil|gas|carbon|esg|environment)\b", q):
        return "Energy & Climate"
    if re.search(r"\b(real estate|property|realty|reit|land|housing|construction)\b", q):
        return "Real Estate"
    # Generic fallback — deliberately NOT "Research Intelligence": the cover
    # page builds its overline as f"{domain.upper()} INTELLIGENCE", so a
    # domain that already ends in "Intelligence" doubles up into the
    # "RESEARCH INTELLIGENCE INTELLIGENCE" label bug.
    return "Research"


# ─── Safe text — strip chars Helvetica can't render ──────────────────────────
def _safe_text(text: str) -> str:
    """Replace characters that Helvetica can't render (shows as ■ tofu).
    En/em dashes are NOT replaced — reportlab's base14 Helvetica renders them
    fine via WinAnsiEncoding, and replacing \u2014 with a literal "--" was
    producing an ugly double-hyphen artifact in generated titles/headings."""
    return (text
        .replace("₹", "Rs.")
        .replace("\u20b9", "Rs.")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2026", "...")
    )


# ─── Inline markdown stripping ────────────────────────────────────────────────
def _strip_inline(text: str) -> str:
    # Remove bold/italic markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove any stray leading/trailing asterisks
    text = re.sub(r"^\*+\s*", "", text)
    text = re.sub(r"\s*\*+$", "", text)
    return _safe_text(text.strip())

def _parse_inline_bold(text: str) -> list:
    """Split text into [(is_bold, segment), ...] for inline bold rendering."""
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    result = []
    for p in parts:
        if p.startswith("**") and p.endswith("**") and len(p) > 4:
            result.append((True, _safe_text(p[2:-2])))
        elif p:
            result.append((False, _safe_text(p)))
    return result


def _draw_rich_line(c, x, y, text, regular_font, bold_font, size, color, max_w, justify=False):
    """Draw a line with inline **bold** support. Returns width drawn.

    justify=True distributes the line's slack evenly between word gaps so
    its right edge lands on x+max_w — standard justified-paragraph
    behaviour. Callers should only pass justify=True for lines that aren't
    the last line of their paragraph/bullet/numbered item: the final line
    of a justified paragraph conventionally stays left-aligned/ragged
    rather than being stretched, since a short last line stretched to full
    width reads as a spacing bug, not "justified" text.
    """
    segments = _parse_inline_bold(text)
    # Flatten segments into a per-word list (still tagged bold/not-bold) so
    # justification spacing can be computed across bold/regular boundaries —
    # a bold run is still just words for spacing purposes.
    words: list[tuple[bool, str]] = []
    for is_bold, seg in segments:
        for w in seg.split(" "):
            if w:
                words.append((is_bold, w))

    if not words:
        return 0

    space_w = c.stringWidth(" ", regular_font, size)

    if justify and len(words) > 1:
        natural_w = sum(
            c.stringWidth(w, bold_font if is_bold else regular_font, size)
            for is_bold, w in words
        ) + space_w * (len(words) - 1)
        extra = max_w - natural_w
        gap = space_w + (extra / (len(words) - 1)) if extra > 0 else space_w
    else:
        gap = space_w

    cx = x
    for idx, (is_bold, w) in enumerate(words):
        font = bold_font if is_bold else regular_font
        c.setFont(font, size)
        c.setFillColorRGB(*color)
        c.drawString(cx, y, w)
        cx += c.stringWidth(w, font, size)
        if idx < len(words) - 1:
            cx += gap
    return cx - x


# ─── Markdown tokeniser ───────────────────────────────────────────────────────
def _tokenise(md: str):
    """Yield token dicts from markdown text."""
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        if re.match(r"^\[CHART_\d+\]", stripped):
            m = re.match(r"^\[CHART_(\d+)\]", stripped)
            yield {"type": "chart_placeholder", "index": int(m.group(1)) - 1}

        elif re.match(r"^\[FILE_IMG_\d+\]", stripped) or re.match(r"^\[PAGE_IMG_\d+\]", stripped):
            m = re.match(r"^\[FILE_IMG_(\d+)\]", stripped) or re.match(r"^\[PAGE_IMG_(\d+)\]", stripped)
            yield {"type": "file_img_placeholder", "index": int(m.group(1)) - 1}

        elif re.match(r"^\[WEB_IMG_\d+\]", stripped):
            m = re.match(r"^\[WEB_IMG_(\d+)\]", stripped)
            yield {"type": "web_img_placeholder", "index": int(m.group(1)) - 1}

        elif stripped.startswith("#### "):
            yield {"type": "h4", "text": stripped[5:].strip()}
        elif stripped.startswith("### "):
            yield {"type": "h3", "text": stripped[4:].strip()}
        elif stripped.startswith("## "):
            yield {"type": "h2", "text": stripped[3:].strip()}
        elif stripped.startswith("# "):
            yield {"type": "h1", "text": stripped[2:].strip()}

        elif stripped == "---":
            yield {"type": "hr"}

        elif stripped.startswith("| "):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                row_line = lines[i]
                if re.match(r"^\|[\s|:-]+\|$", row_line):
                    i += 1
                    continue
                cells = [c.strip() for c in row_line.strip("|").split("|")]
                rows.append(cells)
                i += 1
            if rows:
                # Filter out data rows where ALL non-first cells are empty/placeholder
                _EMPTY_CELL = {
                    "", "-", "—", "n/a", "na",
                    "not available", "data not available",
                    "not extractable", "data not extractable",
                    "no data", "tbd", "pending", "unknown",
                }
                header = rows[0] if rows else []
                filtered = [header] if header else []
                for row in rows[1:]:
                    data_cells = row[1:] if len(row) > 1 else row
                    if any(cell.strip().lower() not in _EMPTY_CELL for cell in data_cells):
                        filtered.append(row)
                # Only yield if header + at least 1 real data row
                if len(filtered) >= 2:
                    yield {"type": "table", "rows": filtered}
            continue

        elif re.match(r"^\s*[-*+]\s+", stripped):
            yield {"type": "bullet", "text": re.sub(r"^\s*[-*+]\s+", "", stripped)}

        elif re.match(r"^\d+\.\s+", stripped):
            m = re.match(r"^(\d+)\.\s+(.*)", stripped)
            yield {"type": "numbered", "num": m.group(1), "text": m.group(2)}

        elif stripped:
            # Split any inline [CHART_n], [WEB_IMG_n], [FILE_IMG_n] placeholders
            # (also accepts legacy [PAGE_IMG_n] for safety) that the LLM
            # embedded mid-sentence rather than on their own line. We tokenise
            # the paragraph by splitting on placeholder patterns and yielding
            # each part separately so the renderer handles them correctly.
            _INLINE_PH = re.compile(
                r"(\[CHART_\d+\]|\[WEB_IMG_\d+\]|\[FILE_IMG_\d+\]|\[PAGE_IMG_\d+\])"
            )
            parts = _INLINE_PH.split(stripped)
            if len(parts) == 1:
                # No placeholders — plain paragraph
                yield {"type": "para", "text": stripped}
            else:
                for part in parts:
                    if not part:
                        continue
                    cm = re.match(r"^\[CHART_(\d+)\]$", part)
                    wm = re.match(r"^\[WEB_IMG_(\d+)\]$", part)
                    pm = re.match(r"^\[FILE_IMG_(\d+)\]$", part) or re.match(r"^\[PAGE_IMG_(\d+)\]$", part)
                    if cm:
                        yield {"type": "chart_placeholder", "index": int(cm.group(1)) - 1}
                    elif wm:
                        yield {"type": "web_img_placeholder", "index": int(wm.group(1)) - 1}
                    elif pm:
                        yield {"type": "file_img_placeholder", "index": int(pm.group(1)) - 1}
                    else:
                        t = part.strip()
                        if t:
                            yield {"type": "para", "text": t}

        i += 1


# ─── Text word-wrap helper ────────────────────────────────────────────────────
def _wrap(canvas, text: str, font: str, size: float, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if canvas.stringWidth(test, font, size) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _fit_cell(canvas, text: str, font: str, size: float, max_width: float) -> str:
    """Truncate to the actual rendered pixel width (not a fixed char count),
    so long cell values never overflow into the next column. Adds an
    ellipsis when truncated; returns the original text untouched if it
    already fits."""
    if canvas.stringWidth(text, font, size) <= max_width:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if canvas.stringWidth(text[:mid] + ell, font, size) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell if lo > 0 else ell


_SIGNED_CELL_RE = re.compile(r"^[^\d+\-]*([+\-])\s*[₹$]?\s*[\d,.]+")
_PAREN_NEG_RE   = re.compile(r"^[^\d(]*\(\s*[\d,.]")


def _signed_cell_color(cell_str: str):
    """Return GREEN/RED for a table cell that is a signed gain/loss figure
    (e.g. '+6.4%', '-9.56%', '(1,234)' as accounting-style negative), or
    None for anything else (names, dates, unsigned levels) so those keep
    the row's default text color."""
    s = cell_str.strip()
    if not s:
        return None
    m = _SIGNED_CELL_RE.match(s)
    if m:
        return GREEN if m.group(1) == "+" else RED
    if _PAREN_NEG_RE.match(s):
        return RED
    return None


# ─── Chart renderers ──────────────────────────────────────────────────────────
def _axis_titles(c, spec, x0, y0, PL, PB, pw, ph):
    """Draw the axis TITLE text (e.g. 'Return (%)' / 'Sector') distinct from the
    numeric/category tick labels the bar/line renderers already draw. Falls back
    to the unit string for the Y title when the LLM didn't supply an explicit
    yLabel, so a chart is never left with unlabeled axes."""
    x_label = _safe_text((spec.get("xLabel") or "").strip())
    y_label = _safe_text((spec.get("yLabel") or "").strip())
    unit = _safe_text(spec.get("unit", ""))
    if not y_label and unit:
        y_label = unit
    if x_label:
        c.setFillColorRGB(*GREY); c.setFont("Helvetica-Oblique", 6.5)
        c.drawCentredString(x0 + PL + pw / 2, y0 + 2, x_label[:40])
    if y_label:
        c.saveState()
        c.translate(x0 + 10, y0 + PB + ph / 2)
        c.rotate(90)
        c.setFillColorRGB(*GREY); c.setFont("Helvetica-Oblique", 6.5)
        c.drawCentredString(0, 0, y_label[:30])
        c.restoreState()


def _bar(c, spec, x0, y0, w, h):
    series_list = spec.get("series") or [{}]
    data = series_list[0].get("data") or []
    if not data:
        return
    unit  = spec.get("unit", "")
    n_ser = len(series_list)
    # Multi-series composition/breakdown charts (e.g. "Portfolio Allocation by
    # Fund" — Equity/Debt/Cash per fund) are meant to render as a STACKED
    # column, not a grouped one — the parts sum to a whole per label. The
    # model sets spec["stacked"]=true for these; anything else (comparisons
    # across entities) stays grouped side-by-side as before.
    stacked = bool(spec.get("stacked")) and n_ser > 1

    if stacked:
        # Y scale is the tallest CUMULATIVE stack across labels, not the
        # tallest individual value.
        n = max(len(data), 1)
        totals = []
        for i in range(n):
            tot = sum(abs(_coerce_value((s.get("data") or [{}])[i].get("value", 0)))
                       for s in series_list if i < len(s.get("data") or []))
            totals.append(tot)
        max_v = max(totals) if totals else 1
    else:
        # All values across all series for unified Y scale
        all_vals = [abs(_coerce_value(d.get("value", 0))) for s in series_list for d in s.get("data", [])]
        max_v    = max(all_vals) if all_vals else 1
        n        = max(len(data), 1)

    has_legend = n_ser > 1
    LEGEND_H   = 16 if has_legend else 0
    PL, PB     = 52, 40   # PB includes room below the category tick labels for the axis title
    pw         = w - PL - 12
    ph         = h - PB - 24 - LEGEND_H
    sp         = pw / n
    # Per-series bar width — grouped layout; stacked uses one column per label
    bw_total   = min(sp * 0.78, 70.0)
    bw         = max(4, bw_total if stacked else bw_total / n_ser)

    safe_unit = _safe_text(unit)
    # Grid lines
    for f in (0.25, 0.5, 0.75, 1.0):
        gy = y0 + PB + f * ph
        c.setStrokeColorRGB(0.88, 0.9, 0.94); c.setLineWidth(0.4)
        c.line(x0 + PL, gy, x0 + PL + pw, gy)
        lbl = f"{f * max_v:.1f}{safe_unit}" if max_v < 10 else f"{f * max_v:.0f}{safe_unit}"
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 6.5)
        c.drawRightString(x0 + PL - 3, gy - 2.5, lbl)

    c.setStrokeColorRGB(0.78, 0.82, 0.88); c.setLineWidth(0.8)
    c.line(x0 + PL, y0 + PB, x0 + PL + pw, y0 + PB)

    if stacked:
        # Draw each label's column as segments stacked bottom-to-top, one
        # segment per series, in series order — the classic stacked-bar look.
        for i, d in enumerate(data):
            if i >= n:
                continue
            bx = x0 + PL + i * sp + (sp - bw) / 2
            running_y = y0 + PB
            for si, ser in enumerate(series_list):
                ser_data = ser.get("data") or []
                if i >= len(ser_data):
                    continue
                v  = abs(_coerce_value(ser_data[i].get("value", 0)))
                bh = max(1.5, (v / max_v) * ph) if v else 0
                color = CHART_COLORS[si % len(CHART_COLORS)]
                c.setFillColorRGB(*color)
                c.rect(bx, running_y, bw - 1, bh, fill=1, stroke=0)
                # Segment value label — only if the segment is tall enough
                # to hold text without overlapping its neighbors.
                if bh > 9:
                    vs = f"{v:.0f}{safe_unit}" if max_v >= 10 else f"{v:.1f}{safe_unit}"
                    c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 5.5)
                    c.drawCentredString(bx + (bw - 1) / 2, running_y + bh / 2 - 2, vs)
                running_y += bh
            # Total label above the stack
            total_v = totals[i] if i < len(totals) else 0
            ts = f"{total_v:.0f}{safe_unit}" if max_v >= 10 else f"{total_v:.1f}{safe_unit}"
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica-Bold", 6)
            c.drawCentredString(bx + (bw - 1) / 2, running_y + 3, ts)
    else:
        # Draw grouped bars
        for si, ser in enumerate(series_list):
            ser_data = ser.get("data") or []
            color    = CHART_COLORS[si % len(CHART_COLORS)]
            for i, d in enumerate(ser_data):
                if i >= n: continue
                v  = _coerce_value(d.get("value", 0))
                bh = max(2, (abs(v) / max_v) * ph)
                group_x = x0 + PL + i * sp + (sp - bw_total) / 2
                bx = group_x + si * bw
                bx = min(bx, x0 + PL + pw - bw)
                bar_color = RED if v < 0 else color
                c.setFillColorRGB(*bar_color)
                c.rect(bx, y0 + PB, bw - 1, bh, fill=1, stroke=0)
                # Value label on top
                if n_ser == 1 or bw > 14:
                    vs = f"{v:+.1f}{safe_unit}" if safe_unit == "%" else f"{v:.1f}{safe_unit}" if max_v < 10 else f"{v:.0f}{safe_unit}"
                    c.setFillColorRGB(*bar_color); c.setFont("Helvetica-Bold", 5.5)
                    c.drawCentredString(bx + (bw - 1) / 2, y0 + PB + bh + 2, vs)

    # X-axis labels — thin these out once bars get too narrow for every
    # label to fit without the (rotated) text overlapping its neighbor.
    # Mirrors the line chart's step-based label skipping below.
    label_step = max(1, math.ceil(n / 10))
    max_chars  = 18 if sp >= 30 else 12 if sp >= 20 else 8
    for i, d in enumerate(data):
        if i % label_step != 0 and i != n - 1:
            continue
        lbl = d.get("label", "")
        bx  = x0 + PL + i * sp + sp / 2
        c.setFillColorRGB(*GREY)
        if len(lbl) > 8:
            c.saveState()
            c.translate(bx, y0 + PB - 4)
            c.rotate(30)
            c.setFont("Helvetica", 6)
            c.drawString(0, 0, lbl[:max_chars])
            c.restoreState()
        else:
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(bx, y0 + PB - 11, lbl[:max_chars])

    # Legend for multi-series — wraps to a new row instead of running off
    # the right edge of the card when there are several series or long names.
    if has_legend:
        lx = x0 + PL
        ly = y0 + PB + ph + 14
        right_edge = x0 + PL + pw
        for si, ser in enumerate(series_list):
            color = CHART_COLORS[si % len(CHART_COLORS)]
            sname = ser.get("name", f"Series {si+1}")[:22]
            item_w = 15 + c.stringWidth(sname, "Helvetica", 7) + 22
            if lx + item_w > right_edge and lx > x0 + PL:
                lx = x0 + PL
                ly -= 12
            c.setFillColorRGB(*color)
            c.rect(lx, ly - 4, 12, 6, fill=1, stroke=0)
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 7)
            c.drawString(lx + 15, ly - 2, sname)
            lx += item_w

    _axis_titles(c, spec, x0, y0, PL, PB, pw, ph)


def _line(c, spec, x0, y0, w, h):
    series = spec.get("series") or []
    if not series:
        return
    unit  = spec.get("unit", "")
    safe_unit = _safe_text(unit)
    title = spec.get("title", "")

    # Collect all values across ALL series for unified Y scale
    all_v = [_coerce_value(d.get("value", 0)) for s in series for d in s.get("data", [])]
    if not all_v:
        return
    mn, mx = min(all_v), max(all_v)
    rng = mx - mn
    if rng < 1e-9:
        rng = max(abs(mx) * 0.1, 1.0)
        mn -= rng / 2
        mx += rng / 2

    has_legend = len(series) > 1
    LEGEND_H   = 16 if has_legend else 0
    PL, PB     = 52, 40   # PB includes room below the category tick labels for the axis title
    pw         = w - PL - 12
    ph         = h - PB - 24 - LEGEND_H

    # Grid lines
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y0 + PB + f * ph
        c.setStrokeColorRGB(0.88, 0.9, 0.94); c.setLineWidth(0.4)
        c.line(x0 + PL, gy, x0 + PL + pw, gy)
        lbl = f"{mn + f * rng:.2f}{safe_unit}" if rng < 5 else f"{mn + f * rng:.1f}{safe_unit}"
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 6.5)
        c.drawRightString(x0 + PL - 3, gy - 2.5, lbl)

    # X axis baseline
    c.setStrokeColorRGB(0.78, 0.82, 0.88); c.setLineWidth(0.8)
    c.line(x0 + PL, y0 + PB, x0 + PL + pw, y0 + PB)

    # Draw each series
    max_pts = max(len(s.get("data", [])) for s in series)
    step    = max(1, max_pts // 8)

    for si, s in enumerate(series):
        pts = s.get("data", [])
        if not pts:
            continue
        color  = CHART_COLORS[si % len(CHART_COLORS)]
        n      = len(pts)
        coords = [
            (x0 + PL + (j / max(n - 1, 1)) * pw,
             y0 + PB + ((_coerce_value(d.get("value", 0)) - mn) / rng) * ph)
            for j, d in enumerate(pts)
        ]
        # Line
        c.setStrokeColorRGB(*color); c.setLineWidth(2.0)
        p = c.beginPath(); p.moveTo(*coords[0])
        for cx2, cy2 in coords[1:]:
            p.lineTo(cx2, cy2)
        c.drawPath(p, fill=0, stroke=1)
        # Dots + value labels on key points
        c.setFillColorRGB(*color)
        for j, (px2, py2) in enumerate(coords):
            show = (j % step == 0 or j == n - 1)
            c.circle(px2, py2, 2.8 if show else 1.8, fill=1, stroke=0)
            if show and has_legend:
                # Small value tooltip above dot
                val = _coerce_value(pts[j].get("value", 0))
                c.setFont("Helvetica-Bold", 5.5)
                c.setFillColorRGB(*color)
                c.drawCentredString(px2, py2 + 5, f"{val:.1f}{safe_unit}")
                c.setFillColorRGB(*color)

        # For a single-series trend (e.g. an index/stock level across
        # sessions) — callout the peak and trough points in gold, echoing
        # the annotated-trend style of the sample report rather than
        # leaving a bare line with no narrative markers.
        if not has_legend and n >= 3:
            vals = [_coerce_value(d.get("value", 0)) for d in pts]
            hi_j, lo_j = vals.index(max(vals)), vals.index(min(vals))
            for j in (hi_j, lo_j):
                if j == hi_j == lo_j:
                    continue
                px2, py2 = coords[j]
                is_hi = (j == hi_j)
                c.setFillColorRGB(*GOLD)
                c.circle(px2, py2, 3.2, fill=1, stroke=0)
                vs = f"{vals[j]:.1f}{safe_unit}" if rng < 5 else f"{vals[j]:,.0f}{safe_unit}"
                c.setFont("Helvetica-Bold", 6)
                label_y = py2 + 8 if is_hi else py2 - 11
                # Keep the callout from being clipped off the top/bottom of
                # the plotted area.
                label_y = min(max(label_y, y0 + PB + 6), y0 + PB + ph + 4)
                c.drawCentredString(px2, label_y, vs)

    # X-axis labels — use full label, no truncation
    labels = series[0].get("data", [])
    for j, d in enumerate(labels):
        if j % step == 0 or j == len(labels) - 1:
            px2 = x0 + PL + (j / max(len(labels) - 1, 1)) * pw
            lbl = d.get("label", "")
            # Rotate long labels
            c.setFillColorRGB(*GREY)
            if len(lbl) > 7:
                c.saveState()
                c.translate(px2, y0 + PB - 4)
                c.rotate(30)
                c.setFont("Helvetica", 6)
                c.drawString(0, 0, lbl[:14])
                c.restoreState()
            else:
                c.setFont("Helvetica", 6.5)
                c.drawCentredString(px2, y0 + PB - 11, lbl[:12])

    # Legend for multi-series — wraps to a new row instead of running off
    # the right edge of the card when there are several series.
    if has_legend:
        lx = x0 + PL
        ly = y0 + PB + ph + 14
        right_edge = x0 + PL + pw
        for si, s in enumerate(series):
            color = CHART_COLORS[si % len(CHART_COLORS)]
            sname = s.get("name", f"Series {si+1}")[:20]
            item_w = 15 + c.stringWidth(sname, "Helvetica", 7) + 20
            if lx + item_w > right_edge and lx > x0 + PL:
                lx = x0 + PL
                ly -= 12
            c.setFillColorRGB(*color)
            c.rect(lx, ly - 4, 12, 6, fill=1, stroke=0)
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 7)
            c.drawString(lx + 15, ly - 2, sname)
            lx += item_w

    _axis_titles(c, spec, x0, y0, PL, PB, pw, ph)


def _pie(c, spec, x0, y0, w, h):
    data = (spec.get("series") or [{}])[0].get("data") or []
    if not data:
        return
    total = sum(abs(_coerce_value(d.get("value", 0))) for d in data) or 1
    R = min(w * 0.38, h * 0.42)
    cx2, cy2 = x0 + R + 12, y0 + h / 2
    angle = math.pi / 2

    for i, d in enumerate(data):
        frac = abs(_coerce_value(d.get("value", 0))) / total
        sweep = frac * 2 * math.pi
        color = CHART_COLORS[i % len(CHART_COLORS)]
        c.setFillColorRGB(*color); c.setStrokeColorRGB(1, 1, 1); c.setLineWidth(1)
        steps = max(4, int(sweep * 20))
        p = c.beginPath(); p.moveTo(cx2, cy2)
        for s in range(steps + 1):
            a = angle + s * sweep / steps
            p.lineTo(cx2 + R * math.cos(a), cy2 + R * math.sin(a))
        p.close(); c.drawPath(p, fill=1, stroke=1)
        angle += sweep

    lx = cx2 + R + 18
    ly = cy2 + R * 0.8
    # Cap the per-row height so the legend never runs past the bottom of the
    # chart card, however many slices there are — with the default spacing
    # (15pt/row) more than ~ (R*1.8)/15 slices would otherwise overflow the
    # card's bottom edge and collide with whatever content follows.
    available_h = max(ly - y0, 30)
    row_h = min(15.0, available_h / max(len(data), 1))
    font_sz = 7 if row_h >= 11 else 6
    for i, d in enumerate(data):
        color = CHART_COLORS[i % len(CHART_COLORS)]
        pct = abs(_coerce_value(d.get("value", 0))) / total * 100
        lbl = d.get("label", "")[:22]
        iy = ly - i * row_h
        box = min(8, row_h - 3)
        c.setFillColorRGB(*color); c.rect(lx, iy - box*0.6, box, box, fill=1, stroke=0)
        c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", font_sz)
        c.drawString(lx + 12, iy - 1, lbl)
        c.setFont("Helvetica-Bold", font_sz)
        c.drawRightString(lx + 12 + 110, iy - 1, f"{pct:.1f}%")


def _datawrapper_image(c, spec, x0, y0, w, h):
    """Draw a fetched Datawrapper PNG export, scaled to fit the card, centred.

    Falls back to the native renderer if pngBytes is missing, corrupt, or not
    a valid PNG (e.g. a JSON status object returned while Datawrapper was still
    rendering the export).
    """
    from reportlab.lib.utils import ImageReader
    import base64 as _b64
    png_bytes = (spec.get("datawrapper") or {}).get("pngBytes")
    if not png_bytes:
        return False
    # Decode base64 string — pngBytes may be a b64 string after JSON serialization
    if isinstance(png_bytes, str):
        try:
            png_bytes = _b64.b64decode(png_bytes)
        except Exception:
            log.warning("Datawrapper pngBytes for chart %r is not valid base64 — using native renderer", spec.get("title", "?"))
            return False
    # Reject non-PNG payloads (JSON status objects, HTML error pages, etc.)
    if not isinstance(png_bytes, (bytes, bytearray)) or png_bytes[:4] != b"\x89PNG":
        log.warning(
            "Datawrapper pngBytes for chart %r is not a valid PNG (%d bytes, starts %r) — using native renderer",
            spec.get("title", "?"), len(png_bytes), png_bytes[:16],
        )
        return False
    try:
        img = ImageReader(io.BytesIO(png_bytes))
        iw, ih = img.getSize()
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        dx = x0 + (w - dw) / 2
        dy = y0 + (h - dh) / 2
        c.drawImage(img, dx, dy, width=dw, height=dh,
                    preserveAspectRatio=True, mask="auto")
        return True
    except Exception as exc:
        log.warning("Failed to draw Datawrapper PNG for chart %r: %s", spec.get("title", "?"), exc)
        return False


def _table(c, spec, x0, y0, w, h):
    """Fallback grid renderer used only if the Datawrapper PNG isn't available."""
    columns = spec.get("columns") or []
    rows = spec.get("rows") or []
    if not columns:
        return
    n_cols = len(columns)
    col_w = w / n_cols
    header_h = 18
    row_h = 14
    # If showing every row wouldn't fit, reserve one row's worth of space for
    # the "+N more rows" note up front — otherwise that note gets computed
    # AFTER filling every available row and ends up drawn below the card's
    # bottom edge, overlapping whatever content follows the table.
    max_rows_no_note = max(1, int((h - header_h) // row_h))
    needs_note = len(rows) > max_rows_no_note
    max_rows = max(1, max_rows_no_note - 1) if needs_note else max_rows_no_note
    shown_rows = rows[:max_rows]

    # Header
    c.setFillColorRGB(*NAVY)
    c.rect(x0, y0 + h - header_h, w, header_h, fill=1, stroke=0)
    c.setFillColorRGB(*WHITE)
    c.setFont("Helvetica-Bold", 7)
    for ci, col in enumerate(columns):
        cx = x0 + ci * col_w + 4
        c.drawString(cx, y0 + h - header_h + 6, str(col)[:int(col_w / 4)])

    # Rows
    c.setFont("Helvetica", 7)
    for ri, row in enumerate(shown_rows):
        ry = y0 + h - header_h - (ri + 1) * row_h
        if ri % 2 == 1:
            c.setFillColorRGB(*LIGHT)
            c.rect(x0, ry, w, row_h, fill=1, stroke=0)
        c.setFillColorRGB(*BODY_TXT)
        for ci, cell in enumerate(row):
            cx = x0 + ci * col_w + 4
            c.drawString(cx, ry + 4, str(cell)[:int(col_w / 4)])

    if len(rows) > max_rows:
        c.setFillColorRGB(*GREY)
        c.setFont("Helvetica-Oblique", 6.5)
        c.drawString(x0, y0 + h - header_h - (max_rows + 1) * row_h - 2,
                     f"+ {len(rows) - max_rows} more rows — see full table on Datawrapper")


def _draw_chart(c, spec, x0, y0, w, h):
    if _datawrapper_image(c, spec, x0, y0, w, h):
        return
    t = spec.get("type", "bar")
    if t == "table":
        _table(c, spec, x0, y0, w, h)
    elif t == "pie":
        _pie(c, spec, x0, y0, w, h)
    elif t == "line":
        _line(c, spec, x0, y0, w, h)
    else:
        _bar(c, spec, x0, y0, w, h)


# ─── PDF builder ──────────────────────────────────────────────────────────────
def build_pdf(report: str, title: str, question: str, summary: str,
              key_stats: list, charts: list, logo_b64: str = "",
              file_images: list | None = None, web_images: list | None = None) -> bytes:
    import json as _json

    # Last-resort: if report arrived as raw JSON, extract the markdown field
    _s = (report or "").strip()
    if _s.startswith("{") and '"report"' in _s:
        try:
            _inner = _json.loads(_s)
            if isinstance(_inner, dict) and "report" in _inner:
                report = (_inner["report"] or "").replace("\\n", "\n").strip()
                title   = title   or _inner.get("title", "")
                summary = summary or _inner.get("summary", "")
                key_stats = key_stats or _inner.get("keyStats", [])
                charts    = charts    or _inner.get("charts", [])
        except Exception as e:
            log.debug("PDF: report field is not double-encoded JSON, using as-is (%s)", e)

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image as PILImage

    PAGE_W, PAGE_H = A4          # 595 x 842 pt
    MARGIN = 52
    CW = PAGE_W - 2 * MARGIN     # content width
    HEADER_H = 34
    FOOTER_H = 22
    BODY_TOP = PAGE_H - HEADER_H - 24   # 24pt gap below header band (was 16 — still touching)
    BODY_BOT = FOOTER_H + 24            # guard above footer

    now_utc = datetime.now(timezone.utc)
    now_str  = now_utc.strftime("%d %b %Y")
    date_str = now_utc.strftime("%d %B %Y")
    domain   = detect_domain(question)
    logo_bytes = _load_logo_bytes(logo_b64)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    page_num = [0]

    # ── Per-page header / footer ───────────────────────────────────────────────
    def hf(section_title: str = ""):
        page_num[0] += 1
        # Body background — paint white over the full body area first so no
        # stray graphics state from a previous page can bleed through.
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, FOOTER_H, PAGE_W, PAGE_H - HEADER_H - FOOTER_H, fill=1, stroke=0)
        # Header
        c.setFillColorRGB(*NAVY)
        c.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
        if logo_bytes:
            if not _draw_logo(c, logo_bytes, MARGIN, PAGE_H - HEADER_H + 5, 72, 22):
                _logo_text()
        else:
            _logo_text()
        if section_title:
            c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 8)
            c.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 18, section_title[:70])
        c.setFillColorRGB(0.65, 0.68, 0.82); c.setFont("Helvetica", 7)
        c.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 7, now_str)
        # Footer — branded: report title + firm name on the left, page number on the right.
        c.setFillColorRGB(0.94, 0.95, 1.0)
        c.rect(0, 0, PAGE_W, FOOTER_H, fill=1, stroke=0)
        c.setStrokeColorRGB(*NAVY); c.setLineWidth(1.2)
        c.line(0, FOOTER_H, PAGE_W, FOOTER_H)
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 7)
        footer_label = f"{(title or 'Market Intelligence Report')[:60]} · Growth Gradual"
        c.drawString(MARGIN, 7, footer_label)
        c.drawRightString(PAGE_W - MARGIN, 7, f"Page {page_num[0]}")

    def _logo_text():
        c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGIN, PAGE_H - HEADER_H + 10, "Growth Gradual")

    # ── cursor helpers ─────────────────────────────────────────────────────────
    y = [BODY_TOP]

    def need(space: float, section: str = ""):
        if y[0] - space < BODY_BOT:
            c.showPage(); hf(section); y[0] = BODY_TOP

    def nl(n: float = 6):
        y[0] -= n

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 1 — TITLE PAGE (navy textured cover, "Market Currents" style)
    # ═══════════════════════════════════════════════════════════════════════════
    # Solid navy background, full bleed
    c.setFillColorRGB(*NAVY)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Subtle stipple texture — a sparse grid of faint dots to approximate the
    # canvas/linen texture of the reference cover without needing an image
    # asset. Kept very low-contrast so it reads as texture, not noise.
    c.saveState()
    c.setFillColorRGB(34/255, 39/255, 92/255)  # one shade lighter than NAVY
    _tex_step = 9
    for _tx in range(0, int(PAGE_W) + _tex_step, _tex_step):
        for _ty in range(0, int(PAGE_H) + _tex_step, _tex_step):
            # Skew every other row slightly so the grid doesn't read as
            # perfectly mechanical — cheap approximation of woven texture.
            _ox = (_tex_step / 2) if (_ty // _tex_step) % 2 else 0
            c.circle(_tx + _ox, _ty, 0.5, fill=1, stroke=0)
    c.restoreState()

    # Gold accent bar at very top (thin, matches brand rule elsewhere)
    c.setFillColorRGB(*GOLD)
    c.rect(0, PAGE_H - 6, PAGE_W, 6, fill=1, stroke=0)

    # Logo in a white card, top-left (source cover contains the logo inside
    # a plain white box since the logo art itself expects a light background)
    logo_card_w, logo_card_h = 150, 76
    logo_card_x, logo_card_y = MARGIN, PAGE_H - 40 - logo_card_h
    c.setFillColorRGB(*WHITE)
    c.rect(logo_card_x, logo_card_y, logo_card_w, logo_card_h, fill=1, stroke=0)
    if logo_bytes:
        if not _draw_logo(c, logo_bytes, logo_card_x + 14, logo_card_y + 15, logo_card_w - 28, logo_card_h - 30):
            c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 14)
            c.drawString(logo_card_x + 14, logo_card_y + logo_card_h / 2 - 5, "Growth Gradual")
    else:
        c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 14)
        c.drawString(logo_card_x + 14, logo_card_y + logo_card_h / 2 - 5, "Growth Gradual")

    # Overline — small-caps gold label, letter-spaced
    overline_y = logo_card_y - 46
    _domain_up = domain.upper()
    overline = _domain_up if _domain_up.endswith("INTELLIGENCE") else f"{_domain_up} INTELLIGENCE"
    c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 9.5)
    _ox = MARGIN
    for _ch in overline:
        c.drawString(_ox, overline_y, _ch)
        _ox += c.stringWidth(_ch, "Helvetica-Bold", 9.5) + 1.6  # manual letter-spacing

    # ── Title — serif, gold, wraps to fit; date line in white beneath it ──────
    raw_title = title or question
    raw_title = re.sub(r"^\*+\s*", "", raw_title.strip())
    m = re.match(r"^(.{0,80}?)(?:\*\*|:|\.\s|,\s*here are|,\s*based on)", raw_title, re.IGNORECASE)
    if m and len(m.group(1).strip()) >= 8:
        raw_title = m.group(1).strip()
    _PREAMBLE_PATTERNS = (
        r"^here(?:'s| is| are)\b",
        r"^based on\b",
        r"^(?:latest|some of the latest)\b.{0,40}\b(news|trends|updates|data)\b",
        r"^the following\b",
        r"^below (?:is|are)\b",
        r"^so,?\s",
        r"^you(?:'re| are)\b",
        r"^i(?:'ve| have| will| can)\b",
        r"^let(?:'s| us)\b",
        r"^looking for\b",
        r"^exploring\b",
        r"^understanding\b",
        r"^a (?:look|guide|deep dive|breakdown|comprehensive|detailed)\b",
        r"^an (?:analysis|overview|exploration|examination|in-depth)\b",
    )
    _SENTENCE_PATTERNS = (
        r":\s*$",
        r"\bas\s+follows\s*$",
        r"\b(?:are|is|were|have\s+been|has\s+been)\s+(?:as\s+follows|below|here|outlined|shown|listed|summarized|summarised)\b",
    )
    if (any(re.match(p, raw_title.strip(), re.IGNORECASE) for p in _PREAMBLE_PATTERNS)
            or any(re.search(p, raw_title.strip(), re.IGNORECASE) for p in _SENTENCE_PATTERNS)):
        q_clean = re.sub(r"(?i)^(tell me about|what are|what is|show me|give me|find me|list|compare|analyse|analyze|explain)\s+", "", question.strip())
        q_clean = q_clean.rstrip("?!").strip()
        raw_title = q_clean[:80] if len(q_clean) >= 8 else f"{domain} Briefing"
    report_title = _strip_inline(raw_title)

    title_size = 27
    while title_size > 18 and c.stringWidth(report_title, "Times-Bold", title_size) > CW:
        title_size -= 1

    def _wrap_title(text: str, font: str, size: float, max_w: float):
        if c.stringWidth(text, font, size) <= max_w:
            return [text]
        words = text.split()
        line1, line2 = [], []
        for w in words:
            test = " ".join(line1 + [w])
            if c.stringWidth(test, font, size) <= max_w:
                line1.append(w)
            else:
                line2.append(w)
        l2_text = " ".join(line2)
        while size > 14 and c.stringWidth(l2_text, font, size) > max_w:
            size -= 1
        return [" ".join(line1), l2_text] if line2 else [" ".join(line1)]

    title_lines = _wrap_title(report_title, "Times-Bold", title_size, CW)
    ty = overline_y - 34
    c.setFillColorRGB(*GOLD); c.setFont("Times-Bold", title_size)
    for tl in title_lines:
        c.drawString(MARGIN, ty, tl)
        ty -= title_size + 6

    # Date subtitle line, in white, directly under the title (mirrors the
    # reference cover's "June 2026" line under "India : Market Currents")
    ty -= 4
    c.setFillColorRGB(*WHITE); c.setFont("Helvetica", 15)
    c.drawString(MARGIN, ty, date_str)
    ty -= 30

    # ── Description paragraph — light grey/white body text ────────────────────
    if summary:
        c.setFillColorRGB(0.82, 0.85, 0.95); c.setFont("Helvetica", 10.5)
        s_lines = _wrap(c, summary, "Helvetica", 10.5, CW)
        if len(s_lines) > 5:
            # Don't hard-cut mid-sentence — back up to the last sentence
            # boundary that fits within the 5-line budget, so the cover
            # paragraph always ends cleanly (with punctuation) rather than
            # stopping on a dangling clause.
            kept_text = " ".join(s_lines[:5])
            last_punct = max(kept_text.rfind(". "), kept_text.rfind("! "), kept_text.rfind("? "))
            if last_punct > len(kept_text) * 0.4:  # only trim back if it doesn't lose too much
                kept_text = kept_text[: last_punct + 1]
            else:
                kept_text = kept_text.rstrip(",;: ") + "…"
            s_lines = _wrap(c, kept_text, "Helvetica", 10.5, CW)
        for ln in s_lines[:5]:
            c.drawString(MARGIN, ty, ln)
            ty -= 15
        ty -= 8

    # Compact "about this report" line — small, unobtrusive, single row
    c.setFillColorRGB(0.55, 0.60, 0.80); c.setFont("Helvetica", 7.5)
    about_line = f"RESEARCH INTELLIGENCE   ·   GENERATED {date_str.upper()}   ·   GROWTH GRADUAL"
    c.drawString(MARGIN, ty, about_line)
    ty -= 22

    # ── Table of contents — compact, restyled for the navy cover ──────────────
    section_headings = []
    for _tok in _tokenise(report):
        if _tok["type"] == "h2":
            section_headings.append(_strip_inline(_tok["text"]))
        if len(section_headings) >= 6:
            break

    STAT_ROW_H = 74   # reserved height for the bottom stat-card row
    available = ty - STAT_ROW_H - 24
    if available >= 60 and section_headings:
        c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 7.5)
        c.drawString(MARGIN, ty, "CONTENTS")
        lw = c.stringWidth("CONTENTS", "Helvetica-Bold", 7.5)
        c.setStrokeColorRGB(*GOLD); c.setLineWidth(0.8)
        c.line(MARGIN, ty - 4, MARGIN + lw, ty - 4)

        toc_y = ty - 18
        col_w2 = (CW - 12) / 2
        col_idx = 0
        for i, heading in enumerate(section_headings):
            if toc_y < ty - available:
                break
            tx = MARGIN + col_idx * (col_w2 + 12)
            c.setFillColorRGB(*GOLD)
            c.roundRect(tx, toc_y - 2, 14, 12, 2, fill=1, stroke=0)
            c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 7)
            c.drawCentredString(tx + 7, toc_y + 1, str(i + 1))
            c.setFillColorRGB(0.85, 0.87, 0.97); c.setFont("Helvetica", 8)
            c.drawString(tx + 18, toc_y, heading[:40])
            col_idx += 1
            if col_idx >= 2:
                col_idx = 0
                toc_y -= 16
        ty = toc_y - 10

    # ── Bottom stat-card row — colour-coded KPI cards (mirrors the reference
    #    cover's NIFTY/SENSEX/sector stat blocks at the foot of the page) ──────
    if key_stats:
        n_cards = min(len(key_stats), 4)
        card_gap = 18
        card_w = (CW - card_gap * (n_cards - 1)) / n_cards
        card_y = 34
        for i in range(n_cards):
            st = key_stats[i]
            val = _safe_text(fmt_inr(str(st.get("value", ""))))[:14]
            lbl = _safe_text(st.get("label", ""))[:60]
            chg = _safe_text(st.get("change", ""))
            col_c = GREEN if chg.startswith("+") or val.startswith("+") else (RED if chg.startswith("-") or val.startswith("-") else WHITE)
            cx = MARGIN + i * (card_w + card_gap)
            c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 17)
            c.drawString(cx, card_y + 26, val)
            c.setFillColorRGB(0.7, 0.74, 0.9); c.setFont("Helvetica-Bold", 7.5)
            lbl_lines = _wrap(c, lbl.upper(), "Helvetica-Bold", 7.5, card_w)
            lyy = card_y + 12
            for ll in lbl_lines[:2]:
                c.drawString(cx, lyy, ll)
                lyy -= 10
            if chg and chg != val:
                c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 7.5)
                c.drawString(cx, lyy, chg)
    # "growth-gradual.com" — placed top-right, level with the overline, so it
    # never collides with the bottom stat-card row (which can run tall when
    # a card's label wraps to two lines).
    c.setFillColorRGB(0.55, 0.60, 0.80); c.setFont("Helvetica", 8)
    c.drawRightString(PAGE_W - MARGIN, overline_y, "growth-gradual.com")
    # Cover-page footer stamp (light text on the navy background) so page
    # numbering is visible from page 1, consistent with the body pages' footer.
    page_num[0] += 1
    c.setFillColorRGB(0.55, 0.60, 0.80); c.setFont("Helvetica", 7)
    c.drawRightString(PAGE_W - MARGIN, 16, f"Page {page_num[0]}")
    c.drawString(MARGIN, 16, f"{(title or 'Market Intelligence Report')[:60]} · Growth Gradual")
    c.showPage()

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGES 2+ — REPORT BODY
    # ═══════════════════════════════════════════════════════════════════════════
    # Section counter used for accent colour cycling
    section_idx = [0]
    current_section = [""]

    def start_page(sec: str = ""):
        hf(sec)
        y[0] = BODY_TOP

    start_page()

    # Section number colour accent cycling
    def accent():
        return SECTION_ACCENTS[section_idx[0] % len(SECTION_ACCENTS)]

    # ── Inline infographic stat-card strip ───────────────────────────────────
    # The cover page only has room for 4 keyStats; this renders additional
    # batches inline in the body so the report's data points show up as
    # scannable cards (not just buried in prose) — mirrors the reference
    # report's "RBI Policy Snapshot" style stat blocks.
    def stat_strip(stats_subset: list, heading_label: str = ""):
        stats_subset = [s for s in (stats_subset or []) if s.get("value") or s.get("label")]
        if not stats_subset:
            return
        n = len(stats_subset)
        gap = 12
        card_w = (CW - gap * (n - 1)) / n
        STRIP_H = 54
        LABEL_H = 12 if heading_label else 0
        need(STRIP_H + LABEL_H + 24, current_section[0])
        nl(14)
        if heading_label:
            c.setFillColorRGB(*GREY); c.setFont("Helvetica-BoldOblique", 7)
            c.drawString(MARGIN, y[0], heading_label.upper())
            nl(LABEL_H)
        bottom = y[0] - STRIP_H
        for i, st in enumerate(stats_subset):
            cx = MARGIN + i * (card_w + gap)
            c.setFillColorRGB(0.96, 0.97, 1.0)
            c.roundRect(cx, bottom, card_w, STRIP_H, 4, fill=1, stroke=0)
            c.setStrokeColorRGB(0.87, 0.9, 0.95); c.setLineWidth(0.6)
            c.roundRect(cx, bottom, card_w, STRIP_H, 4, fill=0, stroke=1)
            val = _safe_text(fmt_inr(str(st.get("value", ""))))[:14]
            lbl = _safe_text(st.get("label", ""))[:28]
            chg = _safe_text(st.get("change", ""))
            col_c = GREEN if chg.startswith("+") or val.startswith("+") else (RED if chg.startswith("-") or val.startswith("-") else NAVY)
            c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 13)
            c.drawString(cx + 8, bottom + STRIP_H - 22, val)
            c.setFillColorRGB(*GREY); c.setFont("Helvetica-Bold", 6.5)
            c.drawString(cx + 8, bottom + STRIP_H - 34, lbl.upper())
            if chg and chg != val:
                c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 7)
                c.drawString(cx + 8, bottom + 6, chg)
        y[0] = bottom - 14

    _stats_strip_1_shown = [False]  # after Executive Summary
    _stats_strip_2_shown = [False]  # before Conclusion

    # ── Token renderer ─────────────────────────────────────────────────────────
    tokens = list(_tokenise(report))
    chart_idx = [0]   # next chart to render
    rendered_charts: set[int] = set()  # indices of charts already rendered inline
    _seen_table_sigs: set[str] = set()  # dedup identical tables (LLM sometimes repeats them)
    _seen_line_sigs: set[str] = set()   # dedup identical bullets/numbered items/paragraphs

    def _line_sig(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip(" .,-—:"))

    for tok in tokens:
        tp = tok["type"]

        # ── Chart placeholder ────────────────────────────────────────────────
        if tp == "chart_placeholder":
            ci = tok.get("index", chart_idx[0])
            chart_idx[0] = ci + 1
            if ci < len(charts) and _is_valid_chart(charts[ci]):
                ch = charts[ci]
                GAP_ABOVE = 8      # breathing room between previous content and this card

                # When a Datawrapper PNG is available the title and axes are baked
                # into the image — no separate ReportLab title bar needed, and we
                # give the card more height so the PNG fills it properly.
                has_dw_png = bool((ch.get("datawrapper") or {}).get("pngBytes"))
                CHART_H    = 220 if has_dw_png else 185   # DW PNG is taller (includes title)
                CARD_HEADER = 0 if has_dw_png else 26     # 0 = no title bar, PNG fills card

                CARD_TOTAL = CHART_H + CARD_HEADER + 10 + GAP_ABOVE
                need(CARD_TOTAL, current_section[0])
                nl(GAP_ABOVE)

                card_top    = y[0]                       # top of card (just below the gap)
                card_bottom = card_top - (CARD_HEADER + CHART_H + 4)

                # White card background
                c.setFillColorRGB(1, 1, 1)
                c.roundRect(MARGIN, card_bottom, CW, card_top - card_bottom, 5, fill=1, stroke=0)
                c.setStrokeColorRGB(0.87, 0.9, 0.95)
                c.roundRect(MARGIN, card_bottom, CW, card_top - card_bottom, 5, fill=0, stroke=1)

                if has_dw_png:
                    # Datawrapper PNG already contains the chart title — fill the
                    # entire card with the image, no separate title bar needed.
                    chart_top = card_top - 4
                else:
                    # Title bar — sits inside the card, near its top
                    title_y = card_top - 18
                    c.setFillColorRGB(*accent())
                    c.rect(MARGIN, title_y - 2, 4, 14, fill=1, stroke=0)
                    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 9)
                    c.drawString(MARGIN + 10, title_y, ch.get("title", "")[:80])
                    c.setStrokeColorRGB(0.9, 0.92, 0.96); c.setLineWidth(0.6)
                    c.line(MARGIN + 8, title_y - 6, MARGIN + CW - 8, title_y - 6)
                    chart_top = title_y - 10

                # Chart body — fills remaining card area below the title bar
                _draw_chart(c, ch, MARGIN + 8, chart_top - CHART_H, CW - 16, CHART_H)
                y[0] = card_bottom - 14   # gap below the card before next content
                rendered_charts.add(ci)
            continue

        # ── Extracted file image placeholder (chart/figure/photo pulled out of
        #    the uploaded file — never a full-page screenshot) ────────────────
        if tp == "file_img_placeholder":
            imgs = file_images or []
            pi = tok.get("index", 0)
            if pi < len(imgs):
                img_info = imgs[pi]
                try:
                    import base64 as _b64
                    import io as _io
                    img_data = _b64.b64decode(img_info["data"])
                    pil_img = PILImage.open(_io.BytesIO(img_data))
                    pil_img = _flatten_to_rgb(pil_img)
                    # Scale to fit content width. Max height kept modest (220pt)
                    # since these are individual extracted charts/figures, not
                    # full pages — a full page would need ~340pt+ to stay legible.
                    MAX_IMG_W = CW
                    MAX_IMG_H = 220
                    ow, oh = pil_img.size
                    scale = min(MAX_IMG_W / ow, MAX_IMG_H / oh, 1.0)
                    iw, ih = ow * scale, oh * scale
                    need(ih + 32, current_section[0])
                    nl(10)
                    # Caption bar above image
                    cap = img_info.get("name", f"Figure {pi+1}")[:80]
                    cx = MARGIN + (CW - iw) / 2
                    c.setFillColorRGB(0.95, 0.96, 0.99)
                    c.roundRect(MARGIN, y[0] - ih - 26, CW, ih + 26, 4, fill=1, stroke=0)
                    c.setStrokeColorRGB(0.87, 0.9, 0.95)
                    c.roundRect(MARGIN, y[0] - ih - 26, CW, ih + 26, 4, fill=0, stroke=1)
                    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 8)
                    c.drawString(MARGIN + 8, y[0] - 14, f"🖼 {cap}")
                    # Draw image
                    img_buf = _io.BytesIO()
                    pil_img.save(img_buf, format="JPEG", quality=85)
                    img_buf.seek(0)
                    img_reader = ImageReader(img_buf)
                    c.drawImage(img_reader, cx, y[0] - ih - 22, width=iw, height=ih, mask="auto")
                    y[0] = y[0] - ih - 36
                except Exception as exc:
                    log.warning("FILE_IMG_%d render failed: %s", pi + 1, exc)
            continue

        # ── Web-search image placeholder (fetched from Tavily image results) ──
        if tp == "web_img_placeholder":
            imgs = web_images or []
            wi = tok.get("index", 0)
            if wi < len(imgs) and imgs[wi]:
                img_info = imgs[wi]
                try:
                    import base64 as _b64
                    import io as _io
                    img_data = _b64.b64decode(img_info["data"])
                    pil_img = PILImage.open(_io.BytesIO(img_data))
                    pil_img = _flatten_to_rgb(pil_img)
                    MAX_IMG_W = CW
                    MAX_IMG_H = 260
                    ow, oh = pil_img.size
                    if ow <= 0 or oh <= 0:
                        log.warning("WEB_IMG_%d has zero dimension (%dx%d) — skipping", wi + 1, ow, oh)
                        continue
                    scale = min(MAX_IMG_W / ow, MAX_IMG_H / oh, 1.0)
                    iw, ih = ow * scale, oh * scale
                    if iw < 10 or ih < 10:
                        log.warning("WEB_IMG_%d scaled too small (%.1fx%.1f) — skipping", wi + 1, iw, ih)
                        continue
                    cap = (img_info.get("caption") or "")[:120]
                    cap_h = 18 if cap else 0
                    need(ih + 26 + cap_h, current_section[0])
                    nl(10)
                    cx = MARGIN + (CW - iw) / 2
                    c.setFillColorRGB(0.97, 0.97, 0.98)
                    c.roundRect(MARGIN, y[0] - ih - 22 - cap_h, CW, ih + 22 + cap_h, 4, fill=1, stroke=0)
                    c.setStrokeColorRGB(0.88, 0.89, 0.91)
                    c.roundRect(MARGIN, y[0] - ih - 22 - cap_h, CW, ih + 22 + cap_h, 4, fill=0, stroke=1)
                    img_buf = _io.BytesIO()
                    pil_img.save(img_buf, format="JPEG", quality=85)
                    img_buf.seek(0)
                    img_reader = ImageReader(img_buf)
                    c.drawImage(img_reader, cx, y[0] - ih - 10, width=iw, height=ih, mask="auto")
                    if cap:
                        c.setFillColorRGB(0.4, 0.42, 0.46); c.setFont("Helvetica-Oblique", 7.5)
                        c.drawCentredString(MARGIN + CW / 2, y[0] - ih - 10 - cap_h + 5, cap)
                    y[0] = y[0] - ih - 22 - cap_h - 10
                except Exception as exc:
                    import traceback as _tb
                    log.warning("WEB_IMG_%d render failed: %s\n%s", wi + 1, exc, _tb.format_exc())
            continue
        if tp == "hr":
            need(12, current_section[0])
            c.setStrokeColorRGB(0.87, 0.9, 0.94); c.setLineWidth(0.6)
            c.line(MARGIN, y[0], PAGE_W - MARGIN, y[0])
            nl(10); continue

        # ── H1 (report title — skip, already on cover) ───────────────────────
        if tp == "h1":
            # Skip rendering the H1 title since it's on the cover page
            continue

        # ── H2 — major section heading ───────────────────────────────────────
        if tp == "h2":
            section_idx[0] += 1
            text = _strip_inline(tok["text"])
            # Strip any leading "N. " the LLM already put in the heading text
            # itself, since we render our own "SECTION 0N" overline instead —
            # avoids a duplicated/mismatched number ("SECTION 02" over "2. Title").
            text_display = re.sub(r"^\d+\.\s*", "", text)

            # Drop in an infographic stat-card strip right as we LEAVE the
            # Executive Summary (using the keyStats batch the cover page had
            # no room for), and again right as we ENTER the Conclusion — so
            # the report's data points surface as scannable cards at two more
            # points beyond the cover, not just inline in prose.
            if "executive summary" in current_section[0].lower() and not _stats_strip_1_shown[0]:
                stat_strip(key_stats[4:8], "Key Metrics")
                _stats_strip_1_shown[0] = True
            if "conclusion" in text_display.lower() and not _stats_strip_2_shown[0]:
                stat_strip(key_stats[8:12], "At a Glance")
                _stats_strip_2_shown[0] = True

            current_section[0] = text
            # 22(overline+gap) + up to 2 title lines + 10(rule) + 14(gap) + 30(min content) = ~110
            need(110, text)
            nl(20)   # visible gap before section heading block
            col = accent()
            # Gold "SECTION 0N" overline, letter-spaced small caps
            ov = f"SECTION {section_idx[0]:02d}"
            c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 8.5)
            _ox = MARGIN
            for _ch in ov:
                c.drawString(_ox, y[0], _ch)
                _ox += c.stringWidth(_ch, "Helvetica-Bold", 8.5) + 1.4
            nl(20)
            # Serif navy title, wraps to 2 lines if needed
            title_font_sz = 16
            while title_font_sz > 11 and c.stringWidth(text_display, "Times-Bold", title_font_sz) > CW:
                title_font_sz -= 1
            title_words = text_display.split()
            t_line1, t_line2 = [], []
            for w in title_words:
                test = " ".join(t_line1 + [w])
                if c.stringWidth(test, "Times-Bold", title_font_sz) <= CW:
                    t_line1.append(w)
                else:
                    t_line2.append(w)
            c.setFillColorRGB(*NAVY); c.setFont("Times-Bold", title_font_sz)
            c.drawString(MARGIN, y[0], " ".join(t_line1)[:90])
            if t_line2:
                nl(title_font_sz + 4)
                c.drawString(MARGIN, y[0], " ".join(t_line2)[:90])
            nl(12)
            # Thin gold rule under the title, in the section's accent colour
            c.setStrokeColorRGB(*col); c.setLineWidth(1.4)
            c.line(MARGIN, y[0], MARGIN + CW, y[0])
            nl(20); continue

        # ── H3 — sub-section ─────────────────────────────────────────────────
        if tp == "h3":
            text = _strip_inline(tok["text"])
            # 8(gap above) + 20(banner) + 24(nl after) + 28(min content below) = 80
            need(80, current_section[0])
            nl(10)   # visible gap before sub-section
            # Mid-navy tinted background (visible, but lighter than H2)
            c.setFillColorRGB(0.18, 0.22, 0.48)
            c.rect(MARGIN, y[0] - 4, CW, 20, fill=1, stroke=0)
            c.setFillColorRGB(*accent())
            c.rect(MARGIN, y[0] - 4, 3, 20, fill=1, stroke=0)
            c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 10.5)
            c.drawString(MARGIN + 9, y[0] + 4, text[:90])
            nl(28); continue   # gap after sub-section banner

        # ── H4 ───────────────────────────────────────────────────────────────
        if tp == "h4":
            text = _strip_inline(tok["text"])
            need(20, current_section[0])
            nl(6)
            c.setFillColorRGB(0.22, 0.28, 0.52); c.setFont("Helvetica-Bold", 10)
            c.drawString(MARGIN, y[0], text[:90])
            nl(14); continue

        # ── Bullet ───────────────────────────────────────────────────────────
        if tp == "bullet":
            plain_text = _strip_inline(tok["text"])
            _sig = _line_sig(plain_text)
            if len(_sig) > 12 and _sig in _seen_line_sigs:
                continue  # exact repeat of an earlier bullet — skip, don't say it twice
            _seen_line_sigs.add(_sig)
            wlines = _wrap(c, plain_text, "Helvetica", 10, CW - 16)
            for li, ln in enumerate(wlines):
                need(15, current_section[0])
                if li == 0:
                    c.setFillColorRGB(*accent())
                    c.circle(MARGIN + 5, y[0] + 3.5, 2.5, fill=1, stroke=0)
                _draw_rich_line(c, MARGIN + 15, y[0], ln, "Helvetica", "Helvetica-Bold", 10, BODY_TXT, CW - 15,
                                 justify=(li < len(wlines) - 1))
                nl(15)
            continue

        # ── Numbered item ────────────────────────────────────────────────────
        if tp == "numbered":
            plain_text = _strip_inline(tok["text"])
            _sig = _line_sig(plain_text)
            if len(_sig) > 12 and _sig in _seen_line_sigs:
                continue  # exact repeat of an earlier finding/item — skip
            _seen_line_sigs.add(_sig)
            num = tok.get("num", "•")
            indent = 18
            wlines = _wrap(c, plain_text, "Helvetica", 10, CW - indent)
            for li, ln in enumerate(wlines):
                need(15, current_section[0])
                if li == 0:
                    c.setFillColorRGB(*accent()); c.setFont("Helvetica-Bold", 10)
                    c.drawString(MARGIN, y[0], f"{num}.")
                _draw_rich_line(c, MARGIN + indent, y[0], ln, "Helvetica", "Helvetica-Bold", 10, BODY_TXT, CW - indent,
                                 justify=(li < len(wlines) - 1))
                nl(15)
            continue

        # ── Paragraph ────────────────────────────────────────────────────────
        if tp == "para":
            raw_text = tok["text"]
            plain_text = _strip_inline(raw_text)
            # Suppress LLM "Note:" disclaimers about missing/unavailable data —
            # they become orphaned and misleading once the empty table is suppressed.
            _tlow = plain_text.lower().strip()
            _DATA_NOTE_HINTS = (
                "note: specific numerical data", "note: the table above indicates",
                "were not directly extractable", "data not available",
                "not directly available in the provided snippets",
            )
            if any(h in _tlow for h in _DATA_NOTE_HINTS):
                continue
            _sig = _line_sig(plain_text)
            if len(_sig) > 40 and _sig in _seen_line_sigs:
                continue  # exact repeat of an earlier paragraph — skip
            _seen_line_sigs.add(_sig)
            wlines = _wrap(c, plain_text, "Helvetica", 10, CW)
            for li, ln in enumerate(wlines):
                need(15, current_section[0])
                _draw_rich_line(c, MARGIN, y[0], ln, "Helvetica", "Helvetica-Bold", 10, BODY_TXT, CW,
                                 justify=(li < len(wlines) - 1))
                nl(15)
            nl(6); continue

        # ── Table ─────────────────────────────────────────────────────────────
        if tp == "table":
            rows = tok["rows"]
            if not rows:
                continue
            # Deduplicate: skip table if an identical one was already rendered
            _tbl_sig = "|".join(",".join(r) for r in rows[:3])
            if _tbl_sig in _seen_table_sigs:
                continue
            _seen_table_sigs.add(_tbl_sig)
            col_count = max(len(r) for r in rows)
            col_w = CW / col_count
            ROW_H = 15

            for ri, row in enumerate(rows):
                need(ROW_H + 2, current_section[0])
                if ri == 0:
                    c.setFillColorRGB(*NAVY)
                    c.rect(MARGIN, y[0] - ROW_H + 4, CW, ROW_H, fill=1, stroke=0)
                    tfont, tsize, tcol = "Helvetica-Bold", 7.5, WHITE
                elif ri % 2 == 0:
                    c.setFillColorRGB(0.96, 0.97, 1.0)
                    c.rect(MARGIN, y[0] - ROW_H + 4, CW, ROW_H, fill=1, stroke=0)
                    tfont, tsize, tcol = "Helvetica", 8.5, BODY_TXT
                else:
                    tfont, tsize, tcol = "Helvetica", 8.5, BODY_TXT

                c.setStrokeColorRGB(0.87, 0.9, 0.94); c.setLineWidth(0.4)
                c.rect(MARGIN, y[0] - ROW_H + 4, CW, ROW_H, fill=0, stroke=1)

                for ci, cell in enumerate(row[:col_count]):
                    cx2 = MARGIN + ci * col_w + 4
                    # Format large numbers in Indian style for data rows (not header)
                    cell_str = str(cell)
                    if ri > 0 and ci > 0:
                        cell_str = fmt_inr(cell_str)
                    cell_str = _strip_inline(cell_str)
                    cell_str = _fit_cell(c, cell_str, tfont, tsize, col_w - 8)
                    # Gain/loss cells (e.g. "+6.4%", "-9.56%") get green/red ink so
                    # the table reads at a glance, same as the chart bars do.
                    cell_color = _signed_cell_color(cell_str) if ri > 0 else None
                    c.setFillColorRGB(*(cell_color or tcol)); c.setFont(tfont, tsize)
                    c.drawString(cx2, y[0] - ROW_H + 7, cell_str)
                nl(ROW_H)
            nl(8)

    # Fallback chart rendering is intentionally suppressed.
    # The LLM system prompt places all charts inline via [CHART_n] placeholders.
    # Dumping leftovers after References corrupts the document structure.

    # Disclaimer removed per product requirement

    c.save()
    return buf.getvalue()


# ─── Route ────────────────────────────────────────────────────────────────────
@router.post("")
async def generate_pdf(request: Request):
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        log.warning("PDF: invalid request body")
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    report: str     = body.get("report", "")
    if not report:
        log.warning("PDF: no report content in request")
        return JSONResponse({"error": "No report content"}, status_code=400)

    title: str      = body.get("title", "")
    question: str   = body.get("question", "Research Report")
    summary: str    = body.get("summary", "")
    key_stats: list = body.get("keyStats", [])
    charts: list    = body.get("charts", [])
    logo_b64: str   = body.get("logoB64", "")
    file_images: list = body.get("fileImages", [])  # [{name, mimeType, data}]
    images: list    = body.get("images", [])  # [{url, caption}] from report generation

    # Debug: log images and whether report contains [WEB_IMG_n] placeholders
    import re as _re_dbg
    _web_img_in_report = _re_dbg.findall(r"\[WEB_IMG_\d+\]", report)
    log.info("PDF: images received=%d  [WEB_IMG] placeholders in report=%s", len(images), _web_img_in_report or "none")
    if images:
        for _ii, _img in enumerate(images[:4]):
            log.info("PDF: images[%d] url=%s caption=%r", _ii, str(_img.get("url",""))[:80] if isinstance(_img,dict) else type(_img), str(_img.get("caption",""))[:40] if isinstance(_img,dict) else "")

    # ── Safety: unwrap double-encoded report (LLM sometimes stuffs JSON into report field) ──
    stripped = report.strip()
    if stripped.startswith("{") and '"report"' in stripped:
        try:
            import json as _json
            inner = _json.loads(stripped)
            if isinstance(inner.get("report"), str) and len(inner["report"]) > 100:
                log.warning("PDF: unwrapping double-encoded report field")
                report  = inner["report"]
                title   = title or inner.get("title", "")
                summary = summary or inner.get("summary", "")
                if not key_stats:
                    key_stats = inner.get("keyStats", [])
                if not charts:
                    charts = inner.get("charts", [])
        except Exception as e:
            log.debug("PDF: report field is not double-encoded JSON, using as-is (%s)", e)

    # Unescape any literal \n sequences
    if "\\n" in report:
        report = report.replace("\\n", "\n")

    # Strip accidental markdown fences
    report = re.sub(r"^```(?:json|markdown)?\s*", "", report.strip())
    report = re.sub(r"```\s*$", "", report).strip()

    log.info("PDF: generating — title=%r  charts=%d  keyStats=%d", title[:60], len(charts), len(key_stats))

    # Charts arriving from the report step already carry chart["datawrapper"]
    # (id/embedUrl/publicUrl/pngUrl) but not the PNG bytes — fetch those now
    # so we can embed real Datawrapper renders in the PDF. Any chart that
    # doesn't have a "datawrapper" block yet (e.g. PDF requested standalone)
    # gets published on the fly. Failures just fall back to the hand-drawn
    # renderer in _draw_chart.
    async def _hydrate_charts(chs: list) -> list:
        needs_publish = [ch for ch in chs if not ch.get("datawrapper")]
        if needs_publish:
            await attach_datawrapper_charts(needs_publish, fetch_png_bytes=False)

        async with httpx.AsyncClient(timeout=120.0) as client:
            async def _one(ch):
                dw = ch.get("datawrapper")
                if not dw:
                    return
                # pngBytes may arrive as a base64 string (survived JSON serialization
                # round-trip through the frontend) — decode it back to bytes.
                existing = dw.get("pngBytes")
                if existing:
                    if isinstance(existing, str):
                        try:
                            import base64 as _b64
                            decoded = _b64.b64decode(existing)
                            if decoded[:4] == b"\x89PNG":
                                dw["pngBytes"] = decoded
                                return  # valid PNG bytes decoded from base64 — done
                        except Exception as e:
                            log.debug("PDF: chart pngBytes base64 decode failed, will re-fetch (%s)", e)
                        dw["pngBytes"] = None  # invalid base64 — re-fetch below
                    elif isinstance(existing, (bytes, bytearray)) and existing[:4] == b"\x89PNG":
                        return  # already valid raw bytes — skip re-fetch
                    else:
                        dw["pngBytes"] = None

                png = await fetch_png(client, dw.get("id", ""), dw.get("pngUrl", ""))
                if png:
                    dw["pngBytes"] = png
                    log.info("PDF: fetched DW PNG for %r (%d bytes)", ch.get("title", "?"), len(png))
                else:
                    log.warning("PDF: DW PNG unavailable for %r — native renderer will be used", ch.get("title", "?"))
            await asyncio.gather(*[_one(ch) for ch in chs])

        n_png = sum(1 for ch in chs if isinstance((ch.get("datawrapper") or {}).get("pngBytes"), (bytes, bytearray)))
        log.info("PDF: %d/%d charts have DW PNG; %d use native renderer", n_png, len(chs), len(chs) - n_png)
        return chs

    try:
        charts = await _hydrate_charts(charts)
    except Exception as exc:
        log.warning("PDF: Datawrapper hydration failed, falling back to drawn charts: %s", exc)

    # The report step only returns {url, caption} for selected web images —
    # fetch the actual bytes now, right before rendering, same pattern as the
    # Datawrapper PNG hydration above. Each image fetch is independent and
    # failures are skipped rather than failing the whole PDF — a missing
    # photo just means one fewer [WEB_IMG_n] renders, nothing else degrades.
    async def _fetch_web_images(imgs: list, max_count: int = 6, max_bytes: int = 6_000_000) -> list[dict | None]:
        if not imgs:
            return []
        sem = asyncio.Semaphore(4)
        out: list[dict | None] = [None] * min(len(imgs), max_count)

        async def _one(i: int, info: dict):
            url = (info.get("url") or "").strip()
            if not url:
                return
            async with sem:
                try:
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                        resp = await client.get(url, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                            "Referer": "https://growth-gradual.com/",
                        })
                    ctype = resp.headers.get("content-type", "")
                    if not resp.is_success:
                        log.debug("WEB_IMG HTTP %d for %s", resp.status_code, url[:80])
                        return
                    if not ctype.startswith("image/"):
                        # Some CDNs return image bytes with generic content-type — check magic bytes
                        content = resp.content
                        img_magic = (
                            content[:4] == b"\x89PNG" or
                            content[:3] == b"\xff\xd8\xff" or  # JPEG
                            content[:6] in (b"GIF87a", b"GIF89a") or
                            content[:4] == b"RIFF"  # WebP
                        )
                        if not img_magic:
                            log.debug("WEB_IMG non-image content-type %r for %s", ctype[:40], url[:80])
                            return
                        content_bytes = content
                    else:
                        content_bytes = resp.content
                    if len(content_bytes) > max_bytes or len(content_bytes) < 500:
                        log.debug("WEB_IMG size %d out of range for %s", len(content_bytes), url[:80])
                        return
                    out[i] = {"data": base64.b64encode(content_bytes).decode("ascii"),
                              "caption": (info.get("caption") or "")[:120]}
                    log.info("WEB_IMG[%d] fetched %d bytes from %s", i + 1, len(content_bytes), url[:60])
                except Exception as exc:
                    log.warning("WEB_IMG[%d] fetch failed for %s: %s", i + 1, url[:80], exc)

        await asyncio.gather(*[_one(i, info) for i, info in enumerate(imgs[:max_count])])
        # IMPORTANT: don't compact `out` by dropping the None gaps left by failed
        # fetches — report_text already has [WEB_IMG_n] placeholders baked in
        # against the ORIGINAL 1-based image order. Compacting would shift every
        # placeholder after a failed fetch onto the wrong photo/caption (or, if
        # the count shrinks below a later placeholder's index, silently drop a
        # perfectly good image instead of the one that actually failed). The
        # renderer in build_pdf already treats a None/missing entry as "skip
        # this placeholder" via its try/except, so leaving the gaps is safe.
        n_ok = sum(1 for img in out if img)
        if n_ok:
            log.info("PDF: fetched %d/%d web images", n_ok, len(out))
        return out

    try:
        web_images = await _fetch_web_images(images)
    except Exception as exc:
        log.warning("PDF: web image fetch failed entirely, continuing without images: %s", exc)
        web_images = []

    # Fallback: if no [WEB_IMG_n] placeholders in report but we have fetched images, inject them
    existing_ph = re.findall(r"\[WEB_IMG_\d+\]", report)
    fetched_imgs = [img for img in web_images if img]
    if fetched_imgs and not existing_ph:
        log.info("PDF: no [WEB_IMG_n] placeholders — injecting fallback for %d image(s)", len(fetched_imgs))
        paragraphs = report.split("\n\n")
        n_para = len(paragraphs)
        if len(fetched_imgs) == 1:
            insertion_points = [max(1, n_para // 4)]
        else:
            step = max(1, n_para // (len(fetched_imgs) + 1))
            insertion_points = [step * (k + 1) for k in range(len(fetched_imgs))]
        for img_idx, para_idx in reversed(list(enumerate(insertion_points))):
            if img_idx < len(web_images) and web_images[img_idx]:
                paragraphs.insert(min(para_idx, n_para - 1), f"\n\n[WEB_IMG_{img_idx + 1}]\n")
        report = "\n\n".join(paragraphs)
        log.info("PDF: injected fallback [WEB_IMG_n] placeholders")

    try:
        pdf_bytes = build_pdf(report, title, question, summary, key_stats, charts, logo_b64, file_images, web_images)
    except Exception as e:
        log.error("PDF: build_pdf failed: %s", e)
        return JSONResponse({"error": f"Failed to generate PDF: {e}"}, status_code=500)

    elapsed = (time.perf_counter() - t0) * 1000
    log.info("PDF: done — %.1f KB in %.0fms", len(pdf_bytes) / 1024, elapsed)

    date_filename = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="growth-gradual-report-{date_filename}.pdf"',
            "Cache-Control": "no-store",
        },
    )
