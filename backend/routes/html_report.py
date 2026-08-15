"""
POST /api/chat/report/html — Generate an animated, interactive HTML report.

Body: { report, title, question, summary, keyStats, charts, images, logoB64 }
(identical shape to /api/chat/report/pdf's body — this is a second renderer
for the SAME structured report data, not a second report-generation step.)

Why this exists as a separate route rather than an option on the PDF one:
PDF (ReportLab, see routes/pdf.py) is a static-document format — there is no
such thing as an animated or interactive PDF element in that pipeline, by
construction of the format itself. When a request asks for "animated
images," "creative UI," or an "interactive" report, there is no amount of
prompting that makes ReportLab produce that; the deliverable format itself
has to change. This route renders the exact same report/title/charts/
keyStats/images payload as a single self-contained HTML document instead,
using CSS transitions/keyframes, an IntersectionObserver-driven scroll-reveal,
animated count-up stat cards, and real Chart.js charts (which can render
enter-animations, tooltips, and hover states — none of which a static PDF
chart can do).

Nothing about the report-generation step (routes/report.py) changes: the
LLM still writes the same markdown with the same [CHART_n]/[WEB_IMG_n]
placeholders and the same charts/keyStats/images arrays it always has. This
file only changes what those get turned INTO — same data, different (and in
this one dimension, genuinely more capable) renderer.
"""
import html
import json
import logging
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()
log = logging.getLogger("html_report")

# Same "known failure sentinel" guard as routes/pdf.py — don't build a
# polished animated report around an error message.
_KNOWN_FAILURE_MESSAGES = (
    "All LLM keys exhausted or rate-limited. Try again in a minute.",
    "Could not retrieve data for this topic. Please try again.",
    "Invalid request body.",
)

BRAND_NAVY = "#0f1a3c"
BRAND_NAVY_DEEP = "#0a1230"
BRAND_GOLD = "#d4a24c"
BRAND_GOLD_LIGHT = "#e8c27e"


# ─────────────────────────────────────────────────────────────────────────
# Markdown → HTML, resolving [CHART_n] / [WEB_IMG_n] placeholders inline
# ─────────────────────────────────────────────────────────────────────────

_CHART_RE = re.compile(r"^\[CHART_(\d+)\]$")
_WEBIMG_RE = re.compile(r"^\[WEB_IMG_(\d+)\]$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")


def _inline_md(text: str) -> str:
    """Bold/italic only — headings/lists/blockquotes are handled per-line
    by the block-level parser below, not here."""
    text = html.escape(text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def _to_num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _chart_type_js(chart_type: str) -> str:
    """Map our internal chart spec 'type' to a Chart.js chart type."""
    return {
        "bar": "bar",
        "line": "line",
        "pie": "doughnut",
        "scatter": "scatter",
        "arrow": "bar",  # rendered as a grouped before/after bar comparison
    }.get(chart_type, "bar")


def _render_chart_block(chart: dict, idx: int, theme: dict | None = None) -> str:
    ctype = chart.get("type", "bar")
    title = html.escape(chart.get("title") or "")
    x_label = html.escape(chart.get("xLabel") or "")
    y_label = html.escape(chart.get("yLabel") or "")

    if ctype == "table":
        columns = chart.get("columns") or []
        rows = chart.get("rows") or []
        if len(columns) < 2 or len(rows) < 2:
            log.warning("HTML report: skipping table chart '%s' — insufficient columns/rows", chart.get("title", "?"))
            return ""
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
        body_rows = []
        for r_i, row in enumerate(rows):
            cells = "".join(f"<td>{html.escape(str(c))}</td>" for c in row)
            body_rows.append(f'<tr style="--row-i:{r_i}">{cells}</tr>')
        body = "".join(body_rows)
        return f"""
<div class="gg-reveal gg-table-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-table-scroll">
    <table class="gg-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
  </div>
</div>"""

    canvas_id = f"gg-chart-{idx}"
    series = chart.get("series") or []
    theme = theme or {}
    theme_gold = theme.get("accentColor") or BRAND_GOLD
    theme_navy = theme.get("primaryColor") or BRAND_NAVY
    palette = [theme_gold, theme_navy, "#7fb3d5", "#c97b63", "#8e9aaf"]

    if ctype == "scatter":
        # Scatter shares report.py's "two-series-sharing-labels" shape
        # (series[0] = x-metric per label, series[1] = y-metric per label),
        # the same shape utils/datawrapper.py merges into (x, y) columns for
        # the PDF path. Chart.js's scatter type needs {x, y} point objects,
        # not the flat category-indexed arrays bar/line use — feeding it
        # those renders a chart with zero visible points.
        x_series = series[0] if len(series) > 0 else {}
        y_series = series[1] if len(series) > 1 else {}
        x_by_label = {str(pt.get("label", "")): pt.get("value", 0) for pt in (x_series.get("data") or [])}
        y_by_label = {str(pt.get("label", "")): pt.get("value", 0) for pt in (y_series.get("data") or [])}
        points = [
            {"x": _to_num(x_by_label[lbl]), "y": _to_num(y_by_label[lbl]), "label": lbl}
            for lbl in x_by_label
            if lbl in y_by_label
        ]
        if not points:
            log.warning("HTML report: skipping scatter chart '%s' — no matched (x,y) points", chart.get("title", "?"))
            return ""
        datasets = [{
            "label": f"{x_series.get('name', 'X')} vs {y_series.get('name', 'Y')}",
            "data": points,
            "backgroundColor": theme_gold,
            "borderColor": theme_gold,
            "pointRadius": 6,
            "pointHoverRadius": 8,
        }]
        chart_config = {
            "type": "scatter",
            "data": {"datasets": datasets},
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1400, "easing": "easeOutQuart"},
                "plugins": {
                    "legend": {"display": False},
                    "title": {"display": False},
                },
                "scales": {
                    "x": {"title": {"display": bool(x_label or x_series.get("name")), "text": x_label or x_series.get("name", ""), "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                    "y": {"title": {"display": bool(y_label or y_series.get("name")), "text": y_label or y_series.get("name", ""), "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                },
            },
        }
        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-chart-canvas-box"><canvas id="{canvas_id}"></canvas></div>
</div>
<script>
window.__ggCharts = window.__ggCharts || [];
window.__ggCharts.push({{ id: "{canvas_id}", config: {json.dumps(chart_config)} }});
</script>"""

    if ctype == "waterfall":
        # Running-total bridge (e.g. Opening AUM -> Inflows -> Redemptions ->
        # Closing AUM), rendered as a floating bar chart: each dataset entry
        # is a [bottom, top] pair (Chart.js's built-in "floating bar" shape),
        # so no extra plugin is needed beyond the vanilla bar controller
        # already loaded. A point with "isTotal": true is an anchor (resets
        # the running total to its own value); everything else is a delta
        # off the previous cumulative — same semantics as pdf.py's _waterfall.
        data_pts = (series[0].get("data") or []) if series else []
        if len(data_pts) < 1:
            log.warning("HTML report: skipping waterfall chart '%s' — no data points", chart.get("title", "?"))
            return ""
        wf_labels = [str(d.get("label", "")) for d in data_pts]
        ranges, colors = [], []
        cum = 0.0
        GREEN_HEX, RED_HEX = "#178a4c", "#c0392b"
        for d in data_pts:
            v = _to_num(d.get("value", 0))
            is_total = bool(d.get("isTotal"))
            if is_total:
                bottom, top = 0.0, v
                cum = v
            else:
                bottom, top = cum, cum + v
                cum = top
            ranges.append([min(bottom, top), max(bottom, top)])
            colors.append(theme_navy if is_total else (GREEN_HEX if v >= 0 else RED_HEX))
        chart_config = {
            "type": "bar",
            "data": {"labels": wf_labels, "datasets": [{
                "label": (series[0].get("name") if series else None) or "Value",
                "data": ranges,
                "backgroundColor": colors,
                "borderRadius": 3,
                "barPercentage": 0.6,
            }]},
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1400, "easing": "easeOutQuart"},
                "plugins": {"legend": {"display": False}, "title": {"display": False}},
                "scales": {
                    "x": {"title": {"display": bool(x_label), "text": x_label, "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                    "y": {"title": {"display": bool(y_label), "text": y_label, "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                },
            },
        }
        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-chart-canvas-box"><canvas id="{canvas_id}"></canvas></div>
</div>
<script>
window.__ggCharts = window.__ggCharts || [];
window.__ggCharts.push({{ id: "{canvas_id}", config: {json.dumps(chart_config)} }});
</script>"""

    if ctype == "candlestick":
        # OHLC candlestick, built from two overlaid floating-bar datasets on
        # the SAME category position (a thin one for the high/low wick, a
        # wide one for the open/close body) rather than a separate financial-
        # chart plugin/date-adapter — "grouped: false" on the x scale is what
        # makes Chart.js stack multiple bar datasets at the same x position
        # instead of placing them side by side.
        data_pts = (series[0].get("data") or []) if series else []
        if len(data_pts) < 2:
            log.warning("HTML report: skipping candlestick chart '%s' — need ≥2 sessions", chart.get("title", "?"))
            return ""
        cs_labels = [str(d.get("label", "")) for d in data_pts]
        wick_ranges, body_ranges, cs_colors = [], [], []
        GREEN_HEX, RED_HEX = "#178a4c", "#c0392b"
        for d in data_pts:
            o, hi, lo, cl = (_to_num(d.get(k, 0)) for k in ("open", "high", "low", "close"))
            wick_ranges.append([lo, hi])
            body_ranges.append([min(o, cl), max(o, cl)])
            cs_colors.append(GREEN_HEX if cl >= o else RED_HEX)
        chart_config = {
            "type": "bar",
            "data": {"labels": cs_labels, "datasets": [
                {"label": "Range", "data": wick_ranges, "backgroundColor": cs_colors,
                 "barPercentage": 0.12, "categoryPercentage": 0.8, "borderSkipped": False},
                {"label": "Open-Close", "data": body_ranges, "backgroundColor": cs_colors,
                 "barPercentage": 0.5, "categoryPercentage": 0.8, "borderSkipped": False},
            ]},
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1400, "easing": "easeOutQuart"},
                "plugins": {"legend": {"display": False}, "title": {"display": False}},
                "scales": {
                    "x": {"grouped": False,
                          "title": {"display": bool(x_label), "text": x_label, "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                    "y": {"title": {"display": bool(y_label), "text": y_label, "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                },
            },
        }
        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-chart-canvas-box"><canvas id="{canvas_id}"></canvas></div>
</div>
<script>
window.__ggCharts = window.__ggCharts || [];
window.__ggCharts.push({{ id: "{canvas_id}", config: {json.dumps(chart_config)} }});
</script>"""

    if ctype == "sparkline":
        # Minimal axis-less trend line — deliberately no gridlines/ticks, no
        # legend, and no axis titles even if the spec supplied xLabel/yLabel
        # (a sparkline's whole point is to be a quick inline cue, not a full
        # chart). Only the two endpoints get a visible point marker.
        data_pts = (series[0].get("data") or []) if series else []
        if len(data_pts) < 2:
            log.warning("HTML report: skipping sparkline chart '%s' — need ≥2 points", chart.get("title", "?"))
            return ""
        sp_labels = [str(d.get("label", "")) for d in data_pts]
        sp_vals = [_to_num(d.get("value", 0)) for d in data_pts]
        up = sp_vals[-1] >= sp_vals[0]
        color = "#178a4c" if up else "#c0392b"
        point_radius = [0] * len(sp_vals)
        point_radius[0] = point_radius[-1] = 4
        chart_config = {
            "type": "line",
            "data": {"labels": sp_labels, "datasets": [{
                "label": (series[0].get("name") if series else None) or "Value",
                "data": sp_vals,
                "borderColor": color,
                "backgroundColor": "transparent",
                "borderWidth": 2.5,
                "pointRadius": point_radius,
                "pointBackgroundColor": color,
                "tension": 0.35,
                "fill": False,
            }]},
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1200, "easing": "easeOutQuart"},
                "plugins": {"legend": {"display": False}, "title": {"display": False}},
                "scales": {
                    "x": {"display": False, "grid": {"display": False}},
                    "y": {"display": False, "grid": {"display": False}},
                },
            },
        }
        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-chart-canvas-box"><canvas id="{canvas_id}"></canvas></div>
</div>
<script>
window.__ggCharts = window.__ggCharts || [];
window.__ggCharts.push({{ id: "{canvas_id}", config: {json.dumps(chart_config)} }});
</script>"""

    labels: list[str] = []
    for s in series:
        for pt in (s.get("data") or []):
            lbl = str(pt.get("label", ""))
            if lbl not in labels:
                labels.append(lbl)

    datasets = []
    for s_i, s in enumerate(series):
        by_label = {str(pt.get("label", "")): pt.get("value", 0) for pt in (s.get("data") or [])}
        values = [by_label.get(lbl, 0) for lbl in labels]
        color = palette[s_i % len(palette)]
        datasets.append({
            "label": s.get("name", f"Series {s_i+1}"),
            "data": values,
            "backgroundColor": color if ctype != "line" else "transparent",
            "borderColor": color,
            "borderWidth": 2,
            "fill": False,
            "tension": 0.35,
        })

    # Belt-and-braces: charts/labels normally arrive pre-validated by
    # _is_plausible_chart in report.py, but if a chart ever reaches here
    # with no labels or every dataset value missing/zero, emitting a
    # canvas would just render an empty box — skip it instead.
    if not labels or not any(v not in (None, 0) for ds in datasets for v in ds["data"]):
        log.warning("HTML report: skipping chart '%s' — no plottable data points", chart.get("title", "?"))
        return ""

    chart_config = {
        "type": _chart_type_js(ctype),
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "animation": {"duration": 1400, "easing": "easeOutQuart"},
            "plugins": {
                "legend": {"display": len(datasets) > 1, "labels": {"color": "#e8e8f0"}},
                "title": {"display": False},
            },
            "scales": ({} if ctype == "pie" else {
                "x": {"title": {"display": bool(x_label), "text": x_label, "color": "#9aa3c0"},
                      "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                "y": {"title": {"display": bool(y_label), "text": y_label, "color": "#9aa3c0"},
                      "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
            }),
        },
    }

    return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-chart-canvas-box"><canvas id="{canvas_id}"></canvas></div>
</div>
<script>
window.__ggCharts = window.__ggCharts || [];
window.__ggCharts.push({{ id: "{canvas_id}", config: {json.dumps(chart_config)} }});
</script>"""


def _render_image_block(image: dict, idx: int) -> str:
    url = html.escape(str(image.get("url", "")))
    caption = html.escape(str(image.get("caption", "")))
    if not url:
        return ""
    return f"""
<figure class="gg-reveal gg-figure" data-reveal>
  <img src="{url}" alt="{caption}" loading="lazy" />
  {f'<figcaption>{caption}</figcaption>' if caption else ''}
</figure>"""


def _markdown_to_html(md: str, charts: list, images: list, theme: dict | None = None) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_mode: str | None = None  # "ul" | "ol" | None
    para_buf: list[str] = []
    reveal_counter = 0

    def flush_para():
        nonlocal para_buf, reveal_counter
        if para_buf:
            text = " ".join(para_buf).strip()
            if text:
                reveal_counter += 1
                out.append(f'<p class="gg-reveal" data-reveal style="--d:{(reveal_counter % 4) * 60}ms">{_inline_md(text)}</p>')
            para_buf = []

    def close_list():
        nonlocal list_mode
        if list_mode:
            out.append(f"</{list_mode}>")
            list_mode = None

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        chart_m = _CHART_RE.match(stripped)
        webimg_m = _WEBIMG_RE.match(stripped)

        if chart_m:
            flush_para(); close_list()
            n = int(chart_m.group(1))
            if 1 <= n <= len(charts):
                out.append(_render_chart_block(charts[n - 1], n, theme))
            continue

        if webimg_m:
            flush_para(); close_list()
            n = int(webimg_m.group(1))
            if 1 <= n <= len(images):
                out.append(_render_image_block(images[n - 1], n))
            continue

        if not stripped:
            flush_para(); close_list()
            continue

        if stripped.startswith("### "):
            flush_para(); close_list()
            out.append(f'<h3 class="gg-reveal" data-reveal>{_inline_md(stripped[4:])}</h3>')
            continue
        if stripped.startswith("## "):
            flush_para(); close_list()
            out.append(f'<h2 class="gg-reveal gg-section-h2" data-reveal>{_inline_md(stripped[3:])}</h2>')
            continue
        if stripped.startswith("# "):
            flush_para(); close_list()
            out.append(f'<h1 class="gg-reveal" data-reveal>{_inline_md(stripped[2:])}</h1>')
            continue

        if stripped.startswith("> "):
            flush_para(); close_list()
            out.append(f'<blockquote class="gg-reveal gg-pullquote" data-reveal>{_inline_md(stripped[2:])}</blockquote>')
            continue

        if stripped in ("---", "***", "___"):
            flush_para(); close_list()
            out.append('<hr class="gg-divider" />')
            continue

        bullet_m = re.match(r"^[-•*]\s+(.*)$", stripped)
        numbered_m = re.match(r"^\d+[.)]\s+(.*)$", stripped)

        if bullet_m:
            flush_para()
            if list_mode != "ul":
                close_list()
                out.append('<ul class="gg-reveal gg-list" data-reveal>')
                list_mode = "ul"
            out.append(f"<li>{_inline_md(bullet_m.group(1))}</li>")
            continue

        if numbered_m:
            flush_para()
            if list_mode != "ol":
                close_list()
                out.append('<ol class="gg-reveal gg-list" data-reveal>')
                list_mode = "ol"
            out.append(f"<li>{_inline_md(numbered_m.group(1))}</li>")
            continue

        close_list()
        para_buf.append(stripped)

    flush_para(); close_list()
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────
# Key stats — animated count-up cards
# ─────────────────────────────────────────────────────────────────────────

_NUMERIC_RE = re.compile(r"-?[\d,]+\.?\d*")


def _render_key_stats(key_stats: list) -> str:
    if not key_stats:
        return ""
    cards = []
    for i, stat in enumerate(key_stats[:12]):
        label = html.escape(str(stat.get("label", "")))
        value = str(stat.get("value", ""))
        change = str(stat.get("change", "") or "")
        m = _NUMERIC_RE.search(value)
        if m:
            numeric_str = m.group(0).replace(",", "")
            prefix = html.escape(value[: m.start()])
            suffix = html.escape(value[m.end():])
            try:
                target = float(numeric_str)
                is_float = "." in numeric_str
                data_attrs = f'data-count-target="{target}" data-count-decimals="{2 if is_float else 0}"'
                value_html = f'{prefix}<span class="gg-count" {data_attrs}>0</span>{suffix}'
            except ValueError:
                value_html = html.escape(value)
        else:
            value_html = html.escape(value)

        change_cls = ""
        if change.strip().startswith("-"):
            change_cls = "gg-change-down"
        elif change.strip().startswith("+"):
            change_cls = "gg-change-up"

        cards.append(f"""
<div class="gg-stat-card gg-reveal" data-reveal style="--d:{(i % 6) * 70}ms">
  <div class="gg-stat-value">{value_html}</div>
  <div class="gg-stat-label">{label}</div>
  {f'<div class="gg-stat-change {change_cls}">{html.escape(change)}</div>' if change.strip() else ''}
</div>""")
    return f'<div class="gg-stats-grid">{"".join(cards)}</div>'


# ─────────────────────────────────────────────────────────────────────────
# Full document
# ─────────────────────────────────────────────────────────────────────────

def _clamp255(v: int) -> int:
    return max(0, min(255, v))


def _shade_hex(hex_color: str, amount: float) -> str:
    """Lighten (amount > 0) or darken (amount < 0) a #rrggbb hex color by a
    fraction of the remaining distance to white/black. Used to derive the
    'light'/'deep' variants of a user-requested theme color the same way
    the fixed BRAND_GOLD_LIGHT / BRAND_NAVY_DEEP variants were hand-picked
    for the default palette."""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except (ValueError, IndexError):
        return hex_color
    if amount >= 0:
        r = _clamp255(int(r + (255 - r) * amount))
        g = _clamp255(int(g + (255 - g) * amount))
        b = _clamp255(int(b + (255 - b) * amount))
    else:
        r = _clamp255(int(r * (1 + amount)))
        g = _clamp255(int(g * (1 + amount)))
        b = _clamp255(int(b * (1 + amount)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _build_css(theme: dict | None) -> str:
    """Build the report stylesheet, substituting the requested theme's
    colors for the default navy/gold brand pair when the report asked for
    one (see report.py's THEME schema field). Falls back to the standard
    Growth Gradual palette whenever no theme, or an invalid one, was given."""
    theme = theme or {}
    navy = theme.get("primaryColor") or BRAND_NAVY
    gold = theme.get("accentColor") or BRAND_GOLD
    navy_deep = _shade_hex(navy, -0.35)
    gold_light = _shade_hex(gold, 0.35)
    return _CSS_TEMPLATE.format(navy=navy, navy_deep=navy_deep, gold=gold, gold_light=gold_light)


_CSS_TEMPLATE = """
:root {{
  --navy: {navy}; --navy-deep: {navy_deep};
  --gold: {gold}; --gold-light: {gold_light};
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0; font-family: 'Georgia', 'Times New Roman', serif;
  background: linear-gradient(180deg, var(--navy-deep), var(--navy) 40%, var(--navy-deep));
  color: #e8e8f0; line-height: 1.7;
}}
.gg-hero {{
  position: relative; padding: 90px 8vw 70px; overflow: hidden;
  background: radial-gradient(ellipse at top left, rgba(212,162,76,0.18), transparent 60%),
              radial-gradient(ellipse at bottom right, rgba(127,179,213,0.12), transparent 55%);
}}
.gg-hero::before {{
  content: ""; position: absolute; inset: 0; opacity: .5; pointer-events: none;
  background-image: repeating-linear-gradient(115deg, rgba(255,255,255,0.02) 0 2px, transparent 2px 40px);
  animation: gg-drift 30s linear infinite;
}}
@keyframes gg-drift {{ from {{ background-position: 0 0; }} to {{ background-position: 400px 200px; }} }}
.gg-eyebrow {{
  font-family: 'Helvetica Neue', Arial, sans-serif; letter-spacing: .18em; text-transform: uppercase;
  font-size: 12px; color: var(--gold-light); opacity: 0; animation: gg-fade-up .8s ease forwards;
}}
.gg-title {{
  font-size: clamp(32px, 5vw, 56px); margin: 14px 0 18px; font-weight: 700; color: #fff;
  opacity: 0; animation: gg-fade-up .9s ease .1s forwards;
}}
.gg-summary {{
  font-size: 18px; max-width: 62ch; color: #cfd3e6; opacity: 0; animation: gg-fade-up .9s ease .22s forwards;
}}
.gg-date {{
  font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px; color: #8890b0; margin-top: 22px;
  opacity: 0; animation: gg-fade-up .9s ease .3s forwards;
}}
@keyframes gg-fade-up {{ from {{ opacity: 0; transform: translateY(18px); }} to {{ opacity: 1; transform: translateY(0); }} }}

.gg-stats-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 16px; padding: 0 8vw 20px;
}}
.gg-stat-card {{
  background: linear-gradient(160deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
  border: 1px solid rgba(212,162,76,0.25); border-radius: 14px; padding: 18px 20px;
  backdrop-filter: blur(6px); transition: transform .25s ease, border-color .25s ease;
}}
.gg-stat-card:hover {{ transform: translateY(-4px); border-color: var(--gold); }}
.gg-stat-value {{ font-size: 26px; font-weight: 700; color: var(--gold-light); font-family: 'Helvetica Neue', Arial, sans-serif; }}
.gg-stat-label {{ font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: #9aa3c0; margin-top: 6px; }}
.gg-stat-change {{ font-size: 13px; margin-top: 6px; font-weight: 600; }}
.gg-change-up {{ color: #6fcf97; }} .gg-change-down {{ color: #eb6161; }}

main {{ max-width: 880px; margin: 0 auto; padding: 10px 8vw 100px; }}
h1, h2, h3 {{ font-family: 'Helvetica Neue', Arial, sans-serif; color: #fff; }}
.gg-section-h2 {{
  font-size: 26px; margin-top: 54px; padding-top: 18px; border-top: 1px solid rgba(212,162,76,0.2);
  position: relative;
}}
.gg-section-h2::before {{
  content: ""; position: absolute; top: -1px; left: 0; height: 2px; width: 60px; background: var(--gold);
}}
h3 {{ font-size: 19px; color: var(--gold-light); margin-top: 32px; }}
p {{ font-size: 16.5px; color: #d6d9e8; }}
.gg-list {{ font-size: 16.5px; color: #d6d9e8; padding-left: 22px; }}
.gg-list li {{ margin: 6px 0; }}
.gg-pullquote {{
  border-left: 3px solid var(--gold); margin: 30px 0; padding: 4px 0 4px 22px;
  font-style: italic; font-size: 19px; color: #f1e6cc;
}}
.gg-divider {{ border: none; border-top: 1px solid rgba(255,255,255,0.1); margin: 42px 0; }}

.gg-chart-wrap, .gg-table-wrap {{
  margin: 28px 0; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
  border-radius: 16px; padding: 20px 22px;
}}
.gg-chart-title {{
  font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px; letter-spacing: .04em;
  color: var(--gold-light); text-transform: uppercase; margin-bottom: 14px;
}}
.gg-chart-canvas-box {{ position: relative; height: 320px; }}
.gg-table-scroll {{ overflow-x: auto; }}
.gg-table {{ width: 100%; border-collapse: collapse; font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 14px; }}
.gg-table th {{
  text-align: left; padding: 10px 12px; color: var(--gold-light); border-bottom: 2px solid rgba(212,162,76,0.35);
  font-size: 12px; letter-spacing: .04em; text-transform: uppercase;
}}
.gg-table td {{ padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #d6d9e8; }}
.gg-table tr {{ opacity: 0; animation: gg-row-in .5s ease forwards; animation-delay: calc(var(--row-i) * 60ms); }}
@keyframes gg-row-in {{ from {{ opacity: 0; transform: translateX(-8px); }} to {{ opacity: 1; transform: translateX(0); }} }}

.gg-figure {{ margin: 32px 0; text-align: center; }}
.gg-figure img {{
  max-width: 100%; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.4);
  transform: scale(0.97); transition: transform .6s ease, box-shadow .6s ease;
}}
.gg-figure.gg-visible img {{ transform: scale(1); }}
.gg-figure figcaption {{ margin-top: 10px; font-size: 13px; color: #9aa3c0; font-style: italic; }}

.gg-reveal {{ opacity: 0; transform: translateY(24px); transition: opacity .7s ease, transform .7s ease; transition-delay: var(--d, 0ms); }}
.gg-reveal.gg-visible {{ opacity: 1; transform: translateY(0); }}

.gg-footer {{
  text-align: center; padding: 40px 8vw 60px; font-family: 'Helvetica Neue', Arial, sans-serif;
  font-size: 12px; color: #6a7295; border-top: 1px solid rgba(255,255,255,0.06);
}}
@media (max-width: 640px) {{ .gg-hero {{ padding: 60px 6vw 40px; }} main {{ padding: 0 6vw 70px; }} }}
"""

_JS = """
document.addEventListener('DOMContentLoaded', function () {
  // Scroll-reveal for anything marked data-reveal (paragraphs, headings,
  // charts, images, tables, stat cards).
  var revealEls = document.querySelectorAll('[data-reveal]');
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('gg-visible');
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });
  revealEls.forEach(function (el) { io.observe(el); });

  // Animated count-up for stat card values.
  var counters = document.querySelectorAll('.gg-count');
  var cio = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var el = entry.target;
      cio.unobserve(el);
      var target = parseFloat(el.getAttribute('data-count-target'));
      var decimals = parseInt(el.getAttribute('data-count-decimals') || '0', 10);
      if (isNaN(target)) return;
      var duration = 1200, start = null;
      function step(ts) {
        if (!start) start = ts;
        var progress = Math.min((ts - start) / duration, 1);
        var eased = 1 - Math.pow(1 - progress, 3);
        var current = target * eased;
        el.textContent = decimals > 0
          ? current.toFixed(decimals).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',')
          : Math.round(current).toLocaleString('en-IN');
        if (progress < 1) requestAnimationFrame(step);
        else el.textContent = decimals > 0
          ? target.toFixed(decimals).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',')
          : Math.round(target).toLocaleString('en-IN');
      }
      requestAnimationFrame(step);
    });
  }, { threshold: 0.4 });
  counters.forEach(function (el) { cio.observe(el); });

  // Chart.js instances, deferred until each canvas scrolls into view so
  // charts animate in as the reader reaches them rather than all firing
  // at page load.
  (window.__ggCharts || []).forEach(function (c) {
    var canvas = document.getElementById(c.id);
    if (!canvas) return;
    var chartIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        chartIO.unobserve(canvas);
        new Chart(canvas.getContext('2d'), c.config);
      });
    }, { threshold: 0.2 });
    chartIO.observe(canvas);
  });
});
"""


def build_html_report(report: str, title: str, question: str, summary: str,
                       key_stats: list, charts: list, images: list,
                       theme: dict | None = None) -> str:
    body_html = _markdown_to_html(report, charts, images, theme)
    stats_html = _render_key_stats(key_stats)
    safe_title = html.escape(title or question or "Research Report")
    safe_summary = html.escape(summary or "")
    date_str = datetime.now(timezone.utc).strftime("%d %B %Y")
    css = _build_css(theme)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{safe_title} — Growth Gradual</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>{css}</style>
</head>
<body>
  <header class="gg-hero">
    <div class="gg-eyebrow">Growth Gradual · Research Intelligence</div>
    <h1 class="gg-title">{safe_title}</h1>
    {f'<p class="gg-summary">{safe_summary}</p>' if safe_summary else ''}
    <div class="gg-date">Generated {date_str}</div>
  </header>
  {stats_html}
  <main>
    {body_html}
  </main>
  <footer class="gg-footer">Growth Gradual — In The Money · growth-gradual.com</footer>
  <script>{_JS}</script>
</body>
</html>"""


@router.post("")
async def generate_html_report(request: Request):
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        log.warning("HTML report: invalid request body")
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    report: str = body.get("report", "")
    if not report:
        log.warning("HTML report: no report content in request")
        return JSONResponse({"error": "No report content"}, status_code=400)

    if report.strip() in _KNOWN_FAILURE_MESSAGES or report.strip().startswith("## Report Generation Error"):
        log.warning("HTML report: refusing to render known report-generation-failure sentinel")
        return JSONResponse(
            {"error": "The report failed to generate, so there's nothing to export yet. Please try generating the report again."},
            status_code=422,
        )

    title: str = body.get("title", "")
    question: str = body.get("question", "Research Report")
    summary: str = body.get("summary", "")
    key_stats: list = body.get("keyStats", [])
    charts: list = body.get("charts", [])
    images: list = body.get("images", [])
    theme: dict | None = body.get("theme") if isinstance(body.get("theme"), dict) else None

    # Same unwrap-double-encoded-JSON safety net as routes/pdf.py.
    stripped = report.strip()
    if stripped.startswith("{") and '"report"' in stripped:
        try:
            inner = json.loads(stripped)
            if isinstance(inner.get("report"), str) and len(inner["report"]) > 100:
                report = inner["report"]
                title = title or inner.get("title", "")
                summary = summary or inner.get("summary", "")
                key_stats = key_stats or inner.get("keyStats", [])
                charts = charts or inner.get("charts", [])
                theme = theme or (inner.get("theme") if isinstance(inner.get("theme"), dict) else None)
        except Exception as e:
            log.debug("HTML report: report field is not double-encoded JSON, using as-is (%s)", e)

    if "\\n" in report:
        report = report.replace("\\n", "\n")
    report = re.sub(r"^```(?:json|markdown)?\s*", "", report.strip())
    report = re.sub(r"```\s*$", "", report).strip()

    try:
        html_doc = build_html_report(report, title, question, summary, key_stats, charts, images, theme)
    except Exception as e:
        log.error("HTML report: build_html_report failed: %s", e)
        return JSONResponse({"error": f"Failed to generate HTML report: {e}"}, status_code=500)

    elapsed = (time.perf_counter() - t0) * 1000
    log.info("HTML report: done — %.1f KB in %.0fms", len(html_doc) / 1024, elapsed)

    date_filename = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=html_doc,
        media_type="text/html",
        headers={
            "Content-Disposition": f'inline; filename="growth-gradual-report-{date_filename}.html"',
            "Cache-Control": "no-store",
        },
    )
