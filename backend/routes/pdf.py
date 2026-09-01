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
import random
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
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


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float] | None:
    """'#rrggbb' -> (r, g, b) each in 0..1, ReportLab's expected range.
    Returns None for anything malformed so callers can fall back safely."""
    try:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return None
        return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)
    except (ValueError, AttributeError):
        return None


def apply_theme(theme: dict | None) -> None:
    """Override the module's navy/gold brand pair (and everything derived
    from it — chart series colours, section accents) for this render, when
    the report requested a visual theme (see report.py's THEME schema
    field / _sanitize_theme). Every drawing function below reads NAVY/GOLD/
    CHART_COLORS/SECTION_ACCENTS as module globals at call time, so
    reassigning them here before build_pdf renders anything is enough —
    no per-function threading needed. No-ops (keeps the default palette)
    when theme is missing or its colours don't parse as valid hex."""
    global NAVY, GOLD, CHART_COLORS, SECTION_ACCENTS
    if not theme:
        return
    new_navy = _hex_to_rgb01(theme.get("primaryColor", "")) if theme.get("primaryColor") else None
    new_gold = _hex_to_rgb01(theme.get("accentColor", "")) if theme.get("accentColor") else None
    if new_navy is None and new_gold is None:
        return
    NAVY = new_navy or NAVY
    GOLD = new_gold or GOLD
    CHART_COLORS = [NAVY, GOLD, TEAL, RED, SLATE, OLIVE, BURGUNDY, GREEN]
    SECTION_ACCENTS = [GOLD, TEAL, GREEN, OLIVE, RED, SLATE]
    log.info("PDF: applied custom theme — navy=%s gold=%s", theme.get("primaryColor"), theme.get("accentColor"))



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

    # Candlestick points carry open/high/low/close instead of "value" — the
    # generic value-based checks below (identical values, unique labels)
    # don't apply to that shape, so validate it separately and return early.
    if chart_type == "candlestick":
        pts = series[0].get("data") or []
        if len(pts) < 2:
            return False
        labels = [str(p.get("label", "")) for p in pts]
        if len(set(labels)) < len(labels):
            return False
        for d in pts:
            o, hi, lo, cl = (_coerce_value(d.get(k, 0)) for k in ("open", "high", "low", "close"))
            if hi < lo:
                return False
            if o == 0 and hi == 0 and lo == 0 and cl == 0:
                return False
        return True

    # For multi-series (comparison charts): validate each series individually.
    # Single-point series are allowed (e.g. a one-bar snapshot or a single
    # comparison value per company) — line charts still need at least 2
    # points per series since a single point can't be plotted as a line.
    # A waterfall needs at least 3 stages (e.g. opening total, one move,
    # closing total) to read as a bridge rather than a single before/after
    # bar, and a sparkline needs enough points to actually show a "shape"
    # of trend rather than a near-straight line between 2-3 dots.
    for s in series:
        pts = s.get("data") or []
        if chart_type == "line":
            min_pts = 2
        elif chart_type == "waterfall":
            min_pts = 3
        elif chart_type == "sparkline":
            min_pts = 4
        else:
            min_pts = 1
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


@lru_cache(maxsize=6)
def _generate_cover_texture(w_pt: int, h_pt: int, base_rgb: tuple, scale: float = 1.5) -> bytes:
    """
    Procedurally generate a cracked-mosaic / crumpled-fabric canvas texture
    (PNG bytes) for the cover background: a jittered-lattice tessellation of
    small irregular tiles (each a slightly different shade of the brand
    navy, separated by a darker grout line), topped with a soft tonal cloud
    and fine grain — matches the small cellular crackle look of the brand's
    reference cover rather than a flat rect or a smooth linen weave. Built
    purely with PIL (already a hard dependency), cached per (size, colour)
    since every cover in a given process run shares the same values.
    """
    from PIL import Image, ImageDraw, ImageFilter

    W, H = max(1, int(w_pt * scale)), max(1, int(h_pt * scale))
    base = tuple(int(round(v * 255)) for v in base_rgb)

    img = Image.new("RGB", (W, H), base)
    draw = ImageDraw.Draw(img, "RGB")
    rnd = random.Random(11)

    # 1) Jittered-lattice mosaic — adjacent tiles share exact corner points
    # (computed once per lattice vertex, reused by all 4 tiles touching it)
    # so the tessellation has no gaps or overlaps, just irregular cell shapes.
    cell = max(8, round(15 * scale))
    jitter = cell * 0.42
    cols, rows = W // cell + 3, H // cell + 3
    pts = {}
    for gy in range(rows):
        for gx in range(cols):
            pts[(gx, gy)] = (
                gx * cell + rnd.uniform(-jitter, jitter),
                gy * cell + rnd.uniform(-jitter, jitter),
            )
    grout = tuple(max(0, v - 24) for v in base)
    for gy in range(rows - 1):
        for gx in range(cols - 1):
            quad = [pts[(gx, gy)], pts[(gx + 1, gy)], pts[(gx + 1, gy + 1)], pts[(gx, gy + 1)]]
            d = rnd.uniform(-16, 14)
            fill = tuple(max(0, min(255, int(v + d))) for v in base)
            draw.polygon(quad, fill=fill, outline=grout)

    # 2) Soft tonal cloud — large-scale variation so the mosaic doesn't read
    # as perfectly uniform corner to corner.
    sw, sh = max(2, W // 10), max(2, H // 10)
    cloud = Image.new("L", (sw, sh))
    cloud.putdata([rnd.randint(90, 175) for _ in range(sw * sh)])
    cloud = cloud.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(max(1, W / 50)))
    lighter = tuple(min(255, v + 18) for v in base)
    tint = Image.new("RGB", (W, H), lighter)
    img = Image.composite(tint, img, cloud.point(lambda p: int(p * 0.22)))

    # 3) Fine grain speckle — per-pixel noise from os.urandom (fast, no Python loop)
    grain = Image.frombytes("L", (W, H), os.urandom(W * H))
    grain_rgb = Image.merge("RGB", (grain, grain, grain))
    img = Image.blend(img, grain_rgb, 0.03)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mix(c1: tuple, c2: tuple, t: float) -> tuple:
    """Linear-blend two 0-1 RGB tuples: t=0 -> c1, t=1 -> c2."""
    return tuple(c1[i] * (1 - t) + c2[i] * t for i in range(3))


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


def _truncate_words(text: str, limit: int) -> str:
    """Truncate to at most `limit` chars WITHOUT cutting a word in half.
    Backs up to the last whitespace boundary within the limit and appends
    an ellipsis, instead of a hard str[:limit] slice that leaves dangling
    fragments like 'subsequen' when the source text runs long (common for
    LLM-written image captions)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.4:  # don't over-trim short captions
        cut = cut[:last_space]
    return cut.rstrip(" ,;:.-") + "…"


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

        elif re.match(r"^```", stripped):
            # Fenced code block. The LLM occasionally wraps an ASCII
            # diagram/roadmap in a ```...``` fence; previously neither the
            # opening/closing ``` fence markers nor this branch existed at
            # all, so each fence line fell through to the generic "para"
            # branch below and got rendered as literal, visible backtick
            # text with the diagram lines as ordinary paragraphs around it.
            # Now: consume the fence, and yield its contents as normal
            # paragraphs (still no attempt to typeset ASCII-art tables /
            # arrows nicely — that's a separate improvement — but at least
            # the ``` markers themselves never show up as visible text).
            i += 1
            while i < len(lines) and not re.match(r"^```", lines[i].rstrip()):
                inner = lines[i].strip()
                if inner:
                    yield {"type": "para", "text": inner}
                i += 1
            # i now points at the closing ``` (or EOF) — the outer i += 1
            # at the bottom of the loop advances past it.

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

        elif re.match(r"^>\s?", stripped):
            # Blockquote — one or more consecutive "> " lines collapse into a
            # single pull-quote/insight-callout token, rendered as a distinct
            # card (see the "quote" branch below) rather than a plain paragraph.
            q_lines = []
            while i < len(lines) and re.match(r"^>\s?", lines[i].rstrip()):
                q_lines.append(re.sub(r"^>\s?", "", lines[i].rstrip()))
                i += 1
            yield {"type": "quote", "text": " ".join(l for l in q_lines if l).strip()}
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
    # Drop an xLabel that just repeats the chart's own title/series name
    # (e.g. title "S&P 500 Session Volatility" + xLabel "S&P 500") — it adds
    # no information and, on charts with only a couple of category ticks,
    # its centred position can land right on top of the last (rotated) tick
    # label instead of clear of it.
    title = _safe_text((spec.get("title") or "").strip()).lower()
    if x_label and title and (x_label.lower() in title or title in x_label.lower()):
        x_label = ""
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


def _waterfall(c, spec, x0, y0, w, h):
    """Waterfall chart: a running total that steps up/down across labeled
    stages (e.g. Opening AUM -> Inflows -> Redemptions -> Closing AUM),
    drawn as floating bars connected stage-to-stage. A data point marked
    "isTotal": true is an ANCHOR — it resets the running total to its own
    value (used for opening/closing balances) instead of adding a delta.
    Datawrapper has no native chart type for this shape (see the early
    return in utils/datawrapper.py's publish_chart), so this always
    renders through this fallback path, Datawrapper token or not."""
    series_list = spec.get("series") or [{}]
    data = series_list[0].get("data") or []
    if not data:
        return
    unit = spec.get("unit", "")
    safe_unit = _safe_text(unit)

    # Walk the stages once to compute each bar's floating [bottom, top]
    # span. Non-total points are deltas off the running cumulative; total
    # points reset the baseline to their own value.
    segments = []  # (label, bottom, top, is_total, raw_value)
    cum = 0.0
    for d in data:
        v = _coerce_value(d.get("value", 0))
        is_total = bool(d.get("isTotal"))
        if is_total:
            bottom, top = 0.0, v
            cum = v
        else:
            bottom = cum
            top = cum + v
            cum = top
        segments.append((d.get("label", ""), min(bottom, top), max(bottom, top), is_total, v))
    if not segments:
        return

    all_edges = [e for seg in segments for e in (seg[1], seg[2])] + [0.0]
    mn, mx = min(all_edges), max(all_edges)
    rng = mx - mn
    if rng < 1e-9:
        rng = max(abs(mx) * 0.1, 1.0)

    n = len(segments)
    PL, PB = 54, 44
    pw = w - PL - 12
    ph = h - PB - 24
    sp = pw / max(n, 1)
    bw = min(sp * 0.6, 60.0)

    def _y(val):
        return y0 + PB + ((val - mn) / rng) * ph

    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y0 + PB + f * ph
        c.setStrokeColorRGB(0.88, 0.9, 0.94); c.setLineWidth(0.4)
        c.line(x0 + PL, gy, x0 + PL + pw, gy)
        lbl_v = mn + f * rng
        lbl = f"{lbl_v:,.0f}{safe_unit}"
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 6.5)
        c.drawRightString(x0 + PL - 3, gy - 2.5, lbl)

    zero_y = _y(0.0)
    c.setStrokeColorRGB(0.78, 0.82, 0.88); c.setLineWidth(0.8)
    c.line(x0 + PL, zero_y, x0 + PL + pw, zero_y)

    prev_top_y = None
    prev_bx_end = None
    for i, (lbl, bottom, top, is_total, v) in enumerate(segments):
        bx = x0 + PL + i * sp + (sp - bw) / 2
        by, ty = _y(bottom), _y(top)
        bar_h = max(1.5, abs(ty - by))
        bar_y = min(by, ty)
        color = NAVY if is_total else (GREEN if v >= 0 else RED)
        c.setFillColorRGB(*color)
        c.rect(bx, bar_y, bw, bar_h, fill=1, stroke=0)

        # Dotted connector from the previous bar's landing edge to this one
        if prev_top_y is not None and prev_bx_end is not None:
            c.setStrokeColorRGB(0.72, 0.75, 0.82); c.setLineWidth(0.6)
            c.setDash(2, 2)
            c.line(prev_bx_end, prev_top_y, bx, prev_top_y)
            c.setDash()
        prev_top_y = _y(top)
        prev_bx_end = bx + bw

        vs = f"{v:+,.0f}{safe_unit}" if not is_total else f"{v:,.0f}{safe_unit}"
        c.setFillColorRGB(*color); c.setFont("Helvetica-Bold", 6)
        label_y = bar_y + bar_h + 4 if top >= bottom else bar_y - 8
        label_y = min(max(label_y, y0 + PB + 4), y0 + PB + ph + 6)
        c.drawCentredString(bx + bw / 2, label_y, vs)

        c.setFillColorRGB(*GREY)
        if len(lbl) > 9:
            c.saveState()
            c.translate(bx + bw / 2, y0 + PB - 4)
            c.rotate(30)
            c.setFont("Helvetica", 6)
            c.drawString(0, 0, lbl[:16])
            c.restoreState()
        else:
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(bx + bw / 2, y0 + PB - 11, lbl[:12])

    _axis_titles(c, spec, x0, y0, PL, PB, pw, ph)


def _candlestick(c, spec, x0, y0, w, h):
    """OHLC candlestick chart for a single instrument's per-session price
    action. Each data point needs open/high/low/close (not "value") —
    see _is_valid_chart for the shape check. Datawrapper has no native
    candlestick type, so this always renders through this fallback path."""
    series_list = spec.get("series") or [{}]
    data = series_list[0].get("data") or []
    if not data:
        return
    unit = spec.get("unit", "")
    safe_unit = _safe_text(unit)

    def _f(d, k):
        return _coerce_value(d.get(k, 0))

    highs = [_f(d, "high") for d in data]
    lows  = [_f(d, "low") for d in data]
    if not highs or not lows:
        return
    mn, mx = min(lows), max(highs)
    rng = mx - mn
    if rng < 1e-9:
        rng = max(abs(mx) * 0.1, 1.0)
        mn -= rng / 2
        mx += rng / 2

    n = len(data)
    PL, PB = 58, 40
    pw = w - PL - 12
    ph = h - PB - 24
    sp = pw / max(n, 1)
    bw = min(sp * 0.5, 18.0)

    def _y(val):
        return y0 + PB + ((val - mn) / rng) * ph

    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y0 + PB + f * ph
        c.setStrokeColorRGB(0.88, 0.9, 0.94); c.setLineWidth(0.4)
        c.line(x0 + PL, gy, x0 + PL + pw, gy)
        lbl_v = mn + f * rng
        lbl = f"{lbl_v:,.1f}{safe_unit}" if rng < 10 else f"{lbl_v:,.0f}{safe_unit}"
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 6.5)
        c.drawRightString(x0 + PL - 3, gy - 2.5, lbl)

    c.setStrokeColorRGB(0.78, 0.82, 0.88); c.setLineWidth(0.8)
    c.line(x0 + PL, y0 + PB, x0 + PL + pw, y0 + PB)

    label_step = max(1, math.ceil(n / 10))
    for i, d in enumerate(data):
        o, hi, lo, cl = _f(d, "open"), _f(d, "high"), _f(d, "low"), _f(d, "close")
        cx2 = x0 + PL + i * sp + sp / 2
        up = cl >= o
        color = GREEN if up else RED
        c.setStrokeColorRGB(*color); c.setLineWidth(1.0)
        c.line(cx2, _y(lo), cx2, _y(hi))
        body_top, body_bot = _y(max(o, cl)), _y(min(o, cl))
        body_h = max(1.5, body_top - body_bot)
        c.setFillColorRGB(*color)
        c.rect(cx2 - bw / 2, body_bot, bw, body_h, fill=1, stroke=0)

        lbl = d.get("label", "")
        if i % label_step == 0 or i == n - 1:
            c.setFillColorRGB(*GREY)
            if len(lbl) > 7:
                c.saveState()
                c.translate(cx2, y0 + PB - 4)
                c.rotate(30)
                c.setFont("Helvetica", 6)
                c.drawString(0, 0, lbl[:12])
                c.restoreState()
            else:
                c.setFont("Helvetica", 6.5)
                c.drawCentredString(cx2, y0 + PB - 11, lbl[:10])

    # ── Moving-average overlay ──────────────────────────────────────────────
    # Computed directly from the candles' own close prices — no extra data
    # needed from the model — so every candlestick chart gets this for free.
    # A bare wall of red/green candles reads as noisy; a short-window MA line
    # gives the eye a trend to follow, the way any real trading chart does.
    # Window scales down for short series so it still draws on a 5-10 point
    # chart instead of silently vanishing.
    ma_window = 5 if n >= 10 else max(2, n // 2)
    if n >= ma_window + 1:
        closes = [_f(d, "close") for d in data]
        ma_pts = []
        for i in range(ma_window - 1, n):
            avg = sum(closes[i - ma_window + 1:i + 1]) / ma_window
            cx2 = x0 + PL + i * sp + sp / 2
            ma_pts.append((cx2, _y(avg)))
        c.setStrokeColorRGB(*GOLD); c.setLineWidth(1.3)
        p = c.beginPath(); p.moveTo(*ma_pts[0])
        for px2, py2 in ma_pts[1:]:
            p.lineTo(px2, py2)
        c.drawPath(p, fill=0, stroke=1)
        c.setFillColorRGB(*GOLD); c.setFont("Helvetica-BoldOblique", 6.5)
        c.drawString(ma_pts[-1][0] + 4, ma_pts[-1][1] - 2, f"{ma_window}-pd MA")

    _axis_titles(c, spec, x0, y0, PL, PB, pw, ph)


def _sparkline(c, spec, x0, y0, w, h):
    """Minimal axis-less single-series trend line — for a quick 'shape of
    the trend' visual (e.g. a 30-session price trend) where a fully-labeled
    line chart would be visual overkill. No gridlines, no tick labels, just
    the line, its endpoints, and the first/last values. Datawrapper has no
    equivalent chart type at this stripped-down scale, so this always
    renders through this fallback path."""
    series_list = spec.get("series") or [{}]
    data = series_list[0].get("data") or []
    if len(data) < 2:
        return
    unit = spec.get("unit", "")
    safe_unit = _safe_text(unit)
    vals = [_coerce_value(d.get("value", 0)) for d in data]
    mn, mx = min(vals), max(vals)
    rng = mx - mn
    if rng < 1e-9:
        rng = max(abs(mx) * 0.1, 1.0)
        mn -= rng / 2

    up = vals[-1] >= vals[0]
    color = GREEN if up else RED

    title = _safe_text((spec.get("title") or "").strip())
    TITLE_H = 14 if title else 0
    PAD_X, PAD_TOP, PAD_BOT = 34, 14 + TITLE_H, 16
    pw = w - 2 * PAD_X
    ph = h - PAD_TOP - PAD_BOT
    n = len(vals)
    coords = [
        (x0 + PAD_X + (i / max(n - 1, 1)) * pw, y0 + PAD_BOT + ((v - mn) / rng) * ph)
        for i, v in enumerate(vals)
    ]

    if title:
        c.setFillColorRGB(*GREY); c.setFont("Helvetica-Oblique", 7)
        c.drawCentredString(x0 + w / 2, y0 + h - 10, title[:44])

    # ── Soft area fill under the line ───────────────────────────────────────
    # A bare stroke on white reads as thin/unfinished at this small scale;
    # a light tint of the trend colour under the curve gives it the same
    # "finished chart" weight as the full bar/line renders elsewhere in the
    # report, while staying subtle enough not to compete with the line itself.
    fill_color = _mix(WHITE, color, 0.14)
    c.setFillColorRGB(*fill_color)
    fp = c.beginPath()
    fp.moveTo(coords[0][0], y0 + PAD_BOT)
    for px2, py2 in coords:
        fp.lineTo(px2, py2)
    fp.lineTo(coords[-1][0], y0 + PAD_BOT)
    fp.close()
    c.drawPath(fp, fill=1, stroke=0)

    c.setStrokeColorRGB(*color); c.setLineWidth(2.2)
    p = c.beginPath(); p.moveTo(*coords[0])
    for px2, py2 in coords[1:]:
        p.lineTo(px2, py2)
    c.drawPath(p, fill=0, stroke=1)

    c.setFillColorRGB(*color)
    for px2, py2 in (coords[0], coords[-1]):
        c.circle(px2, py2, 2.6, fill=1, stroke=0)

    def _fmt(v):
        return f"{v:,.1f}{safe_unit}" if abs(v) < 100 else f"{v:,.0f}{safe_unit}"

    c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica-Bold", 7)
    c.drawString(coords[0][0], coords[0][1] - 12, _fmt(vals[0]))
    c.setFillColorRGB(*color)
    c.drawRightString(coords[-1][0], coords[-1][1] + 6, _fmt(vals[-1]))


def _datawrapper_image(c, spec, x0, y0, w, h):
    """Draw a fetched Datawrapper PNG export, scaled to fit the card, centred.

    Falls back to the native renderer if pngBytes is missing, corrupt, or not
    a valid PNG (e.g. a JSON status object returned while Datawrapper was still
    rendering the export).

    Datawrapper has no literal axis-title field for bar/column/line charts
    (confirmed platform limitation — only scatter plots get one). Previously
    xLabel/yLabel were folded into the chart's "intro" subtitle sentence
    baked into the PNG (e.g. "Amount (INR) by Financial Metric"), which reads
    as a description line under the title, not a real axis label sitting at
    the axis. Fixed here by carving out a thin margin around the PNG —
    bottom for the x-axis title, left (rotated) for the y-axis title — and
    drawing real ReportLab text there with the exact same styling/position
    convention _axis_titles() already uses for natively-rendered charts, so
    a Datawrapper chart and a native chart both end up with genuine,
    consistently-placed axis labels instead of just one of the two.
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
        # Axis titles only make sense for XY chart types — pies/donuts/tables
        # have no x/y axes to label, so skip reserving margin for them.
        dw_type = str(spec.get("type", "bar")).lower()
        # Scatter plots are the one chart type Datawrapper gives *native*
        # per-axis titles to (baked into the exported PNG from the CSV's own
        # column headers — see publish_chart/_spec_to_csv). Drawing our own
        # xLabel/yLabel overlay on top of that, as we do for bar/line/arrow
        # charts (which have no native axis titles at all), just stacks a
        # second, differently-worded label over the real one — e.g. the
        # native "Rating" title plus an overlaid "Cost (Rs.)" both fighting
        # for the same y-axis. Skip the overlay for scatter; the PNG already
        # has the real thing.
        wants_axis_titles = dw_type not in ("pie", "donut", "table", "scatter")
        x_label = _safe_text((spec.get("xLabel") or "").strip()) if wants_axis_titles else ""
        y_label = _safe_text((spec.get("yLabel") or "").strip()) if wants_axis_titles else ""
        if not y_label and wants_axis_titles:
            y_label = _safe_text(spec.get("unit", ""))
        title_lower = _safe_text((spec.get("title") or "").strip()).lower()
        if x_label and title_lower and (x_label.lower() in title_lower or title_lower in x_label.lower()):
            x_label = ""

        PB = 13 if x_label else 0   # bottom strip for the x-axis title
        PL = 13 if y_label else 0   # left strip for the (rotated) y-axis title

        img = ImageReader(io.BytesIO(png_bytes))
        iw, ih = img.getSize()
        avail_w, avail_h = max(w - PL, 1), max(h - PB, 1)
        scale = min(avail_w / iw, avail_h / ih)
        dw, dh = iw * scale, ih * scale
        dx = x0 + PL + (avail_w - dw) / 2
        dy = y0 + PB + (avail_h - dh) / 2
        c.drawImage(img, dx, dy, width=dw, height=dh,
                    preserveAspectRatio=True, mask="auto")

        if x_label:
            c.setFillColorRGB(*GREY); c.setFont("Helvetica-Oblique", 6.5)
            c.drawCentredString(x0 + PL + avail_w / 2, y0 + 2, x_label[:40])
        if y_label:
            c.saveState()
            c.translate(x0 + 10, y0 + PB + avail_h / 2)
            c.rotate(90)
            c.setFillColorRGB(*GREY); c.setFont("Helvetica-Oblique", 6.5)
            c.drawCentredString(0, 0, y_label[:30])
            c.restoreState()
        return True
    except Exception as exc:
        log.warning("Failed to draw Datawrapper PNG for chart %r: %s", spec.get("title", "?"), exc)
        return False


def _wrap_to_width(c, text: str, font: str, size: float, max_w: float, max_lines: int = 3) -> list[str]:
    """Word/char-wrap `text` to fit `max_w` points, measuring actual glyph
    width (c.stringWidth) instead of a crude character-count guess. Falls
    back to hard character breaks for single "words" wider than max_w on
    their own (e.g. a long URL with no spaces) so it still fits rather than
    silently overflowing. Truncates with an ellipsis if it still doesn't
    fit in max_lines."""
    if not text:
        return [""]
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if c.stringWidth(candidate, font, size) <= max_w or not cur:
            # Word itself may still be wider than max_w (e.g. a bare URL) —
            # hard-break it character by character in that case.
            if c.stringWidth(candidate, font, size) <= max_w:
                cur = candidate
                continue
            # cur is empty and the single word overflows — break it up.
            piece = ""
            for ch in word:
                if c.stringWidth(piece + ch, font, size) <= max_w:
                    piece += ch
                else:
                    lines.append(piece)
                    piece = ch
            cur = piece
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and c.stringWidth(last + "…", font, size) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines or [""]


def _table(c, spec, x0, y0, w, h):
    """Fallback grid renderer used only if the Datawrapper PNG isn't available.

    Column widths used to be split perfectly evenly (w / n_cols) and every
    cell was hard-truncated to `col_w / 4` characters regardless of what
    that column actually held. For a 3-4 column table that gives each
    column ~25-30 characters — fine for a short "Data Type" cell, but it
    silently chopped every URL down to a meaningless "https://uk.finance.
    yahoo.com/q…" with no way to tell what page it actually pointed to.
    Fixed by (1) sizing columns by their real content instead of splitting
    evenly, and (2) wrapping long cells (URLs in particular) across up to
    3 lines using actual measured text width instead of truncating them.
    """
    columns = spec.get("columns") or []
    rows = spec.get("rows") or []
    if not columns or not rows:
        return
    n_cols = len(columns)
    header_h = 18
    PAD = 4

    HEADER_FONT, HEADER_SIZE = "Helvetica-Bold", 7
    BODY_FONT, BODY_SIZE = "Helvetica", 6.5

    # ── Column widths: proportional to the longest content each column
    # actually holds (header or any cell), not a blind even split. A
    # generous cap keeps any single very-long column (URLs) from starving
    # the rest down to nothing.
    def _content_weight(ci: int) -> float:
        longest = len(str(columns[ci]))
        for row in rows:
            if ci < len(row):
                longest = max(longest, len(str(row[ci])))
        return min(longest, 60)  # cap so one huge cell doesn't dominate

    weights = [max(_content_weight(ci), 6) for ci in range(n_cols)]
    total_w_units = sum(weights) or n_cols
    min_col_w = 46.0
    col_ws = [max(min_col_w, w * (wt / total_w_units)) for wt in weights]
    # If mins pushed the total over the card width, rescale everything down
    # proportionally so columns still sum to exactly `w`.
    scale = w / sum(col_ws)
    col_ws = [cw * scale for cw in col_ws]
    col_x = [x0]
    for cw in col_ws[:-1]:
        col_x.append(col_x[-1] + cw)

    # ── Pre-wrap every cell (and header) now, using each column's actual
    # width, so we know how tall each row needs to be before drawing.
    def _wrap_cell(text: str, ci: int, font: str, size: float) -> list[str]:
        return _wrap_to_width(c, str(text), font, size, col_ws[ci] - 2 * PAD, max_lines=3)

    wrapped_rows: list[list[list[str]]] = []
    row_heights: list[float] = []
    LINE_H = 8
    for row in rows:
        cells_wrapped = [_wrap_cell(row[ci] if ci < len(row) else "", ci, BODY_FONT, BODY_SIZE)
                          for ci in range(n_cols)]
        n_lines = max(len(cw) for cw in cells_wrapped)
        wrapped_rows.append(cells_wrapped)
        row_heights.append(max(14, n_lines * LINE_H + 6))

    # If showing every row wouldn't fit, reserve space for a "+N more rows"
    # note up front — computed against real (variable) row heights this
    # time, rather than assuming every row is the same fixed height.
    avail_h = h - header_h
    NOTE_H = 12
    # Reserve room for the "+N more rows" note up front whenever not every
    # row is going to fit — otherwise that note gets computed AFTER filling
    # every available row and ends up drawn below the card's bottom edge.
    fits_all = sum(row_heights) <= avail_h
    budget = avail_h if fits_all else max(0.0, avail_h - NOTE_H)
    shown_rows: list[list[list[str]]] = []
    shown_heights: list[float] = []
    running = 0.0
    for cw, rh in zip(wrapped_rows, row_heights):
        if running + rh > budget and shown_rows:
            break
        running += rh
        shown_rows.append(cw)
        shown_heights.append(rh)
    needs_note = len(shown_rows) < len(wrapped_rows)

    # Header
    c.setFillColorRGB(*NAVY)
    c.rect(x0, y0 + h - header_h, w, header_h, fill=1, stroke=0)
    c.setFillColorRGB(*WHITE)
    c.setFont(HEADER_FONT, HEADER_SIZE)
    for ci, col in enumerate(columns):
        cx = col_x[ci] + PAD
        c.drawString(cx, y0 + h - header_h + 6, str(col)[:40])

    # Rows
    ry_top = y0 + h - header_h
    for ri, (cells, rh) in enumerate(zip(shown_rows, shown_heights)):
        ry = ry_top - rh
        if ri % 2 == 1:
            c.setFillColorRGB(*LIGHT)
            c.rect(x0, ry, w, rh, fill=1, stroke=0)
        c.setFillColorRGB(*BODY_TXT)
        c.setFont(BODY_FONT, BODY_SIZE)
        for ci, lines in enumerate(cells):
            cx = col_x[ci] + PAD
            # Cells that look like a URL are also drawn as real clickable
            # links (in addition to being fully visible, wrapped text) so
            # the source is one click away instead of a dead-end ellipsis.
            cell_text_full = " ".join(lines)
            is_url = cell_text_full.strip().lower().startswith(("http://", "https://"))
            ty = ry + rh - LINE_H
            for line in lines:
                c.drawString(cx, ty, line)
                ty -= LINE_H
            if is_url:
                c.linkURL(cell_text_full.strip(), (cx, ry, col_x[ci] + col_ws[ci] - PAD, ry + rh),
                          relative=0, thickness=0)
        ry_top = ry

    if needs_note:
        n_more = len(wrapped_rows) - len(shown_rows)
        c.setFillColorRGB(*GREY)
        c.setFont("Helvetica-Oblique", 6.5)
        c.drawString(x0, ry_top - 10, f"+ {n_more} more row{'s' if n_more != 1 else ''} — see full table on Datawrapper")


def _draw_chart(c, spec, x0, y0, w, h):
    t = spec.get("type", "bar")
    # waterfall/candlestick/sparkline have no Datawrapper equivalent (see
    # the early return in utils/datawrapper.py's publish_chart) — skip the
    # Datawrapper-image lookup entirely for them rather than wasting a
    # lookup that will never find anything under chart["datawrapper"].
    if t not in ("waterfall", "candlestick", "sparkline") and _datawrapper_image(c, spec, x0, y0, w, h):
        return
    if t == "table":
        _table(c, spec, x0, y0, w, h)
    elif t == "pie":
        _pie(c, spec, x0, y0, w, h)
    elif t == "line":
        _line(c, spec, x0, y0, w, h)
    elif t == "waterfall":
        _waterfall(c, spec, x0, y0, w, h)
    elif t == "candlestick":
        _candlestick(c, spec, x0, y0, w, h)
    elif t == "sparkline":
        _sparkline(c, spec, x0, y0, w, h)
    else:
        _bar(c, spec, x0, y0, w, h)


# ─── PDF builder ──────────────────────────────────────────────────────────────
def build_pdf(report: str, title: str, question: str, summary: str,
              key_stats: list, charts: list, logo_b64: str = "",
              file_images: list | None = None, web_images: list | None = None,
              theme: dict | None = None) -> bytes:
    apply_theme(theme)
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
            # Pixel-width-aware ellipsis instead of a blind [:70] char slice,
            # which could cut a long section title off mid-word right against
            # the page-number/date area with no visual indication of the cut.
            title_max_w = PAGE_W - MARGIN - 150
            c.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 18,
                               _fit_cell(c, section_title, "Helvetica-Bold", 8, title_max_w))
        c.setFillColorRGB(0.65, 0.68, 0.82); c.setFont("Helvetica", 7)
        c.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 7, now_str)
        # Footer — branded: report title + firm name on the left, page number on the right.
        c.setFillColorRGB(0.94, 0.95, 1.0)
        c.rect(0, 0, PAGE_W, FOOTER_H, fill=1, stroke=0)
        c.setStrokeColorRGB(*NAVY); c.setLineWidth(1.2)
        c.line(0, FOOTER_H, PAGE_W, FOOTER_H)
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 7)
        # Same fix here: was f"{title[:60]} · Growth Gradual", a fixed
        # character count that cuts a long title mid-word ("...Ten Million
        # Dollar Busine · Growth Gradual") regardless of the actual pixel
        # width available on the page.
        suffix = " · Growth Gradual"
        footer_max_w = PAGE_W - 2 * MARGIN - 70 - c.stringWidth(suffix, "Helvetica", 7)
        footer_title = _fit_cell(c, _safe_text(title) or "Market Intelligence Report", "Helvetica", 7, footer_max_w)
        footer_label = f"{footer_title}{suffix}"
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
    # Solid navy fallback fill first (belt-and-braces if texture gen fails)
    c.setFillColorRGB(*NAVY)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Real woven canvas/linen texture, full bleed — see _generate_cover_texture().
    try:
        _tex_png = _generate_cover_texture(int(PAGE_W), int(PAGE_H), NAVY)
        c.drawImage(ImageReader(io.BytesIO(_tex_png)), 0, 0, width=PAGE_W, height=PAGE_H, mask=None)
    except Exception as _tex_exc:
        log.warning("PDF: cover texture generation failed, falling back to flat navy (%s)", _tex_exc)

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
        summary = _safe_text(summary)
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
            heading_text = _strip_inline(_tok["text"])
            # Report bodies often self-number headings ("1. Historical
            # Context...") while the TOC below assigns its own sequential
            # badge number starting at 1 for "Executive Summary". Keeping
            # both produces mismatched double-numbering (badge "2" next to
            # heading text starting with "1."). Strip any leading "N." /
            # "N)" prefix so the badge is the single source of numbering.
            heading_text = re.sub(r"^\d+[\.\)]\s*", "", heading_text)
            section_headings.append(heading_text)
        if len(section_headings) >= 9:
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
            max_tw = col_w2 - 18 - 4
            c.drawString(tx + 18, toc_y, _fit_cell(c, heading, "Helvetica", 8, max_tw))
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
            lbl = _safe_text(st.get("label", ""))
            chg = _safe_text(st.get("change", ""))
            col_c = GREEN if chg.startswith("+") or val.startswith("+") else (RED if chg.startswith("-") or val.startswith("-") else WHITE)
            cx = MARGIN + i * (card_w + card_gap)
            c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 17)
            c.drawString(cx, card_y + 26, val)
            c.setFillColorRGB(0.7, 0.74, 0.9); c.setFont("Helvetica-Bold", 7.5)
            lbl_lines = _wrap(c, lbl.upper(), "Helvetica-Bold", 7.5, card_w)[:2]
            if len(lbl_lines) == 2 and c.stringWidth(lbl_lines[1], "Helvetica-Bold", 7.5) >= card_w:
                lbl_lines[1] = _fit_cell(c, lbl_lines[1], "Helvetica-Bold", 7.5, card_w)
            lyy = card_y + 12
            for ll in lbl_lines:
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
    c.drawString(MARGIN, 16, f"{_fit_cell(c, _safe_text(title) or 'Market Intelligence Report', 'Helvetica', 7, PAGE_W - 2 * MARGIN - 70 - c.stringWidth(' · Growth Gradual', 'Helvetica', 7))} · Growth Gradual")
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
    def stat_strip(stats_subset: list, heading_label: str = "", strip_color: tuple = None):
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
        _col = strip_color if strip_color is not None else accent()
        card_tint = _mix(WHITE, _col, 0.12)
        card_border = _mix(WHITE, _col, 0.35)
        for i, st in enumerate(stats_subset):
            cx = MARGIN + i * (card_w + gap)
            c.setFillColorRGB(*card_tint)
            c.roundRect(cx, bottom, card_w, STRIP_H, 4, fill=1, stroke=0)
            c.setStrokeColorRGB(*card_border); c.setLineWidth(0.6)
            c.roundRect(cx, bottom, card_w, STRIP_H, 4, fill=0, stroke=1)
            val = _safe_text(fmt_inr(str(st.get("value", ""))))[:14]
            lbl_raw = _safe_text(st.get("label", "")).upper()
            chg = _safe_text(st.get("change", ""))
            col_c = GREEN if chg.startswith("+") or val.startswith("+") else (RED if chg.startswith("-") or val.startswith("-") else NAVY)
            c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 13)
            c.drawString(cx + 8, bottom + STRIP_H - 22, val)
            c.setFillColorRGB(*GREY); c.setFont("Helvetica-Bold", 6.5)
            # Pixel-width-aware wrap (was a blind [:28] char slice, which cut
            # mid-word with no ellipsis — e.g. "...MILLIONAIRES WITHO" — since
            # 28 characters is not a fixed width across different letters).
            lbl_max_w = card_w - 16
            lbl_lines = _wrap(c, lbl_raw, "Helvetica-Bold", 6.5, lbl_max_w)[:2]
            if len(lbl_lines) == 2 and c.stringWidth(lbl_lines[1], "Helvetica-Bold", 6.5) >= lbl_max_w:
                lbl_lines[1] = _fit_cell(c, lbl_lines[1], "Helvetica-Bold", 6.5, lbl_max_w)
            lyy = bottom + STRIP_H - 34
            for ll in lbl_lines:
                c.drawString(cx + 8, lyy, ll)
                lyy -= 8
            if chg and chg != val:
                c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 7)
                c.drawString(cx + 8, bottom + 6, chg)
        y[0] = bottom - 14

    # ── Dynamic stat-strip cadence ───────────────────────────────────────────
    # Rather than hardcoding "one strip after Executive Summary, one before
    # Conclusion" (which forced every report into the same rhythm regardless
    # of how many sections it actually had, or where its data-dense parts
    # fell), a strip is dropped in after whichever sections turn out to be
    # data-rich (a table, or 3+ bullets/numbered points) — up to a cap — so
    # the cadence tracks the actual shape of the content instead of a fixed
    # template. The first 4 keyStats are reserved for the cover; the rest
    # are drawn down here as data-rich sections are encountered.
    _STAT_STRIP_MAX = 3
    _stat_strip_count = [0]
    _stat_pool = list(key_stats[4:]) if key_stats else []
    _sec_signal = {"bullets": 0, "tables": 0}
    _sec_text_buf = [""]   # plain text seen so far in the CURRENT section, for stat relevance matching

    _STOPWORDS = {"the", "and", "for", "with", "from", "this", "that", "yield",
                  "price", "change", "return", "value", "level", "close", "rate"}

    def _pick_relevant_stats(pool: list, section_text: str, k: int = 4) -> list:
        """Pull up to k stats out of `pool` (in place) that are actually about
        this section's topic, instead of just taking whichever 4 happen to be
        next in the list. A stat like "Marqeta 5-Yr Return" showing up under
        an "Energy Markets" section (because it was simply next in line) reads
        as a mistake — this scores each candidate by whether its label's
        distinctive words appear anywhere in the section's own text, and only
        falls back to FIFO order if nothing in the pool actually matches."""
        if not pool:
            return []
        text_lower = section_text.lower()
        scored = []
        for st in pool:
            label = str(st.get("label", "")).lower()
            words = [w.strip(".,()%") for w in re.split(r"\s+", label) if len(w.strip(".,()%")) >= 4]
            words = [w for w in words if w not in _STOPWORDS]
            score = sum(1 for w in words if w in text_lower)
            scored.append((score, st))
        matched = [st for score, st in scored if score > 0]
        if not matched:
            take = pool[:k]
            del pool[:k]
            return take
        take = matched[:k]
        for st in take:
            pool.remove(st)
        return take

    # ── Token renderer ─────────────────────────────────────────────────────────
    tokens = list(_tokenise(report))
    chart_idx = [0]   # next chart to render
    rendered_charts: set[int] = set()  # indices of charts already rendered inline
    _seen_table_sigs: set[str] = set()  # dedup identical tables (LLM sometimes repeats them)
    _seen_line_sigs: set[str] = set()   # dedup identical bullets/numbered items/paragraphs

    # Data Sources is the last REQUIRED ANCHOR in the system prompt's section
    # order — "the ONLY sources listing... nothing more". In practice the
    # model occasionally tacks on a stray token after that table anyway (a
    # [CHART_n] placeholder with no heading/context above it, an extra
    # paragraph, etc.), which renders as an orphaned, unlabeled chart sitting
    # alone on a trailing page with no surrounding text — seen in production.
    # Once we've rendered the Data Sources heading + its one table, drop
    # every token that follows instead of rendering it.
    _past_data_sources = [False]
    _reached_data_sources_heading = False

    def _line_sig(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip(" .,-—:"))

    for tok in tokens:
        tp = tok["type"]

        if _past_data_sources[0]:
            continue

        # ── Chart placeholder ────────────────────────────────────────────────
        if tp == "chart_placeholder":
            ci = tok.get("index", chart_idx[0])
            chart_idx[0] = ci + 1
            if ci < len(charts) and _is_valid_chart(charts[ci]):
                ch = charts[ci]
                _sec_text_buf[0] += " " + str(ch.get("title", "")).lower()
                GAP_ABOVE = 8      # breathing room between previous content and this card

                # When a Datawrapper PNG is available the title and axes are baked
                # into the image — no separate ReportLab title bar needed, and we
                # give the card more height so the PNG fills it properly.
                # Bumped from 220/185 — the extra vertical room lets Datawrapper's
                # own renderer space out gridlines, tick labels and value labels
                # instead of cramming them into a shorter frame, which is what
                # made charts read as small/busy at the old sizing.
                has_dw_png = bool((ch.get("datawrapper") or {}).get("pngBytes"))
                CHART_H    = 260 if has_dw_png else 215   # DW PNG is taller (includes title)
                CARD_HEADER = 0 if has_dw_png else 26     # 0 = no title bar, PNG fills card

                # Row-based chart types (arrow-plots, tables) now export at
                # "auto" height (see utils/datawrapper.py _export_dims) so
                # a 2-3 row chart isn't forced into the same fixed export
                # box as a full-size bar/line chart. But the card height
                # here was still a flat 220pt regardless — a short PNG just
                # ends up centred in a lot of extra white padding instead
                # of the old "content squeezed into 15% of an oversized
                # image" bug. Size the card to the PNG's real proportions
                # (at the card's fixed width) for these chart types,
                # clamped so it never gets absurdly short or taller than
                # the original fixed allocation.
                if has_dw_png:
                    dw_type = str(ch.get("type", "")).lower()
                    if dw_type in ("arrow", "table"):
                        try:
                            png_bytes = (ch.get("datawrapper") or {}).get("pngBytes")
                            if isinstance(png_bytes, str):
                                import base64 as _b64chk
                                png_bytes = _b64chk.b64decode(png_bytes)
                            from reportlab.lib.utils import ImageReader as _IR
                            _pw, _ph = _IR(io.BytesIO(png_bytes)).getSize()
                            fitted_h = (CW - 16) * (_ph / _pw)
                            CHART_H = max(80, min(260, fitted_h))
                        except Exception:
                            pass   # fall back to the flat 220pt default above

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
                    c.drawString(MARGIN + 10, title_y, _safe_text(ch.get("title", ""))[:80])
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
                    cap = _truncate_words(img_info.get("caption") or "", 120)
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
                        cap_fit = _fit_cell(c, cap, "Helvetica-Oblique", 7.5, CW - 20)
                        c.drawCentredString(MARGIN + CW / 2, y[0] - ih - 10 - cap_h + 5, cap_fit)
                    y[0] = y[0] - ih - 22 - cap_h - 10
                except Exception as exc:
                    import traceback as _tb
                    log.warning("WEB_IMG_%d render failed: %s\n%s", wi + 1, exc, _tb.format_exc())
            continue
        # ── Pull-quote / insight callout (from a markdown blockquote) ───────────
        if tp == "quote":
            text = _strip_inline(tok.get("text", ""))
            if not text:
                continue
            QLINE_H = 16
            wlines = _wrap(c, text, "Helvetica-Oblique", 11, CW - 52)
            box_h = len(wlines) * QLINE_H + 26
            need(box_h + 18, current_section[0])
            nl(12)
            top = y[0]
            bottom = top - box_h
            col = accent()
            # Warm cream card — deliberately distinct from the white chart/
            # stat cards so a callout reads as "stop and notice this", not
            # just another data card.
            c.setFillColorRGB(0.975, 0.965, 0.94)
            c.roundRect(MARGIN, bottom, CW, box_h, 5, fill=1, stroke=0)
            c.setFillColorRGB(*col)
            c.rect(MARGIN, bottom, 4, box_h, fill=1, stroke=0)
            c.setFont("Times-Bold", 30)
            c.drawString(MARGIN + 14, top - 24, "\u201C")
            ty2 = top - 20
            c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Oblique", 11)
            for ln in wlines:
                ty2 -= QLINE_H
                c.drawString(MARGIN + 42, ty2 + 4, ln)
            y[0] = bottom - 14
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
            if text_display.strip().lower() in ("data sources", "sources", "data source"):
                _reached_data_sources_heading = True
            else:
                _reached_data_sources_heading = False

            # Drop in an infographic stat-card strip right as we LEAVE a
            # section that turned out data-rich (a table, or 3+ bullets/
            # numbered points) — using the keyStats batches the cover page
            # had no room for — so cards show up where the data actually is,
            # not at two fixed headings every single report repeats.
            # section_idx was already bumped above for the section we're about
            # to render, so the section that just ENDED (the one the strip
            # represents) is one index back — look that colour up explicitly
            # rather than calling accent(), which would give the new section's
            # colour instead of the one the stat card is actually reporting on.
            # NOTE: this used to call stat_strip(...) immediately here, i.e.
            # on the OUTGOING section's page, right before the unconditional
            # page break below. When that outgoing page was already nearly
            # full, need() inside stat_strip pushed the strip onto a brand
            # new page by itself — then the forced h2 page-break a few lines
            # down immediately started ANOTHER new page, leaving the strip's
            # page with just 2-4 small cards and a wall of empty space below
            # (e.g. an "— Key Metrics" page with two stat cards and nothing
            # else). The strip is now deferred and drawn just under the new
            # section's heading instead, so it always lands on a page that
            # already has real content following it.
            _prev_accent = SECTION_ACCENTS[(section_idx[0] - 1) % len(SECTION_ACCENTS)]
            _section_was_data_rich = _sec_signal["tables"] >= 1 or _sec_signal["bullets"] >= 3
            _pending_strip = None
            if (current_section[0] and _section_was_data_rich
                    and _stat_pool and _stat_strip_count[0] < _STAT_STRIP_MAX):
                _take = _pick_relevant_stats(_stat_pool, _sec_text_buf[0])
                if _take:
                    _pending_strip = (_take, f"{current_section[0][:44]} — Key Metrics", _prev_accent)
                    _stat_strip_count[0] += 1
            elif ("conclusion" in text_display.lower() and _stat_pool
                    and _stat_strip_count[0] < _STAT_STRIP_MAX):
                # Safety net: make sure any leftover stats still surface
                # somewhere rather than silently disappearing.
                _take = _stat_pool[:4]
                del _stat_pool[:4]
                _pending_strip = (_take, "At a Glance", _prev_accent)
                _stat_strip_count[0] += 1
            _sec_signal["bullets"] = 0
            _sec_signal["tables"] = 0
            _sec_text_buf[0] = ""

            current_section[0] = text
            # Every major (H2) section heading now ALWAYS starts at the very
            # top of a fresh page — never mid-page, never near the bottom.
            # A soft "is there enough room?" check (the old need(190, ...))
            # still let a heading land mid-page whenever ~190pt happened to
            # be free, which is exactly what produced headings sitting in
            # the middle of a page with unrelated content above them, or
            # (when the guess was too small) squeezed near the bottom with
            # its body pushed to the next page. Forcing an unconditional
            # page break here — unless we're already at the top of a blank
            # page, i.e. this is the very first section right after the
            # intro page — guarantees "SECTION 0N" + its title is always the
            # first thing a reader sees on its page.
            if abs(y[0] - BODY_TOP) > 0.5:
                c.showPage(); hf(text); y[0] = BODY_TOP
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
            nl(20)
            # Any stat strip carried over from the section that just ended
            # (see note above) renders here — top of the new page, directly
            # under its heading — instead of dangling alone on the previous,
            # now-empty page.
            if _pending_strip:
                _take, _label, _color = _pending_strip
                stat_strip(_take, _label, strip_color=_color)
            continue

        # ── H3 — sub-section ─────────────────────────────────────────────────
        if tp == "h3":
            text = _strip_inline(tok["text"])
            # Was need(80, ...) — same "orphaned heading" risk as h2 above,
            # just smaller scale: bump the guaranteed post-banner slack.
            need(120, current_section[0])
            nl(10)   # visible gap before sub-section
            # Section-accent-tinted background (was a fixed mid-navy block for
            # every section) — each section now reads as visually its own.
            c.setFillColorRGB(*_mix(accent(), NAVY, 0.35))
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
            _sec_signal["bullets"] += 1
            _sec_text_buf[0] += " " + plain_text.lower()
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
            _sec_signal["bullets"] += 1  # numbered items count toward "data-rich" same as bullets
            _sec_text_buf[0] += " " + plain_text.lower()
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
            _sec_text_buf[0] += " " + plain_text.lower()
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
            _sec_signal["tables"] += 1
            _sec_text_buf[0] += " " + " ".join(str(cell) for r in rows for cell in r).lower()
            col_count = max(len(r) for r in rows)

            # Column widths sized to each column's actual content (header or
            # any cell), not a blind even split — an even split starves a
            # long free-text column (e.g. "Data Type" in the Data Sources
            # table) down to ~30 chars, forcing a hard single-line ellipsis
            # truncation even though the table has plenty of vertical room
            # to just wrap onto a second line instead.
            def _md_col_weight(ci: int) -> float:
                longest = 6
                for r in rows:
                    if ci < len(r):
                        longest = max(longest, len(_strip_inline(str(r[ci]))))
                return min(longest, 60)
            weights = [_md_col_weight(ci) for ci in range(col_count)]
            total_wt = sum(weights) or col_count
            min_col_w = 60.0
            col_ws = [max(min_col_w, CW * (wt / total_wt)) for wt in weights]
            scale = CW / sum(col_ws)
            col_ws = [cw * scale for cw in col_ws]
            col_x = [MARGIN]
            for cw in col_ws[:-1]:
                col_x.append(col_x[-1] + cw)

            LINE_H = 11
            for ri, row in enumerate(rows):
                if ri == 0:
                    tfont, tsize, tcol = "Helvetica-Bold", 7.5, WHITE
                else:
                    tfont, tsize, tcol = "Helvetica", 8.5, BODY_TXT

                # Pre-wrap every cell in this row using its column's real
                # width so the row is drawn at the height it actually needs
                # (up to 3 lines) instead of a fixed single-line ROW_H.
                cell_strs = []
                for ci in range(col_count):
                    cell = row[ci] if ci < len(row) else ""
                    cell_str = str(cell)
                    if ri > 0 and ci > 0:
                        cell_str = fmt_inr(cell_str)
                    cell_strs.append(_strip_inline(cell_str))
                wrapped_cells = [
                    _wrap_to_width(c, cell_strs[ci], tfont, tsize, col_ws[ci] - 8, max_lines=3)
                    for ci in range(col_count)
                ]
                n_lines = max(len(wc) for wc in wrapped_cells)
                ROW_H = max(15, n_lines * LINE_H + 6)
                need(ROW_H + 2, current_section[0])

                if ri == 0:
                    c.setFillColorRGB(*_mix(accent(), NAVY, 0.4))
                    c.rect(MARGIN, y[0] - ROW_H + 4, CW, ROW_H, fill=1, stroke=0)
                elif ri % 2 == 0:
                    c.setFillColorRGB(0.96, 0.97, 1.0)
                    c.rect(MARGIN, y[0] - ROW_H + 4, CW, ROW_H, fill=1, stroke=0)

                c.setStrokeColorRGB(0.87, 0.9, 0.94); c.setLineWidth(0.4)
                c.rect(MARGIN, y[0] - ROW_H + 4, CW, ROW_H, fill=0, stroke=1)

                for ci, lines in enumerate(wrapped_cells):
                    cx2 = col_x[ci] + 4
                    # Gain/loss cells (e.g. "+6.4%", "-9.56%") get green/red ink so
                    # the table reads at a glance, same as the chart bars do.
                    cell_color = _signed_cell_color(" ".join(lines)) if ri > 0 else None
                    c.setFillColorRGB(*(cell_color or tcol)); c.setFont(tfont, tsize)
                    ty2 = y[0] - ROW_H + 4 + ROW_H - LINE_H + 1
                    for line in lines:
                        c.drawString(cx2, ty2, line)
                        ty2 -= LINE_H
                nl(ROW_H)
            nl(8)
            if _reached_data_sources_heading:
                # This table is the Data Sources table — it's the last thing
                # allowed to render. See the module-level note where
                # _past_data_sources is declared.
                _past_data_sources[0] = True

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

    # Defense-in-depth: routes/report.py's failure paths (all keys exhausted,
    # no sources for a live-data question, unparseable model response, etc.)
    # now return a non-2xx status so the frontend's `!r.ok` check catches
    # them before ever calling this endpoint — but a stale frontend build,
    # a direct API caller, or the email-report route could still forward one
    # of those sentinel error strings as if it were real report content.
    # Building a full branded PDF around "All LLM keys exhausted or
    # rate-limited. Try again in a minute." is exactly what produced the
    # broken-looking "Research Intelligence" PDF seen in production — reject
    # it here too instead of rendering it.
    _known_failure_messages = (
        "All LLM keys exhausted or rate-limited. Try again in a minute.",
        "Could not retrieve data for this topic. Please try again.",
        "Invalid request body.",
    )
    if report.strip() in _known_failure_messages or report.strip().startswith("## Report Generation Error"):
        log.warning("PDF: refusing to render known report-generation-failure sentinel as a PDF")
        return JSONResponse(
            {"error": "The report failed to generate, so there's nothing to export yet. Please try generating the report again."},
            status_code=422,
        )

    title: str      = body.get("title", "")
    question: str   = body.get("question", "Research Report")
    summary: str    = body.get("summary", "")
    key_stats: list = body.get("keyStats", [])
    charts: list    = body.get("charts", [])
    logo_b64: str   = body.get("logoB64", "")
    file_images: list = body.get("fileImages", [])  # [{name, mimeType, data}]
    images: list    = body.get("images", [])  # [{url, caption}] from report generation
    theme: dict | None = body.get("theme") if isinstance(body.get("theme"), dict) else None

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
                if not theme and isinstance(inner.get("theme"), dict):
                    theme = inner.get("theme")
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
            # AI-generated images (see report.py's _generate_ai_report_images) arrive
            # as inline data URIs, not fetchable HTTP links — decode directly and skip
            # the network round-trip entirely.
            if url.startswith("data:image"):
                try:
                    header, _, b64_payload = url.partition(",")
                    if not b64_payload:
                        log.warning("AI_IMG[%d] malformed data URI", i + 1)
                        return
                    content_bytes = base64.b64decode(b64_payload)
                    if len(content_bytes) > max_bytes or len(content_bytes) < 500:
                        log.debug("AI_IMG[%d] size %d out of range", i + 1, len(content_bytes))
                        return
                    out[i] = {"data": b64_payload, "caption": _truncate_words(info.get("caption") or "", 120)}
                    log.info("AI_IMG[%d] decoded %d bytes from inline data URI", i + 1, len(content_bytes))
                except Exception as exc:
                    log.warning("AI_IMG[%d] data URI decode failed: %s", i + 1, exc)
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
                              "caption": _truncate_words(info.get("caption") or "", 120)}
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
        pdf_bytes = build_pdf(report, title, question, summary, key_stats, charts, logo_b64, file_images, web_images, theme)
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
