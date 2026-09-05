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

# Re-validate theme here rather than trusting the client's echoed-back copy
# as-is: this route is the one place customCss gets concatenated into a real
# <style> tag, so re-running the same hex/font-name/CSS-escape checks used
# when the theme was first produced (routes/report.py) costs nothing and
# closes off a client that edits the JSON it sends back before re-download.
from routes.report import _sanitize_theme

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


def _build_css(theme: dict | None) -> str:
    """Build the report stylesheet, substituting the requested theme's
    colors for the default navy/gold brand pair when the report asked for
    one (see report.py's THEME schema field). Falls back to the standard
    Growth Gradual palette whenever no theme, or an invalid one, was given.

    Two more theme fields layer on top of the color swap for styles a flat
    re-theme can't express on its own:
      - fontFamily: swaps the whole document's typography to a Google Font
        that actually fits the requested mood (handled via CSS vars here +
        the <link> tag from _google_font_link in build_html_report).
      - customCss: a small, pre-sanitized (see report.py _sanitize_theme)
        block of extra rules the model wrote specifically for THIS request's
        style — appended last so it can add flourishes (dashed borders,
        tilted headings, sticker badges, glow effects, background patterns)
        on top of the base template without the base template needing to
        hardcode every style anyone might ask for."""
    theme = theme or {}
    navy = theme.get("primaryColor") or BRAND_NAVY
    gold = theme.get("accentColor") or BRAND_GOLD
    navy_deep = _shade_hex(navy, -0.35)
    gold_light = _shade_hex(gold, 0.35)
    font = theme.get("fontFamily")
    if font:
        font_body = f"'{font}', 'Georgia', 'Times New Roman', serif"
        font_heading = f"'{font}', 'Helvetica Neue', Arial, sans-serif"
    else:
        font_body = "'Georgia', 'Times New Roman', serif"
        font_heading = "'Helvetica Neue', Arial, sans-serif"
    css = _CSS_TEMPLATE.format(
        navy=navy, navy_deep=navy_deep, gold=gold, gold_light=gold_light,
        font_body=font_body, font_heading=font_heading,
    )
    custom_css = theme.get("customCss")
    if custom_css:
        # Already length-capped and escape-checked by _sanitize_theme before
        # it ever reaches here — appended raw as its own trailing rule block.
        css += f"\n/* --- request-specific style additions --- */\n{custom_css}\n"
    return css


_CSS_TEMPLATE = """
:root {{
  --navy: {navy}; --navy-deep: {navy_deep};
  --gold: {gold}; --gold-light: {gold_light};
  --font-body: {font_body}; --font-heading: {font_heading};
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0; font-family: var(--font-body);
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
  font-family: var(--font-heading); letter-spacing: .18em; text-transform: uppercase;
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
  font-family: var(--font-heading); font-size: 13px; color: #8890b0; margin-top: 22px;
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
.gg-stat-value {{ font-size: 26px; font-weight: 700; color: var(--gold-light); font-family: var(--font-heading); }}
.gg-stat-label {{ font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: #9aa3c0; margin-top: 6px; }}
.gg-stat-change {{ font-size: 13px; margin-top: 6px; font-weight: 600; }}
.gg-change-up {{ color: #6fcf97; }} .gg-change-down {{ color: #eb6161; }}

main {{ max-width: 880px; margin: 0 auto; padding: 10px 8vw 100px; }}
h1, h2, h3 {{ font-family: var(--font-heading); color: #fff; }}
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
  font-family: var(--font-heading); font-size: 13px; letter-spacing: .04em;
  color: var(--gold-light); text-transform: uppercase; margin-bottom: 14px;
}}
.gg-chart-canvas-box {{ position: relative; height: 320px; }}
.gg-table-scroll {{ overflow-x: auto; }}
.gg-table {{ width: 100%; border-collapse: collapse; font-family: var(--font-heading); font-size: 14px; }}
.gg-table th {{
  text-align: left; padding: 10px 12px; color: var(--gold-light); border-bottom: 2px solid rgba(212,162,76,0.35);
  font-size: 12px; letter-spacing: .04em; text-transform: uppercase;
}}
.gg-table td {{ padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #d6d9e8; }}
.gg-table tr {{ opacity: 0; animation: gg-row-in .5s ease forwards; animation-delay: calc(var(--row-i) * 60ms); }}

.gg-tm-wrap {{ display: flex; flex-direction: column; gap: 2px; height: 340px; }}
.gg-tm-wrap.gg-tm-flat {{ flex-direction: row; flex-wrap: wrap; }}
.gg-tm-row {{ display: flex; flex-direction: column; min-height: 0; }}
.gg-tm-group-label {{
  font-family: var(--font-heading); font-size: 10px; letter-spacing: .05em; text-transform: uppercase;
  color: #9aa3c0; padding: 2px 4px;
}}
.gg-tm-row-cells {{ display: flex; flex: 1; gap: 2px; min-height: 0; }}
.gg-tm-cell {{
  position: relative; display: flex; flex-direction: column; justify-content: flex-end;
  padding: 6px 8px; border-radius: 4px; min-width: 24px; overflow: hidden;
  transition: transform .25s ease; color: #fff;
}}
.gg-tm-cell:hover {{ transform: scale(1.02); z-index: 1; }}
.gg-tm-label {{ font-family: var(--font-heading); font-size: 12px; font-weight: 700; display: block; }}
.gg-tm-value {{ font-size: 11px; opacity: .9; display: block; }}

.gg-hm-wrap {{ display: flex; flex-direction: column; gap: 3px; }}
.gg-hm-row {{ display: flex; align-items: stretch; gap: 6px; }}
.gg-hm-header {{ padding-bottom: 4px; }}
.gg-hm-rowhead {{
  flex: 0 0 90px; display: flex; align-items: center; font-size: 12px; color: #9aa3c0;
  font-family: var(--font-heading);
}}
.gg-hm-cells {{ display: grid; gap: 3px; flex: 1; }}
.gg-hm-colhead {{
  text-align: center; font-family: var(--font-heading); font-size: 11px; letter-spacing: .03em;
  color: var(--gold-light); text-transform: uppercase; padding-bottom: 2px;
}}
.gg-hm-cell {{
  display: flex; align-items: center; justify-content: center; border-radius: 4px;
  min-height: 34px; font-size: 12px; font-weight: 700; color: #10131f;
}}
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
  text-align: center; padding: 40px 8vw 60px; font-family: var(--font-heading);
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
    font_link = _google_font_link((theme or {}).get("fontFamily"))

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
    theme: dict | None = _sanitize_theme(body.get("theme"))

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
