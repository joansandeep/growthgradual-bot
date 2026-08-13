"""
Datawrapper API client.

Turns our internal chart-spec format:
    { "type": "bar" | "line" | "pie", "title": str, "unit": str,
      "series": [ { "name": str, "data": [ {"label": str, "value": number}, ... ] } ] }

into a live, published Datawrapper chart, and returns the bits the frontend /
PDF builder need:
    {
      "id": "abc123",
      "embedUrl": "https://datawrapper.dwcdn.net/abc123/1/",   # use in an <iframe>
      "publicUrl": "https://www.datawrapper.de/_/abc123/",     # human-viewable link
      "pngUrl": "https://api.datawrapper.de/v3/charts/abc123/export/png?...",
      "pngBytes": b"..."  # only populated when fetch_png=True (used by the PDF route)
    }

If DATAWRAPPER_API_TOKEN isn't set, or any call fails, every function here
degrades to returning None so callers can fall back to the existing
hand-drawn charts instead of breaking the report.
"""
import asyncio
import csv
import io
import logging
import os
import re

import httpx

log = logging.getLogger("datawrapper")

API_BASE = "https://api.datawrapper.de/v3"
TOKEN = os.environ.get("DATAWRAPPER_API_TOKEN", "")

# How many charts we'll publish to Datawrapper concurrently per report.
# Keep this modest — reports normally have 2-4 charts.
_CONCURRENCY = 4


def _headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {TOKEN}"}
    if extra:
        h.update(extra)
    return h


# Full reference: https://developer.datawrapper.de/docs/chart-types
# (only the IDs that make sense for our label/value chart-spec shape are used below)
_TEMPORAL_RE = re.compile(
    r"^(?:(19|20)\d{2}|Q[1-4]\s?'?\d{0,4}|FY\s?\d{2,4}|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
    re.IGNORECASE,
)


def _labels_look_temporal(charts_labels: list[str]) -> bool:
    """True if most labels look like years/quarters/months (a chart read left-to-right over time)."""
    if not charts_labels:
        return False
    hits = sum(1 for lbl in charts_labels if _TEMPORAL_RE.match(str(lbl).strip()))
    return hits >= max(1, len(charts_labels) * 0.6)


def _looks_like_composition(series: list[dict]) -> bool:
    """True if, for most labels, the series values sum to ~100 — i.e. parts of a whole (good for stacking)."""
    by_label: dict[str, float] = {}
    for s in series:
        for pt in s.get("data") or []:
            lbl = str(pt.get("label", ""))
            try:
                by_label[lbl] = by_label.get(lbl, 0.0) + abs(float(pt.get("value", 0)))
            except (TypeError, ValueError) as e:
                log.debug("Composition check: skipping non-numeric value %r (%s)", pt.get("value"), e)
    if not by_label:
        return False
    near100 = sum(1 for total in by_label.values() if 90 <= total <= 110)
    return near100 >= max(1, len(by_label) * 0.6)


def _dw_type(spec: dict) -> str:
    """
    Pick the best-fitting Datawrapper chart type for this spec, rather than a
    fixed bar/line/pie -> single-id mapping. Chooses from chart types that
    map cleanly onto our label/value series shape.
    """
    chart_type = spec.get("type", "bar")
    series = spec.get("series") or []
    n_series = len(series)
    all_labels = [pt.get("label", "") for s in series for pt in (s.get("data") or [])]
    temporal = _labels_look_temporal(all_labels)

    if chart_type == "table":
        return "tables"

    if chart_type == "scatter":
        # Two independent numeric metrics per entity (e.g. valuation score
        # vs. 1-yr return) — our 2-series-sharing-labels shape maps directly
        # onto Datawrapper's scatter plot's (x, y) columns once merged by
        # label in _spec_to_csv, same as any other multi-series chart.
        return "d3-scatter-plot"

    if chart_type == "arrow":
        # A single metric that moved from one value to another for the same
        # named entities (guidance revisions, price-target changes, before/
        # after any number) — represented the same way: two series
        # ("Previous"/"Current") sharing labels, which is exactly the shape
        # Datawrapper's arrow/range plot expects (start column, end column).
        return "d3-arrow-plot"

    if chart_type == "pie":
        n_slices = len((series[0].get("data") or [])) if series else 0
        # A 2-slice pie reads better as a donut (emphasises one share vs. the rest)
        return "d3-donuts" if n_slices <= 2 else "d3-pies"

    if chart_type == "line":
        max_pts = max((len(s.get("data") or []) for s in series), default=0)
        # A single long trend reads better filled in (area) than a bare line
        if n_series == 1 and max_pts >= 6:
            return "d3-area"
        return "d3-lines"

    # bar
    if n_series > 1:
        if _looks_like_composition(series):
            return "stacked-column-chart" if temporal else "d3-bars-stacked"
        # Grouped comparison (e.g. revenue vs. profit per company/quarter)
        return "grouped-column-chart"
    # Single series — prefer vertical columns for up to 8 items (reads better in PDF);
    # only use horizontal bars (d3-bars) for longer label lists (>8 items) where
    # rotated x-axis labels would be unreadable.
    all_labels_first_series = [(pt.get("label") or "") for pt in ((series[0].get("data") or []) if series else [])]
    n_items = len(all_labels_first_series)
    if temporal or n_items <= 8:
        return "column-chart"
    return "d3-bars"


def _clean_label(text) -> str:
    """Strip markdown emphasis markers (**bold**, *italic*, `code`) from any
    text that ends up as a Datawrapper CSV value — labels, series names, and
    table cells. The LLM writes chart specs in the same voice it writes the
    report body in, so it sometimes emphasises a label ("**Software / AI
    SaaS**") the same way it would in prose; the PDF's own text renderer
    strips that via _strip_inline, but nothing was doing the equivalent for
    text that flows into a chart instead, so the raw ** markers were showing
    up literally in the published chart image."""
    if not isinstance(text, str):
        return text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def _format_table_cell(raw_value, column_header: str):
    """Datawrapper's Tables chart type auto-detects "number-looking" columns
    and applies its OWN default number format — which, left unset, rounds to
    whole numbers and drops the +/- sign. That's exactly why a genuine
    "-2.12%" or "+26.04%" value was showing up as a bare "-2" / "26" in
    published reports even though the source data had full precision.
    Fix: turn any raw numeric cell in a %/points/price-flavoured column into
    an already-formatted STRING (sign + fixed decimals + unit suffix) here,
    before it ever reaches Datawrapper, so there is no ambiguous number left
    for Datawrapper to reformat — a pre-formatted string just gets printed
    verbatim as text. Cells that already arrived as formatted strings (the
    model wrote "-2.12%" directly) are left untouched.
    """
    if raw_value is None:
        return ""
    if isinstance(raw_value, str):
        return _clean_label(raw_value)  # already text — trust the model's own formatting (minus markdown)
    if not isinstance(raw_value, (int, float)):
        return raw_value

    header_low = (column_header or "").lower()
    is_pct = "%" in header_low or "percent" in header_low
    # "Change"/"delta"-style columns (points, ₹, etc.) read better with an
    # explicit +/- sign even when not a percentage.
    is_signed_delta = any(w in header_low for w in ("change", "delta", "chg", "mom", "yoy", "qoq"))

    if is_pct:
        return f"{raw_value:+.2f}%"
    if is_signed_delta:
        # Preserve decimals if the source actually had them (e.g. 14.68 →
        # "+14.68"), otherwise keep it a clean signed integer (e.g. -1,677).
        if float(raw_value).is_integer():
            return f"{raw_value:+,.0f}"
        return f"{raw_value:+,.2f}"
    # Non-delta numeric column (closing level, price) — just keep full
    # precision with thousands separators, no forced rounding to 0 decimals.
    if float(raw_value).is_integer():
        return f"{raw_value:,.0f}"
    return f"{raw_value:,.2f}"


def _spec_to_csv(spec: dict) -> str:
    """Build the CSV Datawrapper expects from our chart-spec shape."""
    chart_type = spec.get("type", "bar")

    buf = io.StringIO()
    writer = csv.writer(buf)

    if chart_type == "table":
        columns = [_clean_label(c) for c in (spec.get("columns") or [])]
        rows = spec.get("rows") or []
        writer.writerow(columns)
        for row in rows:
            formatted_row = [
                _format_table_cell(_clean_label(cell) if isinstance(cell, str) else cell,
                                    columns[ci] if ci < len(columns) else "")
                for ci, cell in enumerate(row)
            ]
            writer.writerow(formatted_row)
        return buf.getvalue()

    series = spec.get("series") or []

    if chart_type == "pie" or len(series) == 1:
        s = series[0] if series else {"data": []}
        name = _clean_label(s.get("name") or spec.get("unit") or "Value")
        writer.writerow(["Label", name])
        for pt in s.get("data") or []:
            writer.writerow([_clean_label(pt.get("label", "")), pt.get("value", "")])
        return buf.getvalue()

    # Multi-series: merge on label, preserving first-seen order.
    labels: list[str] = []
    seen = set()
    for s in series:
        for pt in s.get("data") or []:
            lbl = _clean_label(pt.get("label", ""))
            if lbl not in seen:
                seen.add(lbl)
                labels.append(lbl)

    lookups = []
    for s in series:
        lookups.append({_clean_label(pt.get("label", "")): pt.get("value", "") for pt in (s.get("data") or [])})

    header = ["Label"] + [_clean_label(s.get("name") or f"Series {i+1}") for i, s in enumerate(series)]
    writer.writerow(header)
    for lbl in labels:
        row = [lbl] + [lookups[i].get(lbl, "") for i in range(len(series))]
        writer.writerow(row)

    return buf.getvalue()


def _export_dims(dw_type: str) -> tuple[str, str]:
    """Pick a PNG export width/height suited to the chart's actual shape,
    instead of forcing every Datawrapper visualization into the same fixed
    900x500 box.

    - Tables grow with row count. A fixed height of 500px crops off any
      rows that don't fit in that height — the table chart silently loses
      data in the exported PNG. Requesting height="auto" (a documented
      value for the export endpoint's height param) lets Datawrapper size
      the export to the actual number of rows instead.
    - Pie/donut charts are circular and read poorly in a wide 900x500
      frame — most of the canvas ends up as side-margin instead of chart.
      A near-square frame uses the space properly.
    - Column/bar/line/area charts already work well at 900x500 (the shape
      this constant was originally tuned for) — leave them as-is.
    """
    if dw_type in ("tables", "d3-arrow-plot"):
        # Tables grow with row count — see the tables case above.
        # Arrow/range-plots draw one horizontal row per category the same
        # way tables draw one row per record — a 2-3 row arrow-plot in a
        # fixed 500px box only fills the top ~15% of the frame, reading as
        # a mostly-blank chart (seen repeatedly in production: "Shift in
        # 2026 Market Outlook", "Change in Average SME IPO Issue Size" —
        # both 3-row arrow-plots rendering as a few marks floating in a
        # sea of white space). "auto" lets Datawrapper size the export to
        # the actual row count instead of a fixed aspect meant for
        # column/bar/line charts.
        return "900", "auto"
    if dw_type in ("d3-pies", "d3-donuts"):
        return "700", "650"
    return "900", "500"


def _export_query(dw_type: str) -> str:
    """Build the export query string for `dw_type`, used for both the
    pngUrl handed to the frontend/PDF route and the actual fetch below —
    keeping both in sync so a chart's stored pngUrl always matches what
    fetch_png actually requests."""
    width, height = _export_dims(dw_type)
    # plain=false keeps the title/subtitle/notes frame baked into the PNG so
    # the PDF card shows a complete labelled chart without a separate
    # ReportLab title bar. scale + zoom both request 2x pixel density —
    # Datawrapper's docs list both as valid, independent export params, so
    # setting both is the safest way to guarantee a retina-quality export.
    return f"unit=px&width={width}&height={height}&plain=false&scale=2&zoom=2"


def _axis_intro(spec: dict) -> str:
    """Build the chart subtitle from the spec's xLabel/yLabel, since bar,
    column, and line charts on Datawrapper have no separate axis-title
    fields to put this in (see the comment in publish_chart above). Skips a
    label that's empty or that just repeats the unit/title, so the subtitle
    never reads as a redundant echo of the title bar right above it."""
    dw_type = _dw_type(spec)
    if dw_type in ("d3-pies", "d3-donuts", "tables"):
        return ""  # axes don't apply to these chart types
    x_label = _clean_label(str(spec.get("xLabel") or "").strip())
    y_label = _clean_label(str(spec.get("yLabel") or "").strip())
    title_lower = str(spec.get("title") or "").lower()
    parts = []
    if y_label and y_label.lower() not in title_lower:
        parts.append(y_label)
    if x_label and x_label.lower() not in title_lower and x_label.lower() != y_label.lower():
        parts.append(f"by {x_label}")
    return " ".join(parts)


async def publish_chart(client: httpx.AsyncClient, spec: dict) -> dict | None:
    """Create, fill, and publish a single Datawrapper chart from a chart-spec dict."""
    if not TOKEN:
        return None

    try:
        dw_type = _dw_type(spec)
        title = _clean_label(spec.get("title") or "Chart")

        create = await client.post(
            f"{API_BASE}/charts",
            headers=_headers({"Content-Type": "application/json"}),
            json={"title": title, "type": dw_type},
        )
        create.raise_for_status()
        chart_id = create.json()["id"]

        csv_data = _spec_to_csv(spec)
        data_resp = await client.put(
            f"{API_BASE}/charts/{chart_id}/data",
            headers=_headers({"Content-Type": "text/csv"}),
            content=csv_data.encode("utf-8"),
        )
        data_resp.raise_for_status()

        unit = spec.get("unit") or ""
        # Build color palette — Growth Gradual brand colors
        series_list = spec.get("series") or []
        brand_colors = ["#1a1f4e", "#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899"]
        GREEN, RED_HEX = "#178a4c", "#c0392b"
        custom_colors: dict = {}
        # Single-series bar/column charts (the common case for "gainers vs.
        # losers" tables like this report's stock/index comparisons) used to
        # get ONE flat colour for every bar, keyed by the series name — e.g.
        # every bar in "Key Indian Stock and Sectoral Performance" painted
        # the same navy regardless of whether the value was +1.50% or
        # -0.23%. That made every bar/column chart in a report look like a
        # copy of the last one (same silhouette, same single colour) even
        # though the underlying data differed — the "charts keep repeating"
        # symptom. Fix: for a single-series chart whose values are mixed
        # sign, colour each bar by its own sign (green = gain, red = loss)
        # via a per-ROW custom-colors entry (Datawrapper keys custom-colors
        # by row label when there is exactly one data series), instead of
        # one colour for the whole series.
        if len(series_list) == 1:
            pts = (series_list[0].get("data") or [])
            vals = []
            for pt in pts:
                try:
                    vals.append(float(pt.get("value", 0)))
                except (TypeError, ValueError):
                    vals.append(0.0)
            mixed_sign = any(v > 0 for v in vals) and any(v < 0 for v in vals)
            if mixed_sign:
                for pt, v in zip(pts, vals):
                    lbl = str(pt.get("label", ""))
                    custom_colors[lbl] = GREEN if v > 0 else (RED_HEX if v < 0 else brand_colors[0])
            else:
                custom_colors[series_list[0].get("name") or "Value"] = brand_colors[0]
        else:
            for i, s in enumerate(series_list):
                s_name = s.get("name", f"Series {i+1}")
                custom_colors[s_name] = brand_colors[i % len(brand_colors)]
            # Also index-based fallback
            for i in range(8):
                custom_colors[str(i)] = brand_colors[i % len(brand_colors)]

        metadata: dict = {
            "describe": {
                "source-name": "Growth Gradual",
                "source-url": "https://growth-gradual.com",
                # Bar/column/line charts on Datawrapper have no literal
                # axis-title text option at all (that's a Datawrapper
                # platform limitation, confirmed on their own Academy docs —
                # it's only available for scatter plots). The chart spec's
                # xLabel/yLabel were being generated by the LLM on every
                # chart and then silently dropped here, so the report's PNG
                # charts had no indication of what the axes represented
                # beyond the bare title. The "intro" field IS rendered (as a
                # subtitle directly under the chart title) for every chart
                # type, so that's where axis meaning goes instead.
                "intro": _axis_intro(spec),
            },
            "annotate": {"notes": ""},
            "visualize": {
                "custom-colors": custom_colors,
                "base-color": "#1a1f4e",
                "label-colors": True,
                # NOTE on value labels: "value-labels" (below) is NOT a real
                # Datawrapper metadata key for bar/column charts — it's a
                # no-op the API silently ignores, which is why every bar in
                # every published chart in this report was missing its
                # number. The actual key (confirmed from Datawrapper's own
                # chart-properties payloads) is "value-label-visibility",
                # and its default is "hover" — i.e. the label ONLY appears
                # on mouse hover in the live embed, and is therefore absent
                # from a static PNG export entirely. That's the exact
                # mechanism behind the missing-labels bug: the charts were
                # real Datawrapper renders, they just never had their
                # labels switched from "hover" to "show". We set both the
                # legacy key (harmless if ignored) and the real one.
                "value-labels": True,
                "value-label-visibility": "show",
                "show-value-labels": True,
            },
        }
        if dw_type in ("d3-bars", "d3-bars-stacked", "column-chart", "grouped-column-chart",
                       "stacked-column-chart", "d3-lines", "d3-area"):
            metadata["visualize"].update({
                "y-grid": "on",
                "tooltip-number-format": f"0,0.0{('a ' + unit) if unit else ''}".strip(),
            })
        if dw_type in ("d3-lines", "d3-area"):
            # Line/area charts use a different toggle family than bars —
            # "hide-value-labels" (inverse boolean), separate from the
            # bar-chart "value-label-visibility" set above.
            metadata["visualize"]["hide-value-labels"] = False
            # Datawrapper's default for line/area charts also direct-labels
            # the series NAME right at the line's endpoint (confirmed
            # Datawrapper behavior — "direct labeling... by default for
            # line charts... on desktop"). Combined with the numeric value
            # labels just enabled above, and a long x-axis category label
            # often sitting at that same last point (e.g. "₹4.25L
            # (Break-even)"), the two text elements collide into an
            # illegible overlapping mess at the right edge of the chart —
            # seen in production. The value labels already carry the data,
            # and a single-series chart's own title already says what it
            # is, so the endpoint name label is redundant — turn it off.
            metadata["visualize"]["labeling"] = "none"
        if unit == "%":
            metadata["visualize"]["value-label-format"] = "0.0%"
        elif unit in ("Cr", "₹", "Rs"):
            metadata["visualize"]["value-label-format"] = "0,0"
        elif dw_type in ("d3-pies", "d3-donuts"):
            # Pie/donut charts are the one case where leaving this unset is
            # actively dangerous rather than merely suboptimal: Datawrapper's
            # own default for these chart types applies a percent-style
            # suffix to whatever raw number is in the data — NOT a genuine
            # share-of-total calculation — whenever `unit` doesn't match one
            # of the branches above. That's how a real ₹1,35,000 crore value
            # ends up rendered as the meaningless "135000.0%" instead of a
            # plain formatted number (this exact bug shipped in a published
            # report: a bar→pie auto-converted "Promoter Selling Channels"
            # chart with an unset/non-matching unit). Any non-percentage
            # unit reaching this branch (blank, "crore", "₹ Cr", etc.) gets
            # an explicit plain-number format instead of Datawrapper's
            # unpredictable default.
            metadata["visualize"]["value-label-format"] = "0,0"

        if dw_type == "tables":
            # _format_table_cell() (in _spec_to_csv) already turns every raw
            # numeric cell into a correctly-signed, fixed-decimal STRING
            # ("-0.58%", "+3.32%", "23,985") before it ever reaches this CSV.
            # But Datawrapper's Tables chart type still runs its own
            # column-type auto-detection on import — it sees a "%"-suffixed,
            # numeric-looking string, decides the column is a number column
            # anyway, and re-parses + re-formats it with ITS OWN default
            # format (0 decimals, sign sometimes dropped). That silently
            # turned "-0.58%" into "-0" and "+3.32%" into "3" in published
            # reports even though the CSV cell was already correct — the
            # cell-level fix alone can't stop Datawrapper from reformatting
            # it a second time on its end.
            #
            # Fix: explicitly force every column to type "text" via
            # metadata.data.column-format (confirmed key from Datawrapper's
            # chart-properties schema — NOT metadata.visualize). A column
            # marked "text" is rendered byte-for-byte as given, so our
            # pre-formatted strings finally survive unchanged.
            table_columns = spec.get("columns") or []
            metadata["data"] = {
                "column-format": {
                    col: {"type": "text"} for col in table_columns
                }
            }
        await client.patch(
            f"{API_BASE}/charts/{chart_id}",
            headers=_headers({"Content-Type": "application/json"}),
            json={"metadata": metadata},
        )

        publish = await client.post(
            f"{API_BASE}/charts/{chart_id}/publish",
            headers=_headers(),
        )
        publish.raise_for_status()
        pub_json = publish.json()
        pub_data = pub_json.get("data", pub_json)
        public_url = pub_data.get("publicUrl") or f"https://www.datawrapper.de/_/{chart_id}/"
        version = pub_data.get("publishedVersion", 1)
        embed_url = f"https://datawrapper.dwcdn.net/{chart_id}/{version}/"

        return {
            "id": chart_id,
            "embedUrl": embed_url,
            "publicUrl": public_url,
            # Dimensions are chosen per chart type by _export_dims — see its
            # docstring (fixed 900x500 cropped table rows and squeezed
            # pies/donuts into a too-wide frame).
            "pngUrl": f"{API_BASE}/charts/{chart_id}/export/png?{_export_query(dw_type)}",
        }
    except Exception as exc:
        log.warning("Datawrapper publish failed for chart %r: %s", spec.get("title", "?"), exc)
        return None


async def fetch_png(client: httpx.AsyncClient, chart_id: str, png_url: str) -> bytes | None:
    """Download the static PNG export of an already-published chart (for the PDF).

    Datawrapper's documented export endpoint is `GET /v3/charts/{id}/export/{format}`,
    which normally renders and returns the image synchronously. (There is a
    separate `POST .../export/{format}/async` endpoint for very large/slow
    exports that returns a job to poll — we don't need it for chart-sized PNGs.)
    We still retry with increasing delays to absorb the brief propagation lag
    right after `publish_chart` publishes a new chart version, and we validate
    that the response body is actually PNG bytes rather than a transient JSON
    error Datawrapper can return while the new version is still propagating.

    `png_url` is the per-chart-type URL `publish_chart` already built (see
    `_export_query`/`_export_dims`) — reusing it here (instead of rebuilding a
    separate hardcoded URL) keeps the dimensions actually requested in sync
    with the dimensions recorded on the chart's `datawrapper` info.
    """
    if not TOKEN or not png_url:
        return None

    PNG_MAGIC = b"\x89PNG"

    try:
        # Charts were already published during report generation — the PNG should
        # be ready immediately. Use short initial delays, then back off.
        delays = [0.5, 1.0, 2.0, 3.0, 4.0, 5.0]
        for attempt, delay in enumerate(delays):
            await asyncio.sleep(delay)
            resp = await client.get(png_url, headers=_headers(), timeout=30.0)
            if resp.status_code == 200 and resp.content:
                if resp.content[:4] == PNG_MAGIC and len(resp.content) > 1024:
                    log.info("Datawrapper PNG ready for chart %s on attempt %d (%d bytes)",
                             chart_id, attempt + 1, len(resp.content))
                    return resp.content
                else:
                    log.info("Datawrapper PNG not ready yet for chart %s (attempt %d) — got: %r",
                             chart_id, attempt + 1, resp.content[:80])
            else:
                log.info("Datawrapper PNG HTTP %s for chart %s (attempt %d)",
                         resp.status_code, chart_id, attempt + 1)

        log.warning("Datawrapper PNG export never became ready for chart %s after %d attempts",
                    chart_id, len(delays))
        return None
    except Exception as exc:
        log.warning("Datawrapper PNG fetch failed for chart %s: %s", chart_id, exc)
        return None

async def attach_datawrapper_charts(charts: list[dict], fetch_png_bytes: bool = False) -> list[dict]:
    """
    Publish every chart in `charts` to Datawrapper and attach the embed info
    in-place under chart["datawrapper"]. Charts that fail just don't get the
    key, so the frontend / PDF builder fall back to the existing SVG/canvas
    renderer automatically.

    Set fetch_png_bytes=True (used by the PDF route) to also download the
    PNG export and store it under chart["datawrapper"]["pngBytes"].
    """
    if not TOKEN or not charts:
        if not TOKEN and charts:
            log.info("DATAWRAPPER_API_TOKEN not set — skipping Datawrapper charts, using fallback renderer")
        return charts

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _do_one(ch: dict, client: httpx.AsyncClient):
        async with sem:
            info = await publish_chart(client, ch)
            if not info:
                return
            if fetch_png_bytes:
                png = await fetch_png(client, info["id"], info["pngUrl"])
                if png:
                    info["pngBytes"] = png
            ch["datawrapper"] = info

    async with httpx.AsyncClient(timeout=60.0) as client:
        await asyncio.gather(*[_do_one(ch, client) for ch in charts])

    n_ok = sum(1 for ch in charts if ch.get("datawrapper"))
    log.info("Datawrapper: published %d/%d charts", n_ok, len(charts))
    return charts
