"""
POST /api/chat/report/html — Generate an animated, interactive HTML report.

Body: { report, title, question, summary, keyStats, charts, images, logoB64 }
(identical shape to /api/chat/report/pdf's body — this is a second renderer
for the SAME structured report data, not a second report-generation step.)

Why this exists as a separate route rather than an option on the PDF one:
PDF (see routes/pdf.py's build_pdf) renders from this SAME presentation-
driven HTML composition (build_html_report, below) via WeasyPrint or headless
Chromium — it is not a separate, independently-templated renderer, and it is
not static in the sense of always looking the same; per-report colors,
typography, section layout/density, and cover treatment all come from the
same planner/writer output this route uses. But PDF is still fundamentally a
static-DOCUMENT format: there is no such thing as an animated or interactive
element once WeasyPrint/Chromium has rasterized the page, by construction of
the format itself. (The old fixed-template ReportLab renderer, NAVY/GOLD
flowables and all, still exists as build_pdf's last-resort fallback for when
both WeasyPrint and Chromium are unavailable — see _legacy_build_pdf — but it
is not the normal path.) When a request asks for "animated images," "creative
UI," or an "interactive" report, there is no amount of prompting that makes a
paginated PDF produce that; the deliverable format itself has to change. This
route renders the exact same report/title/charts/keyStats/images payload as a
single self-contained HTML document instead, using CSS transitions/keyframes,
an IntersectionObserver-driven scroll-reveal, animated count-up stat cards,
and real Chart.js charts (which can render enter-animations, tooltips, and
hover states — none of which a static PDF chart can do).

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

# Re-validate theme here rather than trusting the client's echoed-back copy
# as-is: this route is the one place customCss gets concatenated into a real
# <style> tag, so re-running the same hex/font-name/CSS-escape checks used
# when the theme was first produced (routes/report.py) costs nothing and
# closes off a client that edits the JSON it sends back before re-download.
from routes.report import _sanitize_theme
from utils.presentation_schema import ReportPresentationSpec
from routes.source_manifest import normalise_source_manifest

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
    """Map our internal chart spec 'type' to a Chart.js chart type.

    bullet/boxplot ride the generic/floating-bar 'bar' path (see the
    dedicated blocks in _render_chart_block below); treemap/heatmap render
    as plain HTML/CSS grids with no Chart.js involvement at all (no matrix/
    treemap plugin is loaded); bubble and radar are native Chart.js types."""
    return {
        "bar": "bar",
        "line": "line",
        "pie": "doughnut",
        "scatter": "scatter",
        "arrow": "bar",  # rendered as a grouped before/after bar comparison
        "histogram": "bar",  # touching bars — see the barPercentage tweak below
        "bullet": "bar",
        "boxplot": "bar",
        "bubble": "bubble",
        "radar": "radar",
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
    palette = [theme_gold, theme_navy, theme.get("secondaryColor") or theme_navy, theme.get("mutedColor") or "#64748B", theme.get("surfaceAltColor") or theme_gold]

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

    if ctype == "bullet":
        # Actual-vs-target KPI bar, built with the same floating-bar overlay
        # trick as waterfall/candlestick above — three "bar" datasets pinned
        # to the SAME category via "grouped": false (here on the y-scale,
        # since indexAxis is "y") so they overlap instead of sitting side by
        # side: a wide light "track" to full scale, a narrower colored
        # "actual" bar, and a thin near-zero-width floating slice at the
        # target value drawn last, which reads as a tick mark. Matches
        # pdf.py's _bullet semantics (red when below target, navy otherwise)
        # with no extra plugin needed.
        data_pts = (series[0].get("data") or []) if series else []
        if not data_pts:
            log.warning("HTML report: skipping bullet chart '%s' — no KPI points", chart.get("title", "?"))
            return ""
        unit = html.escape(str(chart.get("unit") or ""))
        bl_labels = [str(d.get("label", "")) for d in data_pts]
        max_v = max((max(_to_num(d.get("value", 0)), _to_num(d.get("target", 0))) for d in data_pts), default=1) or 1
        max_v *= 1.08
        RED_HEX = "#c0392b"
        tick_w = max_v * 0.006
        track, actual, actual_colors, target_ticks = [], [], [], []
        for d in data_pts:
            v = _to_num(d.get("value", 0))
            t = _to_num(d.get("target", 0))
            track.append([0, max_v])
            actual.append([0, v])
            actual_colors.append(RED_HEX if v < t else theme_navy)
            target_ticks.append([max(0, t - tick_w), t + tick_w])
        chart_config = {
            "type": "bar",
            "data": {"labels": bl_labels, "datasets": [
                {"label": "Scale", "data": track, "backgroundColor": "rgba(255,255,255,0.08)",
                 "barPercentage": 0.9, "categoryPercentage": 0.7, "borderSkipped": False},
                {"label": f"Actual{f' ({unit})' if unit else ''}", "data": actual,
                 "backgroundColor": actual_colors, "barPercentage": 0.45, "categoryPercentage": 0.7,
                 "borderSkipped": False},
                {"label": "Target", "data": target_ticks, "backgroundColor": "#e8e8f0",
                 "barPercentage": 0.9, "categoryPercentage": 0.7, "borderSkipped": False},
            ]},
            "options": {
                "indexAxis": "y",
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1400, "easing": "easeOutQuart"},
                "plugins": {"legend": {"display": False}, "title": {"display": False}},
                "scales": {
                    "x": {"max": max_v,
                          "title": {"display": bool(x_label), "text": x_label, "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                    "y": {"grouped": False, "ticks": {"color": "#9aa3c0"}, "grid": {"display": False}},
                },
            },
        }
        box_h = max(200, 46 * len(bl_labels) + 40)
        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-chart-canvas-box" style="height:{box_h}px"><canvas id="{canvas_id}"></canvas></div>
</div>
<script>
window.__ggCharts = window.__ggCharts || [];
window.__ggCharts.push({{ id: "{canvas_id}", config: {json.dumps(chart_config)} }});
</script>"""

    if ctype == "boxplot":
        # Vertical box-and-whisker per entity — the same floating-bar overlay
        # trick, this time with "grouped": false on the x/category scale (as
        # in candlestick): a thin whisker range (min-max), a wider IQR box
        # (q1-q3) drawn on top, and a thin near-zero-height floating slice at
        # the median drawn last so it reads as a bright line through the box.
        data_pts = (series[0].get("data") or []) if series else []
        if not data_pts:
            log.warning("HTML report: skipping boxplot chart '%s' — no entities", chart.get("title", "?"))
            return ""
        unit = html.escape(str(chart.get("unit") or ""))
        bp_labels = [str(d.get("label", "")) for d in data_pts]
        all_v = [_to_num(d.get(k, 0)) for d in data_pts for k in ("min", "max")]
        span = ((max(all_v) if all_v else 1) - (min(all_v) if all_v else 0)) or 1
        median_eps = span * 0.008
        whiskers, boxes, medians, box_colors = [], [], [], []
        for i, d in enumerate(data_pts):
            mn, q1, med, q3, mx = (_to_num(d.get(k, 0)) for k in ("min", "q1", "median", "q3", "max"))
            whiskers.append([mn, mx])
            boxes.append([q1, q3])
            medians.append([max(mn, med - median_eps), min(mx, med + median_eps)])
            box_colors.append(palette[i % len(palette)])
        chart_config = {
            "type": "bar",
            "data": {"labels": bp_labels, "datasets": [
                {"label": "Range", "data": whiskers, "backgroundColor": box_colors,
                 "barPercentage": 0.12, "categoryPercentage": 0.7, "borderSkipped": False},
                {"label": f"IQR{f' ({unit})' if unit else ''}", "data": boxes, "backgroundColor": box_colors,
                 "barPercentage": 0.55, "categoryPercentage": 0.7, "borderSkipped": False},
                {"label": "Median", "data": medians, "backgroundColor": "#ffffff",
                 "barPercentage": 0.55, "categoryPercentage": 0.7, "borderSkipped": False},
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

    if ctype == "treemap":
        # No matrix/treemap Chart.js plugin is loaded, so this renders as a
        # plain HTML/CSS flexbox grid instead of a canvas — the same
        # alternating-row slice-and-dice idea as pdf.py's _treemap (one row
        # per group sized by the group's total, items inside a row sized by
        # their own value via flex-grow), just laid out by the browser
        # instead of computed pixel rects.
        data_pts = (series[0].get("data") or []) if series else []
        if len(data_pts) < 3:
            log.warning("HTML report: skipping treemap chart '%s' — fewer than 3 items", chart.get("title", "?"))
            return ""
        unit = str(chart.get("unit") or "")
        items = sorted(data_pts, key=lambda d: abs(_to_num(d.get("value", 0))), reverse=True)
        total = sum(abs(_to_num(d.get("value", 0))) for d in items) or 1

        def _tm_cell(d: dict, color: str) -> str:
            v = abs(_to_num(d.get("value", 0)))
            pct = v / total * 100
            lbl = html.escape(str(d.get("label", "")))
            vs = html.escape(f"{pct:.1f}%") if (unit == "%" or not unit) else html.escape(f"{v:,.0f}{unit}")
            grow = max(v, total * 0.01)
            return (f'<div class="gg-tm-cell" style="flex-grow:{grow};background:{color}">'
                    f'<span class="gg-tm-label">{lbl}</span><span class="gg-tm-value">{vs}</span></div>')

        groups: dict[str, list] = {}
        order: list[str] = []
        for d in items:
            g = str(d.get("group") or "")
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(d)

        color_i = 0
        if len(groups) > 1:
            rows_html = []
            for g in order:
                g_items = groups[g]
                g_total = sum(abs(_to_num(d.get("value", 0))) for d in g_items) or 1
                cells = []
                for d in g_items:
                    cells.append(_tm_cell(d, palette[color_i % len(palette)]))
                    color_i += 1
                group_label = f'<div class="gg-tm-group-label">{html.escape(g)}</div>' if g else ""
                rows_html.append(
                    f'<div class="gg-tm-row" style="flex-grow:{g_total}">{group_label}'
                    f'<div class="gg-tm-row-cells">{"".join(cells)}</div></div>'
                )
            body = f'<div class="gg-tm-wrap">{"".join(rows_html)}</div>'
        else:
            cells = [_tm_cell(d, palette[i % len(palette)]) for i, d in enumerate(items)]
            body = f'<div class="gg-tm-wrap gg-tm-flat">{"".join(cells)}</div>'

        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  {body}
</div>"""

    if ctype == "heatmap":
        # Also a plain HTML/CSS grid rather than canvas — no Chart.js matrix
        # plugin is loaded, and a rows x columns colored grid is simpler and
        # more accessible as real DOM/table-like markup anyway. Color scale
        # mirrors pdf.py's _heatmap: red (low) -> cream -> green (high).
        rows = chart.get("rows") or []
        cols = chart.get("columns") or []
        values = chart.get("values") or []
        if not rows or not cols or not values:
            log.warning("HTML report: skipping heatmap chart '%s' — missing rows/columns/values", chart.get("title", "?"))
            return ""
        unit = str(chart.get("unit") or "")
        flat = [_to_num(v) for r in values for v in r]
        lo, hi = (min(flat), max(flat)) if flat else (0.0, 1.0)
        span = (hi - lo) or 1
        RED_RGB, LIGHT_RGB, GREEN_RGB = (192, 57, 43), (240, 243, 255), (23, 138, 76)

        def _mix(c1: tuple, c2: tuple, t: float) -> tuple:
            t = max(0.0, min(1.0, t))
            return tuple(round(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))

        def _color_for(v: float) -> str:
            t = (v - lo) / span
            rgb = _mix(RED_RGB, LIGHT_RGB, t / 0.5) if t < 0.5 else _mix(LIGHT_RGB, GREEN_RGB, (t - 0.5) / 0.5)
            return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"

        header_cells = "".join(f'<div class="gg-hm-colhead">{html.escape(str(c))}</div>' for c in cols)
        body_rows = []
        for ri, row_name in enumerate(rows):
            row_vals = values[ri] if ri < len(values) else []
            cells = []
            for ci in range(len(cols)):
                v = _to_num(row_vals[ci]) if ci < len(row_vals) else 0.0
                vs = html.escape(f"{v:.0f}{unit}" if abs(v) >= 10 else f"{v:.1f}{unit}")
                cells.append(f'<div class="gg-hm-cell" style="background:{_color_for(v)}">{vs}</div>')
            body_rows.append(
                f'<div class="gg-hm-row"><div class="gg-hm-rowhead">{html.escape(str(row_name))}</div>'
                f'<div class="gg-hm-cells" style="grid-template-columns:repeat({len(cols)},1fr)">'
                f'{"".join(cells)}</div></div>'
            )
        return f"""
<div class="gg-reveal gg-chart-wrap" data-reveal>
  {f'<div class="gg-chart-title">{title}</div>' if title else ''}
  <div class="gg-hm-wrap">
    <div class="gg-hm-row gg-hm-header"><div class="gg-hm-rowhead"></div>
      <div class="gg-hm-cells" style="grid-template-columns:repeat({len(cols)},1fr)">{header_cells}</div></div>
    {"".join(body_rows)}
  </div>
</div>"""

    if ctype == "bubble":
        # Native Chart.js type. Series order is fixed per report.py's spec:
        # series[0]=x-metric, series[1]=y-metric, series[2]=size-metric, all
        # sharing the same labels — merged into {x, y, r} points here since
        # Chart.js's bubble controller needs that shape, not the flat
        # category-indexed arrays bar/line use.
        if len(series) < 3:
            log.warning("HTML report: skipping bubble chart '%s' — needs 3 series (x/y/size)", chart.get("title", "?"))
            return ""
        x_by = {str(p.get("label", "")): _to_num(p.get("value", 0)) for p in (series[0].get("data") or [])}
        y_by = {str(p.get("label", "")): _to_num(p.get("value", 0)) for p in (series[1].get("data") or [])}
        s_by = {str(p.get("label", "")): abs(_to_num(p.get("value", 0))) for p in (series[2].get("data") or [])}
        bb_labels = [l for l in x_by if l in y_by and l in s_by]
        if not bb_labels:
            log.warning("HTML report: skipping bubble chart '%s' — no matched entities across series", chart.get("title", "?"))
            return ""
        s_max = max(s_by[l] for l in bb_labels) or 1
        points = [{"x": x_by[l], "y": y_by[l], "r": 5 + (s_by[l] / s_max) * 22, "label": l} for l in bb_labels]
        bubble_colors = [palette[i % len(palette)] for i in range(len(points))]
        chart_config = {
            "type": "bubble",
            "data": {"datasets": [{
                "label": series[2].get("name", "Size"),
                "data": points,
                "backgroundColor": [f"{c}a6" for c in bubble_colors],
                "borderColor": bubble_colors,
                "borderWidth": 1.5,
            }]},
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1400, "easing": "easeOutQuart"},
                "plugins": {"legend": {"display": False}, "title": {"display": False}},
                "scales": {
                    "x": {"title": {"display": True, "text": x_label or series[0].get("name", ""), "color": "#9aa3c0"},
                          "ticks": {"color": "#9aa3c0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                    "y": {"title": {"display": True, "text": y_label or series[1].get("name", ""), "color": "#9aa3c0"},
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

    if ctype == "radar":
        # Native Chart.js type. Every series must share the same metric
        # labels in the same order (enforced upstream by report.py's
        # validator) — metrics come from series[0] and every other series is
        # re-keyed by label just in case ordering ever drifts.
        if not series:
            log.warning("HTML report: skipping radar chart '%s' — no series", chart.get("title", "?"))
            return ""
        metrics = [str(p.get("label", "")) for p in (series[0].get("data") or [])]
        if len(metrics) < 3:
            log.warning("HTML report: skipping radar chart '%s' — fewer than 3 metrics", chart.get("title", "?"))
            return ""
        radar_datasets = []
        for s_i, s in enumerate(series):
            by_label = {str(p.get("label", "")): _to_num(p.get("value", 0)) for p in (s.get("data") or [])}
            values = [by_label.get(m, 0) for m in metrics]
            color = palette[s_i % len(palette)]
            radar_datasets.append({
                "label": s.get("name", f"Series {s_i+1}"),
                "data": values,
                "backgroundColor": f"{color}33",
                "borderColor": color,
                "borderWidth": 2,
                "pointBackgroundColor": color,
            })
        chart_config = {
            "type": "radar",
            "data": {"labels": metrics, "datasets": radar_datasets},
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "animation": {"duration": 1400, "easing": "easeOutQuart"},
                "plugins": {
                    "legend": {"display": len(radar_datasets) > 1, "labels": {"color": "#e8e8f0"}},
                    "title": {"display": False},
                },
                "scales": {
                    "r": {
                        "angleLines": {"color": "rgba(255,255,255,0.08)"},
                        "grid": {"color": "rgba(255,255,255,0.08)"},
                        "pointLabels": {"color": "#9aa3c0", "font": {"size": 11}},
                        "ticks": {"color": "#9aa3c0", "backdropColor": "transparent"},
                    },
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

    if ctype == "histogram":
        # Same shape as a single-series bar chart — the only thing that
        # makes it read as a histogram rather than a ranked bar chart is the
        # bars touching, so zero out the inter-bar/inter-category gap
        # instead of adding a separate render path (mirrors datawrapper.py,
        # which needs no special-casing here either).
        for ds in datasets:
            ds["barPercentage"] = 1.0
            ds["categoryPercentage"] = 0.98
            ds["borderColor"] = "#0a1230"
            ds["borderWidth"] = 1

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
    """Small safe Markdown renderer with native tables/charts/images."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_mode: str | None = None
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

    def is_table_start(i: int) -> bool:
        if i + 1 >= len(lines):
            return False
        a = lines[i].strip(); b = lines[i + 1].strip()
        return a.startswith("|") and a.endswith("|") and re.match(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$", b) is not None

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.rstrip()
        stripped = line.strip()

        chart_m = _CHART_RE.match(stripped)
        webimg_m = _WEBIMG_RE.match(stripped)
        if chart_m:
            flush_para(); close_list(); n = int(chart_m.group(1))
            if 1 <= n <= len(charts): out.append(_render_chart_block(charts[n - 1], n, theme))
            i += 1; continue
        if webimg_m:
            flush_para(); close_list(); n = int(webimg_m.group(1))
            if 1 <= n <= len(images): out.append(_render_image_block(images[n - 1], n))
            i += 1; continue

        if is_table_start(i):
            flush_para(); close_list()
            rows = []
            # Header + separator + following pipe rows.
            rows.append(lines[i].strip())
            i += 2
            while i < len(lines):
                candidate = lines[i].strip()
                if not (candidate.startswith("|") and candidate.endswith("|")):
                    break
                rows.append(candidate); i += 1
            def cells(row: str):
                return [c.strip() for c in row.strip("|").split("|")]
            heads = cells(rows[0])
            thead = "".join(f"<th>{_inline_md(c)}</th>" for c in heads)
            body_rows = []
            for row in rows[1:]:
                vals = cells(row)
                body_rows.append("<tr>" + "".join(f"<td>{_inline_md(vals[k] if k < len(vals) else '')}</td>" for k in range(len(heads))) + "</tr>")
            out.append('<div class="gg-table-wrap gg-block"><div class="gg-table-scroll"><table class="gg-table"><thead><tr>' + thead + '</tr></thead><tbody>' + ''.join(body_rows) + '</tbody></table></div></div>')
            continue

        if not stripped:
            flush_para(); close_list(); i += 1; continue
        if stripped.startswith("### "):
            flush_para(); close_list(); out.append(f'<h3 class="gg-reveal" data-reveal>{_inline_md(stripped[4:])}</h3>'); i += 1; continue
        if stripped.startswith("## "):
            flush_para(); close_list(); out.append(f'<h2 class="gg-reveal gg-section-h2" data-reveal>{_inline_md(stripped[3:])}</h2>'); i += 1; continue
        if stripped.startswith("# "):
            flush_para(); close_list(); out.append(f'<h1 class="gg-reveal" data-reveal>{_inline_md(stripped[2:])}</h1>'); i += 1; continue
        if stripped.startswith("> "):
            flush_para(); close_list(); out.append(f'<blockquote class="gg-reveal gg-pullquote" data-reveal>{_inline_md(stripped[2:])}</blockquote>'); i += 1; continue
        if stripped in ("---", "***", "___"):
            flush_para(); close_list(); out.append('<hr class="gg-divider" />'); i += 1; continue

        bullet_m = re.match(r"^[-•*]\s+(.*)$", stripped)
        numbered_m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet_m:
            flush_para()
            if list_mode != "ul": close_list(); out.append('<ul class="gg-reveal gg-list" data-reveal>'); list_mode = "ul"
            out.append(f"<li>{_inline_md(bullet_m.group(1))}</li>"); i += 1; continue
        if numbered_m:
            flush_para()
            if list_mode != "ol": close_list(); out.append('<ol class="gg-reveal gg-list" data-reveal>'); list_mode = "ol"
            out.append(f"<li>{_inline_md(numbered_m.group(1))}</li>"); i += 1; continue

        close_list(); para_buf.append(stripped); i += 1

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


def _google_font_link(font_family: str | None) -> str:
    """Build a <link> tag that loads a theme-requested Google Font, so
    theme.fontFamily (see report.py's THEME schema / _sanitize_theme) is
    actually available for the CSS to use instead of just naming a font the
    browser doesn't have. font_family is already validated upstream as
    alnum+space only (max 40 chars), so it's safe to drop straight into a
    URL path segment with spaces turned into '+'."""
    if not font_family:
        return ""
    family_param = font_family.strip().replace(" ", "+")
    href = f"https://fonts.googleapis.com/css2?family={family_param}:wght@400;600;700&display=swap"
    return f'<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="stylesheet" href="{html.escape(href)}">'


def _merge_presentation_visual(theme: dict | None, presentation: object) -> dict:
    """Merge the validated presentation visual tokens into renderer theme tokens.

    Presentation visual choices take precedence over any legacy/separate theme
    tokens. No domain mapping is performed here; the renderer only executes the
    structured values selected upstream and validated by ReportPresentationSpec.
    """
    out = dict(theme or {}) if isinstance(theme, dict) else {}
    if isinstance(presentation, ReportPresentationSpec):
        visual = presentation.to_dict().get("visual") or {}
    elif isinstance(presentation, dict):
        visual = presentation.get("visual") or {}
    else:
        visual = {}
    if not isinstance(visual, dict):
        return out
    mapping = {
        "primary_color": "primaryColor",
        "secondary_color": "secondaryColor",
        "accent_color": "accentColor",
        "surface_color": "surfaceColor",
        "surface_alt_color": "surfaceAltColor",
        "text_color": "textColor",
        "muted_color": "mutedColor",
        "border_color": "borderColor",
        "mode": "visualMode",
        "typography_scale": "typographyScale",
        "shape_style": "shapeStyle",
        "accent_strategy": "accentStrategy",
        "chart_style": "chartStyle",
        "spacing_scale": "spacingScale",
        "card_style": "cardStyle",
        "section_rule": "sectionRule",
        "title_alignment": "titleAlignment",
        "background_treatment": "backgroundTreatment",
    }
    for src, dst in mapping.items():
        value = visual.get(src)
        if value not in (None, ""):
            out[dst] = value
    return out

def _build_css(theme: dict | None) -> str:
    """Build a neutral renderer stylesheet.

    Composition is driven by ``ReportPresentationSpec``; this stylesheet only
    supplies reusable primitives for the supported layouts.  It intentionally
    does not accept arbitrary CSS from the LLM.
    """
    theme = theme or {}
    navy = theme.get("primaryColor") or "#172033"
    gold = theme.get("accentColor") or "#3f6ed8"
    navy_deep = _shade_hex(navy, -0.22)
    gold_light = _shade_hex(gold, 0.28)
    # Derive secondary surfaces/rules from the report's own theme so the
    # renderer does not impose a single fixed palette.
    paper = theme.get("surfaceColor") or _shade_hex(navy, 0.97)
    paper_alt = theme.get("surfaceAltColor") or _shade_hex(gold, 0.93)
    ink = theme.get("textColor") or navy
    ink_soft = theme.get("mutedColor") or _shade_hex(navy, 0.45)
    rule = theme.get("borderColor") or _shade_hex(navy, 0.82)
    font = theme.get("fontFamily")
    if font:
        font_body = f"'{font}', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
        font_heading = f"'{font}', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    else:
        font_body = "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
        font_heading = "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    base_css = f"""
:root {{
  --primary: {navy}; --accent: {gold}; --secondary: {theme.get("secondaryColor") or _shade_hex(gold, -0.10)}; --accent-soft: {gold_light};
  --ink: {ink}; --ink-soft: {ink_soft}; --paper: {paper}; --paper-alt: {paper_alt}; --surface-strong: {theme.get("surfaceAltColor") or paper_alt}; --rule: {rule}; --muted: {ink_soft};
  --negative: #b42318; --positive: #147a4b; --font-body: {font_body}; --font-heading: {font_heading};
  --content-max: 1120px;
  --radius: {"0px" if theme.get("shapeStyle") == "sharp" else "18px" if theme.get("shapeStyle") == "rounded" else "10px"};
  --shadow: {"none" if theme.get("shapeStyle") == "sharp" else "0 10px 30px rgba(15, 23, 42, .08)"};
  --title-scale: {"0.88" if theme.get("typographyScale") == "compact" else "1.16" if theme.get("typographyScale") == "dramatic" else "1"};
  --space-unit: {"6px" if theme.get("spacingScale") == "compact" else "11px" if theme.get("spacingScale") == "airy" else "8px"};
  --card-border-width: {"0px" if theme.get("cardStyle") == "flat" else "2px" if theme.get("cardStyle") == "filled" else "1px"};
  --title-align: {theme.get("titleAlignment") or "left"};
  --section-rule-color: {"transparent" if theme.get("sectionRule") == "none" else "var(--accent)" if theme.get("sectionRule") == "accent_bar" else "var(--rule)"};
  --panel-radius: var(--radius); --panel-shadow: var(--shadow);
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; background: var(--paper); }}
body {{ margin: 0; font-family: var(--font-body); color: var(--ink); background: var(--paper); line-height: 1.68; }}
a {{ color: var(--accent); }}
h1,h2,h3,h4 {{ font-family: var(--font-heading); color: var(--ink); line-height: 1.18; }}
.gg-cover .gg-eyebrow, .gg-section--high .gg-section-heading, .gg-section--critical .gg-section-heading {{ border-color: var(--accent); }}
p {{ font-size: 16px; color: var(--ink-soft); }}
main {{ width: min(var(--content-max), calc(100% - 48px)); margin: 0 auto; padding: 34px 0 90px; }}

/* Cover treatments — the renderer chooses one based on presentation.cover.treatment. */
.gg-cover {{ width: min(var(--content-max), calc(100% - 48px)); margin: 34px auto 0; border-bottom: 1px solid var(--rule); }}
.gg-cover--minimal {{ padding: 20px 0 28px; }}
.gg-cover--classic {{ text-align: center; padding: 96px 8% 86px; background: var(--paper-alt); border: 1px solid var(--rule); }}
.gg-cover--classic .gg-title {{ max-width: 900px; margin-left: auto; margin-right: auto; }}
.gg-cover--bold_banner {{ padding: 54px 56px; background: linear-gradient(135deg, var(--ink), var(--secondary)); color: #fff; border: 0; }}
.gg-cover--bold_banner .gg-eyebrow, .gg-cover--bold_banner .gg-title, .gg-cover--bold_banner .gg-summary, .gg-cover--bold_banner .gg-date {{ color: #fff; }}
.gg-cover--data_driven {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, .7fr); gap: 32px; align-items: end; padding: 48px 0; }}
.gg-cover__aside {{ padding: 20px; border-left: 3px solid var(--accent); background: var(--paper-alt); }}
.gg-cover__aside-label {{ margin: 0 0 8px; font: 700 11px var(--font-heading); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); }}
.gg-eyebrow {{ margin: 0 0 12px; font: 700 11px var(--font-heading); letter-spacing: .14em; text-transform: uppercase; color: var(--accent); }}
.gg-title {{ margin: 0 0 16px; font-size: 52px; line-height: 1.08; letter-spacing: -.025em; text-align: var(--title-align); }}
.gg-summary {{ max-width: 72ch; margin: 0; font-size: 18px; }}
.gg-date {{ margin-top: 18px; color: var(--muted); font-size: 13px; }}

.gg-summary-wrap {{ margin: 0 0 32px; }}
.gg-summary-wrap--sidebar {{ display: grid; grid-template-columns: minmax(240px, .34fr) minmax(0, 1fr); gap: 28px; align-items: start; }}
.gg-summary-wrap--end {{ margin-top: 42px; }}
.gg-summary-card {{ padding: 26px 28px; border: var(--card-border-width) solid var(--rule); background: var(--paper-alt); box-shadow: var(--shadow); border-radius: var(--radius); }}
.gg-summary-card--high {{ border-left: 4px solid var(--accent); }}
.gg-summary-card--critical {{ border-left: 5px solid var(--negative); }}
.gg-summary-heading {{ margin: 0 0 14px; font-size: 24px; }}

.gg-report-sections {{ display: flex; flex-direction: column; gap: 26px; }}
.gg-composition--grid .gg-report-sections {{ gap: 18px; }}
.gg-composition--two_column .gg-report-sections {{ max-width: 1080px; }}
.gg-composition--sidebar_main .gg-report-sections {{ max-width: 1080px; }}

.gg-section {{ scroll-margin-top: 24px; }}
.gg-section--full_bleed {{ width: 100vw; margin-left: calc(50% - 50vw); padding: 42px max(24px, calc((100vw - var(--content-max)) / 2)); background: var(--paper-alt); }}
.gg-section--sidebar_main {{ display: grid; grid-template-columns: minmax(180px, .26fr) minmax(0, 1fr); gap: 30px; align-items: start; }}
.gg-section--sidebar_main .gg-section-heading {{ position: sticky; top: 20px; }}
.gg-section-heading {{ margin-bottom: 18px; }}
.gg-section-heading h2 {{ margin: 0; font-size: 30px; line-height: 1.15; text-align: var(--title-align); }}
.gg-section-heading p {{ margin: 8px 0 0; font-size: 13px; color: var(--muted); }}
.gg-section--high .gg-section-heading {{ border-top: 3px solid var(--accent); padding-top: 14px; }}
.gg-section--critical .gg-section-heading {{ border-top: 4px solid var(--negative); padding-top: 14px; }}
.gg-section-body--two_column {{ columns: 2 320px; column-gap: 38px; }}
.gg-section-body--grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }}
.gg-section-body--sidebar_main {{ min-width: 0; }}
.gg-section[data-density="sparse"] {{ padding-top: 14px; padding-bottom: 14px; }}
.gg-section[data-density="dense"] {{ font-size: 15px; }}
.gg-section[data-density="dense"] p {{ font-size: 15px; }}
.gg-section[data-density="dense"] .gg-block {{ margin-bottom: 14px; }}
.gg-composition--hybrid .gg-section + .gg-section {{ border-top: 1px solid var(--rule); padding-top: 24px; }}
.gg-section[data-emphasis="critical"] {{ background: color-mix(in srgb, var(--negative) 5%, transparent); padding: 16px; border-radius: 10px; }}
.gg-section[data-section-type="comparison"] .gg-table-wrap, .gg-section[data-section-type="financials"] .gg-table-wrap {{ overflow: auto; }}
.gg-section[data-section-type="timeline"] .gg-section-body, .gg-section[data-section-type="findings"] .gg-section-body {{ max-width: 82ch; }}

.gg-block {{ min-width: 0; break-inside: avoid; }}
.gg-block--emphasis-high {{ border-left: 3px solid var(--accent); padding-left: 18px; }}
.gg-block--emphasis-critical {{ border-left: 4px solid var(--negative); padding-left: 18px; }}
.gg-list {{ font-size: 16px; color: var(--ink-soft); padding-left: 24px; }}
.gg-list li {{ margin: 6px 0; }}
.gg-pullquote {{ border-left: 3px solid var(--accent); margin: 26px 0; padding: 6px 0 6px 20px; font-style: italic; font-size: 19px; color: var(--ink); }}
.gg-divider {{ border: 0; border-top: 1px solid var(--rule); margin: 36px 0; }}

.gg-chart-wrap, .gg-table-wrap {{ margin: calc(var(--space-unit) * 2) 0; padding: calc(var(--space-unit) * 2) calc(var(--space-unit) * 2.5); background: var(--paper-alt); border: var(--card-border-width) solid var(--rule); break-inside: avoid; }}
.gg-chart-title {{ margin-bottom: 12px; font: 700 12px var(--font-heading); letter-spacing: .06em; text-transform: uppercase; color: var(--accent); }}
.gg-chart-canvas-box {{ position: relative; height: 320px; }}
.gg-table-scroll {{ overflow-x: auto; }}
.gg-table {{ width: 100%; border-collapse: collapse; font-family: var(--font-heading); font-size: 14px; }}
.gg-table th {{ text-align: left; padding: 10px 12px; color: var(--ink); border-bottom: 2px solid var(--accent); }}
.gg-table td {{ padding: 10px 12px; border-bottom: 1px solid var(--rule); color: var(--ink-soft); vertical-align: top; }}

.gg-metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 18px 0; }}
.gg-metric {{ padding: 16px 17px; border: 1px solid var(--rule); background: var(--paper); }}
.gg-metric-value {{ font: 700 24px var(--font-heading); color: var(--ink); }}
.gg-metric-label {{ margin-top: 5px; font: 600 11px var(--font-heading); color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}
.gg-metric-change {{ margin-top: 5px; font-size: 12px; font-weight: 700; }}

/* Model-selected visual direction: tokens above are the only inputs. */
body.gg-visual--dark .gg-eyebrow, body.gg-visual--dark .gg-chart-title {{ color: var(--accent); }}
body.gg-type-scale--compact .gg-section-heading h2 {{ letter-spacing: -.01em; }}
body.gg-type-scale--dramatic .gg-section-heading h2 {{ letter-spacing: -.035em; }}
body.gg-shape--sharp .gg-chart-wrap, body.gg-shape--sharp .gg-table-wrap, body.gg-shape--sharp .gg-callout, body.gg-shape--sharp .gg-summary-card, body.gg-shape--sharp .gg-metric, body.gg-shape--sharp .gg-stat-card, body.gg-shape--sharp .gg-source-card {{ border-radius: 0; }}
body.gg-shape--rounded .gg-chart-wrap, body.gg-shape--rounded .gg-table-wrap, body.gg-shape--rounded .gg-callout, body.gg-shape--rounded .gg-summary-card, body.gg-shape--rounded .gg-metric, body.gg-shape--rounded .gg-stat-card, body.gg-shape--rounded .gg-source-card {{ border-radius: 18px; }}
body.gg-spacing--compact .gg-section {{ margin-bottom: 10px; }}
body.gg-spacing--airy .gg-section {{ margin-bottom: 34px; }}
body.gg-card--flat .gg-chart-wrap, body.gg-card--flat .gg-table-wrap, body.gg-card--flat .gg-callout, body.gg-card--flat .gg-summary-card, body.gg-card--flat .gg-metric, body.gg-card--flat .gg-stat-card, body.gg-card--flat .gg-source-card {{ box-shadow: none; border-color: transparent; }}
body.gg-card--filled .gg-chart-wrap, body.gg-card--filled .gg-table-wrap, body.gg-card--filled .gg-callout, body.gg-card--filled .gg-summary-card, body.gg-card--filled .gg-metric, body.gg-card--filled .gg-stat-card, body.gg-card--filled .gg-source-card {{ background: var(--surface-strong, var(--paper-alt)); border-width: 2px; }}
body.gg-rule--none .gg-section-heading {{ border-bottom: 0; }}
body.gg-rule--hairline .gg-section-heading {{ border-bottom: 1px solid var(--rule); padding-bottom: 8px; }}
body.gg-rule--accent_bar .gg-section-heading {{ border-bottom: 3px solid var(--accent); padding-bottom: 8px; }}
body.gg-rule--panel .gg-section-heading {{ background: var(--paper-alt); border: 1px solid var(--rule); padding: 12px 14px; }}
body.gg-align--center .gg-title, body.gg-align--center .gg-section-heading h2 {{ text-align: center; }}
body.gg-bg--tinted {{ background: var(--paper-alt); }}
body.gg-bg--banded .gg-section:nth-of-type(even) {{ background: var(--paper-alt); padding: 18px; }}

/* Legacy/stat-dashboard primitive used by metrics_dashboard sections. */
.gg-stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 18px 0; }}
.gg-stat-card {{ min-width: 0; padding: 14px 15px; border: 1px solid var(--rule); background: var(--paper); break-inside: avoid; }}
.gg-stat-value {{ font: 700 22px var(--font-heading); color: var(--ink); line-height: 1.12; overflow-wrap: anywhere; }}
.gg-stat-label {{ margin-top: 5px; font: 600 10px var(--font-heading); color: var(--muted); text-transform: uppercase; letter-spacing: .045em; line-height: 1.2; }}
.gg-stat-change {{ margin-top: 4px; font-size: 11px; font-weight: 700; }}
.gg-change-up {{ color: var(--positive); }} .gg-change-down {{ color: var(--negative); }}

.gg-timeline {{ position: relative; display: grid; gap: 14px; margin: 18px 0; }}
.gg-timeline-item {{ display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 16px; padding: 14px 0; border-top: 1px solid var(--rule); }}
.gg-timeline-date {{ font: 700 12px var(--font-heading); color: var(--accent); }}
.gg-timeline-title {{ margin: 0; font: 700 15px var(--font-heading); }}
.gg-timeline-desc {{ margin: 5px 0 0; color: var(--ink-soft); font-size: 14px; }}

.gg-callout {{ margin: 20px 0; padding: 18px 20px; border: 1px solid var(--rule); border-left: 4px solid var(--accent); background: var(--paper-alt); break-inside: avoid; }}
.gg-callout--warning, .gg-callout--negative {{ border-left-color: var(--negative); }}
.gg-callout--positive {{ border-left-color: var(--positive); }}
.gg-callout h4 {{ margin: 0 0 6px; font-size: 14px; }}
.gg-callout p {{ margin: 0; }}

.gg-risk-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; margin: 18px 0; }}
.gg-risk {{ padding: 14px; border: 1px solid var(--rule); background: var(--paper-alt); box-shadow: var(--shadow); border-radius: var(--radius); }}
.gg-risk__name {{ font-weight: 700; }}
.gg-risk__meta {{ margin-top: 5px; color: var(--muted); font-size: 12px; }}
.gg-risk__mitigation {{ margin-top: 8px; font-size: 13px; color: var(--ink-soft); }}

.gg-figure {{ margin: 24px 0; text-align: center; break-inside: avoid; }}
.gg-figure img {{ max-width: 100%; height: auto; border: 1px solid var(--rule); }}
.gg-figure figcaption {{ margin-top: 8px; font-size: 13px; color: var(--muted); font-style: italic; }}

.gg-reveal {{ opacity: 0; transform: translateY(12px); transition: opacity .5s ease, transform .5s ease; transition-delay: var(--d, 0ms); }}
.gg-reveal.gg-visible {{ opacity: 1; transform: translateY(0); }}
.gg-sources {{ margin-top: 36px; padding: 22px; border: 1px solid var(--rule); background: var(--paper-alt); box-shadow: var(--shadow); border-radius: var(--radius); }}
.gg-sources, .gg-source-card {{ break-inside: auto; }}

.gg-sources h2 {{ margin-top: 0; }}
.gg-sources-intro {{ color: var(--muted); font-size: 13px; }}
.gg-sources-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; }}
.gg-source-card {{ display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 10px; padding: 12px; border: 1px solid var(--rule); background: var(--paper); break-inside: avoid; }}
.gg-source-number {{ width: 24px; height: 24px; display: grid; place-items: center; border: 1px solid var(--accent); border-radius: 50%; color: var(--accent); font: 700 11px var(--font-heading); }}
.gg-source-title {{ color: var(--ink); font: 600 13px var(--font-heading); line-height: 1.35; overflow-wrap: anywhere; }}
.gg-source-meta {{ color: var(--muted); font-size: 11px; margin-top: 4px; }}
.gg-source-link {{ color: var(--accent); font-size: 12px; text-decoration: none; overflow-wrap: anywhere; }}
.gg-source-link:hover {{ text-decoration: underline; }}
.gg-footer {{ text-align: center; padding: 34px 24px 48px; color: var(--muted); border-top: 1px solid var(--rule); font-size: 11px; }}

@media (max-width: 760px) {{
  main, .gg-cover {{ width: min(100% - 28px, var(--content-max)); }}
  .gg-cover--data_driven, .gg-summary-wrap--sidebar, .gg-section--sidebar_main {{ grid-template-columns: 1fr; }}
  .gg-section--sidebar_main .gg-section-heading {{ position: static; }}
  .gg-section-body--two_column {{ columns: 1; }}
  .gg-timeline-item {{ grid-template-columns: 1fr; gap: 5px; }}
}}
@media print {{
  /* PDF/print layout: override browser-oriented widths so A4 content cannot overflow. */
  html, body {{ background: #fff; }}
  body {{ font-size: 10.5pt; line-height: 1.48; overflow-wrap: anywhere; }}
  p, .gg-list {{ font-size: 10.5pt; line-height: 1.48; }}
  h1 {{ break-after: avoid-page; }}
  h2, h3, h4 {{ break-after: avoid-page; page-break-after: avoid; }}
  p {{ orphans: 3; widows: 3; }}
  main, .gg-cover {{ width: 100% !important; max-width: none !important; margin-left: 0 !important; margin-right: 0 !important; }}
  main {{ padding: 18px 0 38px; }}
  .gg-cover {{ margin-top: 0; }}
  .gg-title {{ font-size: 30pt; line-height: 1.08; }}
  .gg-summary {{ font-size: 12pt; line-height: 1.5; }}
  .gg-summary-card {{ padding: 16px 18px; }}
  .gg-report-sections {{ gap: 18px; }}
  .gg-section {{ break-inside: auto; }}
  .gg-section-heading {{ break-inside: avoid; break-after: avoid-page; margin-bottom: 10px; }}
  .gg-section-heading h2 {{ font-size: 18pt; line-height: 1.15; }}
  .gg-section--full_bleed {{ width: 100% !important; margin-left: 0; padding-left: 0; padding-right: 0; }}
  .gg-section-body--two_column {{ columns: 2 260px; column-gap: 24px; }}
  .gg-chart-wrap, .gg-table-wrap {{ margin: 12px 0; padding: 10px 12px; }}
  .gg-chart-canvas-box {{ height: 220px; min-height: 0; }}
  .gg-pdf-chart-fallback {{ width: 100% !important; max-width: 100% !important; overflow: hidden; }}
  .gg-pdf-chart-fallback svg {{ width: 100% !important; height: auto !important; max-width: 100% !important; display: block; }}
  .gg-table-scroll {{ overflow: visible; width: 100%; }}
  .gg-table {{ width: 100%; table-layout: fixed; font-size: 9.2pt; }}
  .gg-table th {{ padding: 6px 7px; font-size: 9pt; overflow-wrap: anywhere; }}
  .gg-table td {{ padding: 6px 7px; font-size: 9pt; overflow-wrap: anywhere; }}
  .gg-metrics {{ grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }}
  .gg-metric {{ padding: 9px 10px; }}
  .gg-metric-value {{ font-size: 17pt; line-height: 1.1; }}
  .gg-metric-label {{ font-size: 8pt; line-height: 1.2; }}
  .gg-metric-change {{ font-size: 8.5pt; }}
  .gg-stats-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }}
  .gg-stat-card {{ padding: 9px 10px; }}
  .gg-stat-value {{ font-size: 16pt; line-height: 1.08; }}
  .gg-stat-label {{ font-size: 7.8pt; line-height: 1.18; }}
  .gg-stat-change {{ font-size: 8pt; }}
  .gg-timeline-item {{ grid-template-columns: 86px minmax(0, 1fr); gap: 10px; padding: 10px 0; }}
  .gg-timeline-title {{ font-size: 10.5pt; }}
  .gg-timeline-desc {{ font-size: 9.5pt; }}
  .gg-callout {{ margin: 12px 0; padding: 10px 12px; }}
  .gg-risk-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }}
  .gg-risk {{ padding: 9px; }}
  .gg-risk__meta, .gg-risk__mitigation {{ font-size: 8.8pt; }}
  .gg-sources {{ margin-top: 24px; padding: 14px; }}
  .gg-sources-intro {{ font-size: 9pt; }}
  .gg-sources-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }}
  .gg-sources[data-source-placement="appendix"] {{ break-before: page; page-break-before: always; }}
  .gg-source-card {{ grid-template-columns: 18px minmax(0, 1fr); gap: 6px; padding: 7px; }}
  .gg-source-number {{ width: 18px; height: 18px; font-size: 8pt; border-radius: 4px; }}
  .gg-source-title {{ font-size: 8.7pt; line-height: 1.22; }}
  .gg-source-meta {{ font-size: 7.6pt; margin-top: 2px; }}
  .gg-source-link {{ font-size: 7.8pt; line-height: 1.15; }}
  .gg-footer {{ padding: 18px 0 24px; font-size: 8pt; break-before: avoid; }}
  .gg-reveal {{ opacity: 1 !important; transform: none !important; }}
}}
"""
    # Report-specific custom CSS — the writer LLM's optional theme.customCss
    # (see report.py's THEME schema field). Already sanitized by
    # routes.report._sanitize_theme before it ever reaches this renderer:
    # length-capped and rejected outright if it contains any of the
    # </style>/<script>/@import/expression()/url()/javascript:/behaviour:/
    # -moz-binding escape hatches (see _CSS_DANGER_RE there) — what survives
    # is plain CSS declarations (colors, borders, gradients, animations,
    # pseudo-elements, media queries), so it's safe to concatenate as-is.
    # Appended last, after every token/utility rule above, so it can
    # actually override them for an explicitly requested rich visual
    # treatment — that's the whole point of the field; appending it earlier
    # would let the base stylesheet's own rules win on selector order instead.
    custom_css = str((theme or {}).get("customCss") or "").strip()
    if custom_css:
        base_css += f"\n/* --- report-specific custom CSS (model-authored, sanitized) --- */\n{custom_css}\n"
    return base_css


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


def _safe_plain_url(value: object) -> str:
    url = str(value or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return ""
    return url


def _slug_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _split_report_into_sections(report: str) -> list[dict]:
    """Split report markdown at H2 boundaries while preserving all content."""
    lines = report.replace("\r\n", "\n").split("\n")
    sections: list[dict] = []
    current_title = ""
    current: list[str] = []
    for line in lines:
        if re.match(r"^##\s+", line.strip()):
            if current_title or any(x.strip() for x in current):
                sections.append({"title": current_title or "Report", "body": "\n".join(current).strip()})
            current_title = re.sub(r"^##\s+", "", line.strip()).strip()
            current = []
        else:
            current.append(line)
    if current_title or any(x.strip() for x in current):
        sections.append({"title": current_title or "Report", "body": "\n".join(current).strip()})
    return [s for s in sections if s["body"] or s["title"]]


def _fallback_presentation(report: str, title: str, summary: str, key_stats: list, charts: list) -> ReportPresentationSpec:
    """Create a safe composition for legacy payloads that lack a presentation."""
    actual = _split_report_into_sections(report)
    planned = []
    for i, sec in enumerate(actual):
        kind = "narrative"
        lower = _slug_text(sec["title"])
        if any(x in lower for x in ("financial", "revenue", "profit", "valuation")):
            kind = "financials"
        elif any(x in lower for x in ("risk", "threat", "downside")):
            kind = "risk_assessment"
        elif any(x in lower for x in ("timeline", "chronology", "history")):
            kind = "timeline"
        elif any(x in lower for x in ("comparison", "versus", "vs")):
            kind = "comparison"
        elif any(x in lower for x in ("methodology", "methods")):
            kind = "methodology"
        elif any(x in lower for x in ("findings", "results")):
            kind = "findings"
        planned.append({
            "id": f"section-{i + 1}", "title": sec["title"] or f"Section {i + 1}",
            "section_type": kind, "layout": "single_column", "density": "standard",
            "emphasis": "normal", "order": i, "blocks": [],
        })
    if not planned:
        planned = [{"id": "overview", "title": "Report", "section_type": "narrative", "order": 0, "blocks": []}]
    preset = {
        "domain": "generic",
        "cover": {"enabled": True, "title": title or "Research Report", "subtitle": "", "treatment": "minimal", "show_date": True},
        "executive_summary": {"placement": "after_cover" if summary else "none", "heading": "Executive Summary", "key_metrics": []},
        "sections": planned,
        "source_appendix": {"placement": "end_of_report", "group_by_section": False, "include_appendix": True},
        "default_layout": "single_column", "default_density": "standard",
    }
    spec, _ = ReportPresentationSpec.from_llm_output(preset)
    return spec


def _normalise_presentation(presentation: object, report: str, title: str, summary: str, key_stats: list, charts: list) -> tuple[ReportPresentationSpec, list[str]]:
    if isinstance(presentation, ReportPresentationSpec):
        try:
            presentation.validate_strict()
            return presentation, []
        except Exception:
            pass
    if not isinstance(presentation, dict):
        return _fallback_presentation(report, title, summary, key_stats, charts), ["No valid presentation supplied; legacy-safe fallback used."]
    spec, warnings = ReportPresentationSpec.from_llm_output(presentation)
    if not spec.sections:
        fallback = _fallback_presentation(report, title, summary, key_stats, charts)
        warnings.append("Presentation had no valid sections; legacy-safe fallback sections used.")
        return fallback, warnings
    return spec, warnings


def _render_metrics(items: list[dict]) -> str:
    if not items:
        return ""
    cards = []
    for item in items[:20]:
        label = html.escape(str(item.get("label", "")))
        value = html.escape(str(item.get("value", "")))
        unit = html.escape(str(item.get("unit", "")))
        change = item.get("change_pct")
        trend = str(item.get("trend", "unknown"))
        change_html = ""
        if change is not None:
            cls = "gg-change-down" if trend == "down" else "gg-change-up" if trend == "up" else ""
            change_html = f'<div class="gg-metric-change {cls}">{html.escape(str(change))}%</div>'
        cards.append(
            f'<article class="gg-metric"><div class="gg-metric-value">{value}{(" " + unit) if unit else ""}</div>'
            f'<div class="gg-metric-label">{label}</div>{change_html}</article>'
        )
    return f'<div class="gg-block gg-metrics">{"".join(cards)}</div>'


def _render_table_block(block: dict) -> str:
    columns = block.get("columns") or []
    rows = block.get("rows") or []
    if not columns or not rows:
        return ""
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>" for row in rows[:500])
    title = html.escape(str(block.get("title") or ""))
    return f'<div class="gg-block gg-table-wrap">{f"<div class=\"gg-chart-title\">{title}</div>" if title else ""}<div class="gg-table-scroll"><table class="gg-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></div>'


def _render_timeline_block(block: dict) -> str:
    events = block.get("events") or []
    if not events:
        return ""
    items = []
    for event in events[:200]:
        items.append(
            '<article class="gg-block gg-timeline-item">'
            f'<div class="gg-timeline-date">{html.escape(str(event.get("date_label", "")))}</div>'
            f'<div><h3 class="gg-timeline-title">{html.escape(str(event.get("title", "")))}</h3>'
            f'<p class="gg-timeline-desc">{html.escape(str(event.get("description", "")))}</p></div></article>'
        )
    title = html.escape(str(block.get("title") or ""))
    return f'<div class="gg-block gg-timeline">{f"<h3>{title}</h3>" if title else ""}{"".join(items)}</div>'


def _render_comparison_block(block: dict) -> str:
    items = block.get("items") or []
    dims = block.get("dimensions") or []
    if not items:
        return ""
    columns = dims or sorted({k for item in items for k in (item.get("values") or {}).keys()})
    if not columns:
        return ""
    rows = []
    for item in items[:100]:
        vals = item.get("values") or {}
        rows.append("<tr>" + html.escape(str(item.get("name", ""))) + "".join(f"<td>{html.escape(str(vals.get(c, "")))}</td>" for c in columns) + "</tr>")
    header = "".join(f"<th>{html.escape(str(c))}</th>" for c in ["Item"] + columns)
    fixed_rows = []
    for item in items[:100]:
        vals = item.get("values") or {}
        fixed_rows.append("<tr><td>" + html.escape(str(item.get("name", ""))) + "</td>" + "".join(f"<td>{html.escape(str(vals.get(c, "")))}</td>" for c in columns) + "</tr>")
    return f'<div class="gg-block gg-table-wrap"><div class="gg-table-scroll"><table class="gg-table"><thead><tr>{header}</tr></thead><tbody>{"".join(fixed_rows)}</tbody></table></div></div>'


def _render_risk_block(block: dict) -> str:
    risks = block.get("risks") or []
    if not risks:
        return ""
    cards = []
    for risk in risks[:200]:
        cards.append(
            '<article class="gg-risk">'
            f'<div class="gg-risk__name">{html.escape(str(risk.get("name", "")))}</div>'
            f'<div class="gg-risk__meta">Likelihood: {html.escape(str(risk.get("likelihood", "medium")))} · Impact: {html.escape(str(risk.get("impact", "medium")))}</div>'
            f'<div class="gg-risk__mitigation">{html.escape(str(risk.get("mitigation", "")))}</div>'
            '</article>'
        )
    return f'<div class="gg-block gg-risk-grid">{"".join(cards)}</div>'


def _render_evidence_block(block: dict) -> str:
    body = html.escape(str(block.get("body", "")))
    if not body:
        return ""
    tone = str(block.get("tone", "info"))
    heading = html.escape(str(block.get("heading", "")))
    source = html.escape(str(block.get("source_label", "")))
    return (
        f'<aside class="gg-block gg-callout gg-callout--{html.escape(tone, quote=True)}">'
        f'{f"<h4>{heading}</h4>" if heading else ""}<p>{body}</p>'
        f'{f"<div class=\"gg-source-meta\">{source}</div>" if source else ""}</aside>'
    )


def _render_bullets_block(block: dict) -> str:
    items = block.get("items") or block.get("bullets") or []
    if not items:
        return ""
    lis = []
    for item in items[:200]:
        if isinstance(item, dict):
            text = item.get("text") or item.get("label") or item.get("body") or ""
        else:
            text = item
        text = html.escape(str(text))
        if text:
            lis.append(f'<li>{text}</li>')
    if not lis:
        return ""
    return '<div class="gg-block gg-bullets"><ul class="gg-list">' + "".join(lis) + '</ul></div>'


def _render_chart_spec_block(block: dict, idx: int, theme: dict | None = None) -> str:
    chart_type = str(block.get("chart_type") or "line")
    labels = block.get("x_labels") or []
    series = block.get("series") or []
    if not labels or not series:
        return ""
    chart = {"type": "bar" if chart_type == "area" else chart_type, "title": block.get("title", "")}
    converted = []
    for series_item in series[:10]:
        vals = series_item.get("values") or []
        converted.append({"name": series_item.get("name", "Series"), "data": [{"label": str(label), "value": vals[i] if i < len(vals) else 0} for i, label in enumerate(labels)]})
    chart["series"] = converted
    return _render_chart_block(chart, idx, theme)


def _render_structured_blocks(blocks: list, charts: list, key_stats: list, theme: dict | None, chart_cursor: int) -> tuple[str, int, bool]:
    rendered = []
    meaningful = False
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "")).lower()
        html_block = ""
        if kind == "metrics":
            items = raw.get("items") or []
            if items:
                html_block = _render_metrics(items)
            elif key_stats:
                html_block = _render_key_stats(key_stats)
        elif kind == "prose" and raw.get("body"):
            html_block = f'<div class="gg-block">{_markdown_to_html(str(raw.get("body")), charts, [], theme)}</div>'
        elif kind == "bullets":
            html_block = _render_bullets_block(raw)
        elif kind == "table":
            html_block = _render_table_block(raw)
        elif kind == "timeline":
            html_block = _render_timeline_block(raw)
        elif kind == "comparison":
            html_block = _render_comparison_block(raw)
        elif kind == "risk_matrix":
            html_block = _render_risk_block(raw)
        elif kind == "evidence":
            html_block = _render_evidence_block(raw)
        elif kind == "chart":
            html_block = _render_chart_spec_block(raw, chart_cursor + 1, theme)
            if html_block:
                chart_cursor += 1
        if html_block:
            meaningful = True
            rendered.append(html_block)
    return "\n".join(rendered), chart_cursor, meaningful


def _match_planned_sections(spec: ReportPresentationSpec, actual: list[dict]) -> list[tuple[dict, dict]]:
    planned = sorted(spec.to_dict().get("sections", []), key=lambda s: int(s.get("order", 0)))
    remaining = list(range(len(actual)))
    matches: list[tuple[dict, dict]] = []
    for plan in planned:
        title = _slug_text(plan.get("title"))
        chosen = None
        if title:
            for idx in remaining:
                candidate = _slug_text(actual[idx].get("title"))
                if candidate == title or (title in candidate or candidate in title):
                    chosen = idx
                    break
        if chosen is None and remaining:
            chosen = remaining[0]
        if chosen is not None:
            remaining.remove(chosen)
            matches.append((plan, actual[chosen]))
        else:
            matches.append((plan, {"title": plan.get("title") or "Section", "body": ""}))
    # Preserve any unplanned material rather than silently dropping generated research.
    for idx in remaining:
        extra = actual[idx]
        matches.append(({
            "id": f"overflow-{idx + 1}", "title": extra.get("title") or "Additional Analysis",
            "section_type": "narrative", "layout": spec.to_dict().get("default_layout", "single_column"),
            "density": spec.to_dict().get("default_density", "standard"), "emphasis": "normal", "order": 1000 + idx, "blocks": [],
        }, extra))
    return matches


def _render_sources_appendix(sources: object, placement: str = "end_of_report", include_appendix: bool = False) -> str:
    manifest = normalise_source_manifest(sources)
    if not manifest or placement == "none":
        return ""
    cards: list[str] = []
    for index, source in enumerate(manifest, start=1):
        title = html.escape(source["title"])
        publisher = html.escape(source.get("publisher") or source.get("kind") or "Source")
        kind = html.escape(source.get("kind") or "Source")
        url = _safe_plain_url(source.get("url"))
        link = (
            f'<a class="gg-source-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>'
            if url else '<span class="gg-source-link">Provided in report data</span>'
        )
        cards.append(
            f'<article class="gg-source-card"><div class="gg-source-number">{index}</div>'
            f'<div><div class="gg-source-title">{title}</div><div class="gg-source-meta">{publisher} · {kind}</div>{link}</div></article>'
        )
    count = len(manifest)
    heading = "Appendix — Data Sources" if placement == "appendix" or include_appendix else "Data Sources"
    return (
        f'<section class="gg-sources gg-reveal" data-reveal data-source-placement="{html.escape(placement, quote=True)}">'
        f'<h2>{heading}</h2>'
        f'<p class="gg-sources-intro">{count} source{"s" if count != 1 else ""} used or supplied for this report.</p>'
        f'<div class="gg-sources-grid">{"".join(cards)}</div></section>'
    )


def _render_cover(spec_dict: dict, safe_title: str, safe_summary: str, date_str: str, key_stats: list) -> str:
    cover = spec_dict.get("cover") or {}
    if not cover.get("enabled", True):
        return ""
    treatment = str(cover.get("treatment") or "minimal")
    title = html.escape(str(cover.get("title") or "")) or safe_title
    subtitle = html.escape(str(cover.get("subtitle") or ""))
    show_date = bool(cover.get("show_date", True))
    aside = ""
    if treatment == "data_driven" and key_stats:
        first = key_stats[0] if isinstance(key_stats[0], dict) else {}
        aside = (
            '<aside class="gg-cover__aside"><p class="gg-cover__aside-label">Key signal</p>'
            f'<div class="gg-metric-value">{html.escape(str(first.get("value", "")))}</div>'
            f'<div class="gg-metric-label">{html.escape(str(first.get("label", "")))}</div></aside>'
        )
    return (
        f'<header class="gg-cover gg-cover--{html.escape(treatment, quote=True)}">'
        '<div><p class="gg-eyebrow">Growth Gradual · Research Intelligence</p>'
        f'<h1 class="gg-title">{title}</h1>'
        f'{f"<p class=\"gg-summary\">{subtitle or safe_summary}</p>" if (subtitle or safe_summary) else ""}'
        f'{f"<div class=\"gg-date\">Generated {date_str}</div>" if show_date else ""}</div>'
        f'{aside}</header>'
    )


def build_html_report(report: str, title: str, question: str, summary: str,
                       key_stats: list, charts: list, images: list,
                       theme: dict | None = None, sources: object = None,
                       presentation: object = None) -> str:
    spec, warnings = _normalise_presentation(presentation, report, title, summary, key_stats, charts)
    if warnings:
        log.info("HTML report: presentation normalization: %s", warnings)
    spec_dict = spec.to_dict()
    # Presentation is the composition contract. Its validated visual direction
    # is merged into legacy theme tokens so all renderer primitives can respond
    # to the model-selected identity without domain-specific runtime rules.
    selected_layouts = {str(x.get("layout") or "single_column") for x in spec_dict.get("sections") or []}
    composition_class = "gg-composition--" + ("hybrid" if len(selected_layouts) > 1 else (next(iter(selected_layouts), "single_column")))
    effective_theme = _merge_presentation_visual(theme, spec)
    safe_title = html.escape(title or question or "Research Report")
    safe_summary = html.escape(summary or "")
    date_str = datetime.now(timezone.utc).strftime("%d %B %Y")
    css = _build_css(effective_theme)
    visual_mode = str((spec_dict.get("visual") or {}).get("mode") or "light")
    visual_scale = str((spec_dict.get("visual") or {}).get("typography_scale") or "balanced")
    visual_shape = str((spec_dict.get("visual") or {}).get("shape_style") or "soft")
    visual_spacing = str((spec_dict.get("visual") or {}).get("spacing_scale") or "balanced")
    visual_card = str((spec_dict.get("visual") or {}).get("card_style") or "outlined")
    visual_rule = str((spec_dict.get("visual") or {}).get("section_rule") or "hairline")
    visual_align = str((spec_dict.get("visual") or {}).get("title_alignment") or "left")
    visual_bg = str((spec_dict.get("visual") or {}).get("background_treatment") or "plain")
    # theme.customCss (if the writer LLM set it) is emitted too — see
    # _build_css below — but only after routes.report._sanitize_theme has
    # already stripped anything that could escape the <style> block.
    font_link = ""
    actual_sections = _split_report_into_sections(report)
    matches = _match_planned_sections(spec, actual_sections)

    exec_spec = spec_dict.get("executive_summary") or {}
    exec_placement = str(exec_spec.get("placement") or "none")
    # Reports no longer reserve a standalone cover page. If the planner chose
    # the historical "after_cover" placement, keep its composition intent but
    # move the summary to the top of the actual report body.
    if exec_placement == "after_cover":
        exec_placement = "top_of_body"
    exec_body = str(exec_spec.get("body") or summary or "").strip()
    # Do not automatically inject KPIs into every executive summary. Metrics are
    # a presentation choice now; only render them here when the validated plan
    # explicitly places metric blocks in the executive summary.
    exec_metrics = exec_spec.get("key_metrics") or []

    summary_html = ""
    if exec_placement != "none" and exec_body:
        summary_cls = "gg-summary-wrap gg-summary-wrap--sidebar" if exec_placement == "sidebar" else "gg-summary-wrap--end" if exec_placement == "end_summary" else "gg-summary-wrap"
        body = _markdown_to_html(exec_body, [], [], effective_theme)
        metrics = _render_metrics(exec_metrics)
        summary_html = f'<section class="{summary_cls}"><div class="gg-summary-card gg-summary-card--high"><h2 class="gg-summary-heading">{html.escape(str(exec_spec.get("heading") or "Executive Summary"))}</h2>{body}{metrics}</div></section>'

    body_sections = []
    chart_cursor = 0
    key_stats_rendered = False
    for idx, (plan, actual) in enumerate(matches):
        section_title = html.escape(str(plan.get("title") or actual.get("title") or f"Section {idx + 1}"))
        layout = str(plan.get("layout") or spec_dict.get("default_layout") or "single_column")
        density = str(plan.get("density") or spec_dict.get("default_density") or "standard")
        emphasis = str(plan.get("emphasis") or "normal")
        section_type = str(plan.get("section_type") or "narrative")
        body_raw = str(actual.get("body") or "").strip()
        block_html, chart_cursor, blocks_meaningful = _render_structured_blocks(plan.get("blocks") or [], charts, key_stats, effective_theme, chart_cursor)
        if not blocks_meaningful:
            body_html = _markdown_to_html(body_raw, charts, images, effective_theme) if body_raw else ""
        else:
            body_html = block_html
            # Keep generated markdown content as the source-of-truth when a structured layout is only metadata.
            if body_raw and not block_html.strip():
                body_html = _markdown_to_html(body_raw, charts, images, effective_theme)
        if section_type == "metrics_dashboard" and key_stats and not key_stats_rendered and "gg-metric" not in body_html:
            body_html = _render_key_stats(key_stats) + body_html
            key_stats_rendered = True

        # A presentation plan can contain more sections than the generated
        # markdown (for example, a model may plan an optional "Outlook"
        # section that was never actually written). Rendering those empty
        # plans creates pages containing only headings, which is especially
        # harmful in print/PDF output. Only emit a planned section when it has
        # real body content or a real structured block.
        if not body_html.strip():
            log.info("HTML report: skipping empty planned section '%s'", section_title)
            continue
        if section_type == "timeline" and not blocks_meaningful:
            # Markdown remains the content source; presentation controls the arrangement.
            pass
        body_sections.append(
            f'<section id="{html.escape(str(plan.get("id") or f"section-{idx + 1}"), quote=True)}" class="gg-section gg-reveal gg-section--{html.escape(layout, quote=True)} gg-section--{html.escape(emphasis, quote=True)} gg-section-type--{html.escape(section_type, quote=True)}" data-layout="{html.escape(layout, quote=True)}" data-density="{html.escape(density, quote=True)}" data-emphasis="{html.escape(emphasis, quote=True)}" data-section-type="{html.escape(section_type, quote=True)}" data-reveal>'
            f'<div class="gg-section-heading"><h2>{section_title}</h2></div>'
            f'<div class="gg-section-body gg-section-body--{html.escape(layout, quote=True)}">{body_html}</div></section>'
        )

    source_spec = spec_dict.get("source_appendix") or {}
    sources_html = _render_sources_appendix(sources, str(source_spec.get("placement") or "end_of_report"), bool(source_spec.get("include_appendix")))
    placement = str(source_spec.get("placement") or "end_of_report")
    if placement == "none":
        sources_html = ""

    if exec_placement in ("after_cover", "top_of_body"):
        opening_html = summary_html
        closing_summary = ""
    elif exec_placement == "end_summary":
        opening_html = ""
        closing_summary = summary_html
    elif exec_placement == "sidebar":
        opening_html = summary_html
        closing_summary = ""
    else:
        opening_html = ""
        closing_summary = ""

    # No standalone cover page: the report begins with the planner-selected
    # executive summary/first section. The title remains available in the HTML
    # document metadata and the generated section headings provide the visual
    # entry point without wasting a full A4 page.
    cover_html = ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{safe_title} — Growth Gradual</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
{font_link}
<style>{css}</style>
</head>
<body class="gg-visual--{html.escape(visual_mode, quote=True)} gg-type-scale--{html.escape(visual_scale, quote=True)} gg-shape--{html.escape(visual_shape, quote=True)} gg-spacing--{html.escape(visual_spacing, quote=True)} gg-card--{html.escape(visual_card, quote=True)} gg-rule--{html.escape(visual_rule, quote=True)} gg-align--{html.escape(visual_align, quote=True)} gg-bg--{html.escape(visual_bg, quote=True)}">
{cover_html}
<main class="{composition_class}">
  {opening_html}
  <div class="gg-report-sections">{"".join(body_sections)}</div>
  {closing_summary}
  {sources_html}
</main>
<footer class="gg-footer">Growth Gradual — Research Intelligence</footer>
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
    theme: dict | None = _sanitize_theme(body.get("theme"))
    sources = normalise_source_manifest(body.get("sources", []))
    presentation = body.get("presentation")

    # Same unwrap-double-encoded-JSON safety net as routes/pdf.py.
    stripped = report.strip()
    if stripped.startswith("{") and '"report"' in stripped:
        try:
            inner = json.loads(stripped, strict=False)
            if isinstance(inner.get("report"), str) and len(inner["report"]) > 100:
                report = inner["report"]
                title = title or inner.get("title", "")
                summary = summary or inner.get("summary", "")
                key_stats = key_stats or inner.get("keyStats", [])
                charts = charts or inner.get("charts", [])
                theme = theme or _sanitize_theme(inner.get("theme"))
                presentation = presentation or inner.get("presentation")
        except Exception as e:
            log.debug("HTML report: report field is not double-encoded JSON, using as-is (%s)", e)

    if "\\n" in report:
        report = report.replace("\\n", "\n")
    report = re.sub(r"^```(?:json|markdown)?\s*", "", report.strip())
    report = re.sub(r"```\s*$", "", report).strip()

    try:
        html_doc = build_html_report(report, title, question, summary, key_stats, charts, images, theme, sources, presentation)
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
