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
            except (TypeError, ValueError):
                pass
    if not by_label:
        return False
    near100 = sum(1 for total in by_label.values() if 90 <= total <= 110)
    return near100 >= max(1, len(by_label) * 0.6)


def _dw_type(spec: dict) -> str:
    """
    Pick the best-fitting Datawrapper chart type for this spec, rather than a
    fixed bar/line/pie -> single-id mapping. Only chooses from chart types that
    map cleanly onto our label/value series shape (no maps/tables/scatter,
    which need data we don't have).
    """
    chart_type = spec.get("type", "bar")
    series = spec.get("series") or []
    n_series = len(series)
    all_labels = [pt.get("label", "") for s in series for pt in (s.get("data") or [])]
    temporal = _labels_look_temporal(all_labels)

    if chart_type == "table":
        return "tables"

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
    # Single series
    return "column-chart" if temporal else "d3-bars"


def _spec_to_csv(spec: dict) -> str:
    """Build the CSV Datawrapper expects from our chart-spec shape."""
    chart_type = spec.get("type", "bar")

    buf = io.StringIO()
    writer = csv.writer(buf)

    if chart_type == "table":
        columns = spec.get("columns") or []
        rows = spec.get("rows") or []
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)
        return buf.getvalue()

    series = spec.get("series") or []

    if chart_type == "pie" or len(series) == 1:
        s = series[0] if series else {"data": []}
        name = s.get("name") or spec.get("unit") or "Value"
        writer.writerow(["Label", name])
        for pt in s.get("data") or []:
            writer.writerow([pt.get("label", ""), pt.get("value", "")])
        return buf.getvalue()

    # Multi-series: merge on label, preserving first-seen order.
    labels: list[str] = []
    seen = set()
    for s in series:
        for pt in s.get("data") or []:
            lbl = pt.get("label", "")
            if lbl not in seen:
                seen.add(lbl)
                labels.append(lbl)

    lookups = []
    for s in series:
        lookups.append({pt.get("label", ""): pt.get("value", "") for pt in (s.get("data") or [])})

    header = ["Label"] + [s.get("name") or f"Series {i+1}" for i, s in enumerate(series)]
    writer.writerow(header)
    for lbl in labels:
        row = [lbl] + [lookups[i].get(lbl, "") for i in range(len(series))]
        writer.writerow(row)

    return buf.getvalue()


async def publish_chart(client: httpx.AsyncClient, spec: dict) -> dict | None:
    """Create, fill, and publish a single Datawrapper chart from a chart-spec dict."""
    if not TOKEN:
        return None

    try:
        dw_type = _dw_type(spec)
        title = spec.get("title") or "Chart"

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
        metadata: dict = {
            "describe": {
                "source-name": "Growth Gradual",
                "intro": "",
            },
            "annotate": {
                "notes": "",
            },
            "visualize": {
                "custom-colors": {
                    "0": "#1a1f4e",
                    "1": "#3b82f6",
                    "2": "#22c55e",
                    "3": "#f59e0b",
                    "4": "#ef4444",
                    "5": "#8b5cf6",
                },
            },
        }
        if dw_type in ("d3-bars", "d3-bars-stacked", "column-chart", "grouped-column-chart",
                       "stacked-column-chart", "d3-lines", "d3-area"):
            metadata["visualize"].update({
                "y-grid": "on",
                "tooltip-number-format": f"0,0.0{('a ' + unit) if unit else ''}".strip(),
            })
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
            "pngUrl": f"{API_BASE}/charts/{chart_id}/export/png?unit=px&width=900&plain=false",
        }
    except Exception as exc:
        log.warning("Datawrapper publish failed for chart %r: %s", spec.get("title", "?"), exc)
        return None


async def fetch_png(client: httpx.AsyncClient, chart_id: str, png_url: str) -> bytes | None:
    """Download the static PNG export of an already-published chart (for the PDF)."""
    if not TOKEN:
        return None
    try:
        # Datawrapper renders the export async — small retry loop covers that.
        for attempt in range(4):
            resp = await client.get(png_url, headers=_headers())
            if resp.status_code == 200 and resp.content:
                return resp.content
            await asyncio.sleep(1.2 * (attempt + 1))
        log.warning("Datawrapper PNG export never became ready for chart %s", chart_id)
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
