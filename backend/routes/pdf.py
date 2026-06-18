"""
POST /api/chat/report/pdf  — Generate branded PDF (ReportLab, pure Python)
Body: { report, title, charts, question, keyStats, summary }

Report structure rendered:
  Page 1  — Title Page (cover)
  Page 2  — Introduction
  Page 3  — Data Sources & Methodology
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

router = APIRouter()
log = logging.getLogger("pdf")

# ─── Colour palette ───────────────────────────────────────────────────────────
NAVY   = (26/255,  31/255,  78/255)
GOLD   = (200/255, 134/255, 10/255)
BLUE   = (59/255,  130/255, 246/255)
GREEN  = (34/255,  197/255, 94/255)
RED    = (239/255, 68/255,  68/255)
AMBER  = (245/255, 158/255, 11/255)
PURPLE = (139/255, 92/255,  246/255)
CYAN   = (6/255,   182/255, 212/255)
PINK   = (236/255, 72/255,  153/255)
WHITE  = (1.0, 1.0, 1.0)
LIGHT  = (240/255, 243/255, 255/255)
GREY   = (139/255, 147/255, 181/255)
BODY_TXT = (0.18, 0.21, 0.38)

CHART_COLORS = [BLUE, GREEN, AMBER, RED, PURPLE, CYAN, PINK, NAVY]

# Section accent colours (one per major section heading)
SECTION_ACCENTS = [GOLD, BLUE, GREEN, AMBER, RED, PURPLE]


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
        except Exception:
            pass

    # 2. Environment variable (set once at deploy time)
    b64 = os.environ.get("LOGO_B64", "")
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception:
            pass

    # 2b. Explicit path override via env var (most reliable for non-monorepo deploys)
    logo_path_env = os.environ.get("LOGO_PATH", "")
    if logo_path_env:
        p = Path(logo_path_env)
        if p.exists():
            try:
                return p.read_bytes()
            except Exception:
                pass

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



def detect_domain(question: str) -> str:
    q = question.lower()
    if re.search(r"\b(stock|share|nse|bse|sensex|nifty|sebi|ipo|equity|mutual fund|etf|trading|portfolio|invest)\b", q):
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
    return "Research Intelligence"


# ─── Safe text — strip chars Helvetica can't render ──────────────────────────
def _safe_text(text: str) -> str:
    """Replace characters that Helvetica can't render (shows as ■ tofu)."""
    return (text
        .replace("₹", "Rs.")
        .replace("\u20b9", "Rs.")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "--")
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

        elif re.match(r"^\[PAGE_IMG_\d+\]", stripped):
            m = re.match(r"^\[PAGE_IMG_(\d+)\]", stripped)
            yield {"type": "page_img_placeholder", "index": int(m.group(1)) - 1}

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
                yield {"type": "table", "rows": rows}
            continue

        elif re.match(r"^\s*[-*+]\s+", stripped):
            yield {"type": "bullet", "text": re.sub(r"^\s*[-*+]\s+", "", stripped)}

        elif re.match(r"^\d+\.\s+", stripped):
            m = re.match(r"^(\d+)\.\s+(.*)", stripped)
            yield {"type": "numbered", "num": m.group(1), "text": m.group(2)}

        elif stripped:
            yield {"type": "para", "text": stripped}

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


# ─── Chart renderers ──────────────────────────────────────────────────────────
def _bar(c, spec, x0, y0, w, h):
    series_list = spec.get("series") or [{}]
    data = series_list[0].get("data") or []
    if not data:
        return
    unit  = spec.get("unit", "")
    n_ser = len(series_list)

    # All values across all series for unified Y scale
    all_vals = [abs(_coerce_value(d.get("value", 0))) for s in series_list for d in s.get("data", [])]
    max_v    = max(all_vals) if all_vals else 1

    has_legend = n_ser > 1
    LEGEND_H   = 16 if has_legend else 0
    PL, PB     = 52, 32
    pw         = w - PL - 12
    ph         = h - PB - 24 - LEGEND_H
    n          = max(len(data), 1)
    sp         = pw / n
    # Per-series bar width — grouped layout
    bw_total   = min(sp * 0.78, 70.0)
    bw         = max(4, bw_total / n_ser)

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

    # X-axis labels — full text, no truncation
    for i, d in enumerate(data):
        lbl = d.get("label", "")
        bx  = x0 + PL + i * sp + sp / 2
        c.setFillColorRGB(*GREY)
        if len(lbl) > 8:
            c.saveState()
            c.translate(bx, y0 + PB - 4)
            c.rotate(30)
            c.setFont("Helvetica", 6)
            c.drawString(0, 0, lbl[:18])
            c.restoreState()
        else:
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(bx, y0 + PB - 11, lbl[:12])

    # Legend for multi-series
    if has_legend:
        lx = x0 + PL
        ly = y0 + PB + ph + 14
        for si, ser in enumerate(series_list):
            color = CHART_COLORS[si % len(CHART_COLORS)]
            sname = ser.get("name", f"Series {si+1}")[:22]
            c.setFillColorRGB(*color)
            c.rect(lx, ly - 4, 12, 6, fill=1, stroke=0)
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 7)
            c.drawString(lx + 15, ly - 2, sname)
            lx += 15 + c.stringWidth(sname, "Helvetica", 7) + 22


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
    PL, PB     = 52, 32
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

    # Legend for multi-series
    if has_legend:
        lx = x0 + PL
        ly = y0 + PB + ph + 14
        for si, s in enumerate(series):
            color = CHART_COLORS[si % len(CHART_COLORS)]
            sname = s.get("name", f"Series {si+1}")[:20]
            c.setFillColorRGB(*color)
            c.rect(lx, ly - 4, 12, 6, fill=1, stroke=0)
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 7)
            c.drawString(lx + 15, ly - 2, sname)
            lx += 15 + c.stringWidth(sname, "Helvetica", 7) + 20


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
    for i, d in enumerate(data):
        color = CHART_COLORS[i % len(CHART_COLORS)]
        pct = abs(_coerce_value(d.get("value", 0))) / total * 100
        lbl = d.get("label", "")[:22]
        iy = ly - i * 15
        c.setFillColorRGB(*color); c.rect(lx, iy - 5, 8, 8, fill=1, stroke=0)
        c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 7)
        c.drawString(lx + 12, iy - 1, lbl)
        c.setFont("Helvetica-Bold", 7)
        c.drawRightString(lx + 12 + 110, iy - 1, f"{pct:.1f}%")


def _draw_chart(c, spec, x0, y0, w, h):
    t = spec.get("type", "bar")
    if t == "pie":
        _pie(c, spec, x0, y0, w, h)
    elif t == "line":
        _line(c, spec, x0, y0, w, h)
    else:
        _bar(c, spec, x0, y0, w, h)


# ─── PDF builder ──────────────────────────────────────────────────────────────
def build_pdf(report: str, title: str, question: str, summary: str,
              key_stats: list, charts: list, logo_b64: str = "",
              file_images: list | None = None) -> bytes:
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
        except Exception:
            pass

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image as RLImage
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
        # Footer — minimal: just page number
        c.setFillColorRGB(0.94, 0.95, 1.0)
        c.rect(0, 0, PAGE_W, FOOTER_H, fill=1, stroke=0)
        c.setStrokeColorRGB(*NAVY); c.setLineWidth(1.2)
        c.line(0, FOOTER_H, PAGE_W, FOOTER_H)
        c.setFillColorRGB(*GREY); c.setFont("Helvetica", 7)
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
    # PAGE 1 — TITLE PAGE (light background)
    # ═══════════════════════════════════════════════════════════════════════════
    # White background
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    # Gold accent bar at top
    c.setFillColorRGB(*GOLD)
    c.rect(0, PAGE_H - 8, PAGE_W, 8, fill=1, stroke=0)
    # Thin navy rule under header area
    c.setStrokeColorRGB(*NAVY); c.setLineWidth(1)
    c.line(MARGIN, PAGE_H - 100, PAGE_W - MARGIN, PAGE_H - 100)

    # Logo — natural size on white background
    logo_y = PAGE_H - 85
    if logo_bytes:
        if not _draw_logo(c, logo_bytes, MARGIN, logo_y, 150, 46):
            c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 16)
            c.drawString(MARGIN, logo_y + 14, "Growth Gradual")
    else:
        c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 16)
        c.drawString(MARGIN, logo_y + 14, "Growth Gradual")

    # Domain tag
    tag_y = PAGE_H - 150
    tag = f"Research Report  ·  {domain}"
    tw = c.stringWidth(tag, "Helvetica-Bold", 8) + 20
    c.setFillColorRGB(0.96, 0.91, 0.80)
    c.roundRect(MARGIN, tag_y - 4, tw, 18, 3, fill=1, stroke=0)
    c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN + 10, tag_y + 2, tag.upper())

    # Report title — clean, single-line headline
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
    )
    if any(re.match(p, raw_title.strip(), re.IGNORECASE) for p in _PREAMBLE_PATTERNS):
        raw_title = f"{domain} Briefing"
    report_title = _strip_inline(raw_title)
    # Single-line title — truncate with ellipsis if it doesn't fit at a readable size
    title_size = 24
    while title_size > 14 and c.stringWidth(report_title, "Helvetica-Bold", title_size) > CW:
        title_size -= 1
    while c.stringWidth(report_title, "Helvetica-Bold", title_size) > CW and len(report_title) > 10:
        report_title = report_title[:-1]
    if c.stringWidth(report_title, "Helvetica-Bold", title_size) > CW:
        report_title = report_title.rstrip() 
        while c.stringWidth(report_title + "...", "Helvetica-Bold", title_size) > CW and len(report_title) > 10:
            report_title = report_title[:-1]
        report_title = report_title.rstrip() + "..."

    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", title_size)
    ty = tag_y - 36
    c.drawString(MARGIN, ty, report_title)

    # Gold rule
    ty -= 22
    c.setStrokeColorRGB(*GOLD); c.setLineWidth(2)
    c.line(MARGIN, ty, MARGIN + CW * 0.55, ty); ty -= 18

    # Summary block with subtle background
    if summary:
        s_lines = _wrap(c, summary, "Helvetica", 11, CW - 30)
        summary_h = len(s_lines[:6]) * 16 + 22
        c.setFillColorRGB(0.97, 0.97, 1.0)
        c.roundRect(MARGIN, ty - summary_h + 10, CW, summary_h, 4, fill=1, stroke=0)
        c.setStrokeColorRGB(0.88, 0.90, 0.96)
        c.roundRect(MARGIN, ty - summary_h + 10, CW, summary_h, 4, fill=0, stroke=1)
        c.setFillColorRGB(*GOLD); c.rect(MARGIN, ty - summary_h + 10, 4, summary_h, fill=1, stroke=0)
        c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 11)
        ty_s = ty - 6
        for ln in s_lines[:6]:
            c.drawString(MARGIN + 14, ty_s, ln); ty_s -= 16
        ty = ty_s - 14

    # ── Thin gold rule separator ──────────────────────────────────────────────
    ty -= 16
    c.setStrokeColorRGB(*GOLD); c.setLineWidth(0.8)
    c.line(MARGIN, ty, MARGIN + CW * 0.40, ty)
    ty -= 26

    # ── Two-column info panel ─────────────────────────────────────────────────
    PANEL_H = 110
    COL2    = (CW - 14) / 2

    # Left panel — navy background
    c.setFillColorRGB(*NAVY)
    c.roundRect(MARGIN, ty - PANEL_H, COL2, PANEL_H, 5, fill=1, stroke=0)
    c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN + 12, ty - 14, "ABOUT THIS REPORT")
    c.setStrokeColorRGB(*GOLD); c.setLineWidth(0.8)
    c.line(MARGIN + 12, ty - 18, MARGIN + COL2 - 12, ty - 18)
    meta_items = [
        ("DOMAIN",          domain),
        ("GENERATED",       date_str),
        ("CLASSIFICATION",  "Research Intelligence"),
        ("PUBLISHER",       "Growth Gradual"),
    ]
    my = ty - 32
    for lbl, val in meta_items:
        c.setFillColorRGB(0.55, 0.62, 0.84); c.setFont("Helvetica", 6.5)
        c.drawString(MARGIN + 12, my, lbl)
        c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 7.5)
        c.drawString(MARGIN + 12 + 76, my, _safe_text(val)[:30])
        my -= 16

    # Right panel — light background
    rx = MARGIN + COL2 + 14
    c.setFillColorRGB(0.97, 0.97, 1.0)
    c.roundRect(rx, ty - PANEL_H, COL2, PANEL_H, 5, fill=1, stroke=0)
    c.setStrokeColorRGB(0.84, 0.87, 0.95)
    c.roundRect(rx, ty - PANEL_H, COL2, PANEL_H, 5, fill=0, stroke=1)

    if key_stats:
        c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 7.5)
        c.drawString(rx + 12, ty - 14, "KEY METRICS")
        c.setStrokeColorRGB(*GOLD); c.setLineWidth(0.8)
        c.line(rx + 12, ty - 18, rx + COL2 - 12, ty - 18)
        ky = ty - 32
        for st in key_stats[:4]:
            val = _safe_text(st.get("value", ""))[:12]
            lbl = _safe_text(st.get("label", ""))[:24]
            chg = _safe_text(st.get("change", ""))
            col_c = GREEN if chg.startswith("+") else (RED if chg.startswith("-") else GREY)
            c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 8.5)
            c.drawString(rx + 12, ky, val)
            c.setFillColorRGB(*GREY); c.setFont("Helvetica", 7)
            vw = c.stringWidth(val, "Helvetica-Bold", 8.5)
            c.drawString(rx + 12 + vw + 5, ky, lbl)
            if chg:
                c.setFillColorRGB(*col_c); c.setFont("Helvetica-Bold", 7)
                c.drawRightString(rx + COL2 - 12, ky, chg)
            ky -= 16
    else:
        c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 7.5)
        c.drawString(rx + 12, ty - 14, "INTELLIGENCE BRIEF")
        c.setStrokeColorRGB(*GOLD); c.setLineWidth(0.8)
        c.line(rx + 12, ty - 18, rx + COL2 - 12, ty - 18)
        taglines = [
            "AI-powered financial research",
            "Curated from 60+ trusted sources",
            "Real-time market intelligence",
            "Institutional-grade analysis",
        ]
        tly = ty - 34
        for tl in taglines:
            c.setFillColorRGB(*GOLD); c.rect(rx + 12, tly + 1, 3, 7, fill=1, stroke=0)
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 8)
            c.drawString(rx + 20, tly, tl)
            tly -= 16

    ty -= PANEL_H + 20

    # ── Key stats cards row ───────────────────────────────────────────────────
    if key_stats:
        stats    = key_stats[:4]
        n_cards  = len(stats)
        gap      = 8
        card_w   = (CW - gap * (n_cards - 1)) / n_cards
        CARD_H   = 62
        BAR_H    = 4

        for i, st in enumerate(stats):
            sx       = MARGIN + i * (card_w + gap)
            card_bot = ty - CARD_H
            card_top = ty

            c.setFillColorRGB(*LIGHT)
            c.roundRect(sx, card_bot, card_w, CARD_H, 4, fill=1, stroke=0)
            c.setStrokeColorRGB(0.84, 0.87, 0.94)
            c.roundRect(sx, card_bot, card_w, CARD_H, 4, fill=0, stroke=1)
            c.setFillColorRGB(*GOLD)
            c.rect(sx, card_top - BAR_H, card_w, BAR_H, fill=1, stroke=0)

            label    = _safe_text(st.get("label", "")).upper()
            lbl_w    = card_w - 16
            lbl_lines = _wrap(c, label, "Helvetica", 6.5, lbl_w)
            c.setFillColorRGB(*GREY); c.setFont("Helvetica", 6.5)
            lbl_top = card_top - BAR_H - 9
            for li, ll in enumerate(lbl_lines[:2]):
                c.drawString(sx + 8, lbl_top - li * 9, ll)

            value    = _safe_text(st.get("value", ""))[:14]
            val_font = 13 if len(value) <= 9 else 10
            val_y    = lbl_top - len(lbl_lines[:2]) * 9 - 11
            c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", val_font)
            c.drawString(sx + 8, val_y, value)

            chg = _safe_text(st.get("change", ""))
            if chg:
                col_c = GREEN if chg.startswith("+") else (RED if chg.startswith("-") else GREY)
                c.setFillColorRGB(*col_c); c.setFont("Helvetica", 7)
                c.drawString(sx + 8, val_y - val_font - 3, chg[:12])

        ty -= CARD_H + 18

    # ── Table of contents ─────────────────────────────────────────────────────
    section_headings = []
    for _tok in _tokenise(report):
        if _tok["type"] == "h2":
            section_headings.append(_strip_inline(_tok["text"]))
        if len(section_headings) >= 6:
            break

    bottom_strip_top = 68
    available = ty - bottom_strip_top - 16
    if available >= 70 and section_headings:
        c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 7.5)
        c.drawString(MARGIN, ty, "CONTENTS")
        lw = c.stringWidth("CONTENTS", "Helvetica-Bold", 7.5)
        c.setStrokeColorRGB(*GOLD); c.setLineWidth(1)
        c.line(MARGIN, ty - 4, MARGIN + lw, ty - 4)

        toc_y  = ty - 18
        col_w2 = (CW - 12) / 2
        col_idx = 0
        for i, heading in enumerate(section_headings):
            if toc_y < bottom_strip_top + 16:
                break
            tx = MARGIN + col_idx * (col_w2 + 12)
            c.setFillColorRGB(*NAVY)
            c.roundRect(tx, toc_y - 2, 14, 12, 2, fill=1, stroke=0)
            c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 7)
            c.drawCentredString(tx + 7, toc_y + 1, str(i + 1))
            c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 8)
            c.drawString(tx + 18, toc_y, heading[:40])
            name_w = c.stringWidth(heading[:40], "Helvetica", 8)
            dot_x  = tx + 18 + name_w + 4
            dot_end = tx + col_w2 - 4
            c.setStrokeColorRGB(0.82, 0.85, 0.92); c.setLineWidth(0.4)
            c.setDash(1, 3)
            c.line(dot_x, toc_y + 4, dot_end, toc_y + 4)
            c.setDash()
            col_idx += 1
            if col_idx >= 2:
                col_idx = 0
                toc_y -= 16
        ty = toc_y - 10

    # ── Thin rule above footer ────────────────────────────────────────────────
    c.setStrokeColorRGB(0.82, 0.86, 0.94); c.setLineWidth(0.5)
    c.line(MARGIN, bottom_strip_top + 2, PAGE_W - MARGIN, bottom_strip_top + 2)

    # ── Bottom footer strip ───────────────────────────────────────────────────
    c.setFillColorRGB(*NAVY)
    c.rect(0, 0, PAGE_W, bottom_strip_top, fill=1, stroke=0)
    c.setStrokeColorRGB(*GOLD); c.setLineWidth(1.2)
    c.line(0, bottom_strip_top, PAGE_W, bottom_strip_top)
    c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 7)
    c.setFillColorRGB(0.60, 0.65, 0.85); c.setFont("Helvetica", 6.5)
    c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 9)
    c.drawRightString(PAGE_W - MARGIN, 44, "growth-gradual.com")
    c.setFillColorRGB(*GOLD); c.setFont("Helvetica", 7)
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

    # ── Token renderer ─────────────────────────────────────────────────────────
    tokens = list(_tokenise(report))
    chart_idx = [0]   # next chart to render
    rendered_charts: set[int] = set()  # indices of charts already rendered inline

    for tok in tokens:
        tp = tok["type"]

        # ── Chart placeholder ────────────────────────────────────────────────
        if tp == "chart_placeholder":
            ci = tok.get("index", chart_idx[0])
            chart_idx[0] = ci + 1
            if ci < len(charts) and _is_valid_chart(charts[ci]):
                ch = charts[ci]
                CHART_H = 195
                CARD_HEADER = 30   # space for title bar inside the card
                GAP_ABOVE = 14     # breathing room between previous content and this card
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

                # Title bar — sits inside the card, near its top
                title_y = card_top - 18
                c.setFillColorRGB(*accent())
                c.rect(MARGIN, title_y - 2, 4, 14, fill=1, stroke=0)
                c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 9)
                c.drawString(MARGIN + 10, title_y, ch.get("title", "")[:80])
                c.setStrokeColorRGB(0.9, 0.92, 0.96); c.setLineWidth(0.6)
                c.line(MARGIN + 8, title_y - 6, MARGIN + CW - 8, title_y - 6)

                # Chart body — fills remaining card area below the title bar
                chart_top = title_y - 10
                _draw_chart(c, ch, MARGIN + 8, chart_top - CHART_H, CW - 16, CHART_H)
                y[0] = card_bottom - 14   # gap below the card before next content
                rendered_charts.add(ci)
            continue

        # ── Page image placeholder (from uploaded file) ───────────────────────
        if tp == "page_img_placeholder":
            imgs = file_images or []
            pi = tok.get("index", 0)
            if pi < len(imgs):
                img_info = imgs[pi]
                try:
                    import base64 as _b64
                    import io as _io
                    img_data = _b64.b64decode(img_info["data"])
                    pil_img = PILImage.open(_io.BytesIO(img_data))
                    # Scale to fit content width, max height 340pt
                    MAX_IMG_W = CW
                    MAX_IMG_H = 340
                    ow, oh = pil_img.size
                    scale = min(MAX_IMG_W / ow, MAX_IMG_H / oh, 1.0)
                    iw, ih = ow * scale, oh * scale
                    need(ih + 32, current_section[0])
                    nl(10)
                    # Caption bar above image
                    cap = img_info.get("name", f"Page {pi+1}")[:80]
                    cx = MARGIN + (CW - iw) / 2
                    c.setFillColorRGB(0.95, 0.96, 0.99)
                    c.roundRect(MARGIN, y[0] - ih - 26, CW, ih + 26, 4, fill=1, stroke=0)
                    c.setStrokeColorRGB(0.87, 0.9, 0.95)
                    c.roundRect(MARGIN, y[0] - ih - 26, CW, ih + 26, 4, fill=0, stroke=1)
                    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 8)
                    c.drawString(MARGIN + 8, y[0] - 14, f"📄 {cap}")
                    # Draw image
                    img_buf = _io.BytesIO()
                    pil_img.save(img_buf, format="JPEG", quality=85)
                    img_buf.seek(0)
                    rl_img = RLImage(img_buf, width=iw, height=ih)
                    rl_img.drawOn(c, cx, y[0] - ih - 22)
                    y[0] = y[0] - ih - 36
                except Exception as exc:
                    log.warning("PAGE_IMG_%d render failed: %s", pi + 1, exc)
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
            current_section[0] = text
            # 10(gap above) + 28(banner) + 34(nl after) + 40(min content below) = 112
            need(112, text)
            nl(14)   # visible gap before section banner
            col = accent()
            # Full-width section banner
            c.setFillColorRGB(*NAVY)
            c.rect(MARGIN, y[0] - 8, CW, 28, fill=1, stroke=0)
            # Left accent bar in section colour
            c.setFillColorRGB(*col)
            c.rect(MARGIN, y[0] - 8, 5, 28, fill=1, stroke=0)
            c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 12)
            c.drawString(MARGIN + 14, y[0] + 6, text[:80])
            nl(38); continue   # extra gap after banner before content starts

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
            text = _strip_inline(tok["text"])
            wlines = _wrap(c, text, "Helvetica", 10, CW - 16)
            for li, ln in enumerate(wlines):
                need(15, current_section[0])
                if li == 0:
                    c.setFillColorRGB(*accent())
                    c.circle(MARGIN + 5, y[0] + 3.5, 2.5, fill=1, stroke=0)
                c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 10)
                c.drawString(MARGIN + 15, y[0], ln)
                nl(15)
            continue

        # ── Numbered item ────────────────────────────────────────────────────
        if tp == "numbered":
            text = _strip_inline(tok["text"])
            num = tok.get("num", "•")
            indent = 18
            wlines = _wrap(c, text, "Helvetica", 10, CW - indent)
            for li, ln in enumerate(wlines):
                need(15, current_section[0])
                if li == 0:
                    c.setFillColorRGB(*accent()); c.setFont("Helvetica-Bold", 10)
                    c.drawString(MARGIN, y[0], f"{num}.")
                c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 10)
                c.drawString(MARGIN + indent, y[0], ln)
                nl(15)
            continue

        # ── Paragraph ────────────────────────────────────────────────────────
        if tp == "para":
            text = _strip_inline(tok["text"])
            wlines = _wrap(c, text, "Helvetica", 10, CW)
            for ln in wlines:
                need(15, current_section[0])
                c.setFillColorRGB(*BODY_TXT); c.setFont("Helvetica", 10)
                c.drawString(MARGIN, y[0], ln)
                nl(15)
            nl(6); continue

        # ── Table ─────────────────────────────────────────────────────────────
        if tp == "table":
            rows = tok["rows"]
            if not rows:
                continue
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
                    c.setFillColorRGB(*tcol); c.setFont(tfont, tsize)
                    c.drawString(cx2, y[0] - ROW_H + 7, _strip_inline(str(cell))[:28])
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
        except Exception:
            pass

    # Unescape any literal \n sequences
    if "\\n" in report:
        report = report.replace("\\n", "\n")

    # Strip accidental markdown fences
    report = re.sub(r"^```(?:json|markdown)?\s*", "", report.strip())
    report = re.sub(r"```\s*$", "", report).strip()

    log.info("PDF: generating — title=%r  charts=%d  keyStats=%d", title[:60], len(charts), len(key_stats))

    try:
        pdf_bytes = build_pdf(report, title, question, summary, key_stats, charts, logo_b64, file_images)
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
