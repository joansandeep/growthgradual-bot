"""
POST /api/chat/report/okf  — Open Knowledge Format bundle from report data (binary .zip)

Replaces the PDF report download with an OKF bundle: a zip of markdown
concept files with YAML frontmatter (Google Cloud OKF v0.1 spec). Charts
link out to their live Datawrapper URL/PNG export instead of being
rasterized, since OKF concepts are meant to be lightweight, inspectable
markdown rather than print-laid-out documents.
"""
import logging
import re
import time

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response

from utils.datawrapper import attach_datawrapper_charts
from utils.okf import build_report_okf_bundle

router = APIRouter()
log = logging.getLogger("okf")


@router.post("")
async def generate_okf(request: Request):
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        log.warning("OKF: invalid request body")
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    report: str = body.get("report", "")
    if not report:
        log.warning("OKF: no report content in request")
        return JSONResponse({"error": "No report content"}, status_code=400)

    title: str = body.get("title", "")
    question: str = body.get("question", "Research Report")
    summary: str = body.get("summary", "")
    key_stats: list = body.get("keyStats", [])
    charts: list = body.get("charts", [])
    images: list = body.get("images", [])
    file_images: list = body.get("fileImages", [])
    source_documents: list = body.get("sourceDocuments", [])

    # ── Safety: unwrap double-encoded report (same defensive pattern as pdf.py) ──
    stripped = report.strip()
    if stripped.startswith("{") and '"report"' in stripped:
        try:
            import json as _json
            inner = _json.loads(stripped)
            if isinstance(inner.get("report"), str) and len(inner["report"]) > 100:
                log.warning("OKF: unwrapping double-encoded report field")
                report = inner["report"]
                title = title or inner.get("title", "")
                summary = summary or inner.get("summary", "")
                if not key_stats:
                    key_stats = inner.get("keyStats", [])
                if not charts:
                    charts = inner.get("charts", [])
        except Exception as e:
            log.debug("OKF: report field is not double-encoded JSON, using as-is (%s)", e)

    if "\\n" in report:
        report = report.replace("\\n", "\n")

    report = re.sub(r"^```(?:json|markdown)?\s*", "", report.strip())
    report = re.sub(r"```\s*$", "", report).strip()

    log.info("OKF: generating — title=%r  charts=%d  images=%d  keyStats=%d  sources=%d",
              title[:60], len(charts), len(images), len(key_stats), len(source_documents))

    # Hydrate charts with their published Datawrapper URL (no PNG bytes
    # needed — OKF concepts link out rather than embedding raster images).
    try:
        needs_publish = [ch for ch in charts if not ch.get("datawrapper")]
        if needs_publish:
            await attach_datawrapper_charts(needs_publish, fetch_png_bytes=False)
    except Exception as exc:
        log.warning("OKF: Datawrapper publish failed, charts will omit live links: %s", exc)

    try:
        bundle_bytes = build_report_okf_bundle(
            title=title,
            question=question,
            summary=summary,
            report_md=report,
            key_stats=key_stats,
            charts=charts,
            images=images,
            file_images=file_images,
            source_documents=source_documents,
        )
    except Exception as exc:
        log.error("OKF: bundle build failed: %s", exc)
        return JSONResponse({"error": f"OKF bundle generation failed: {exc}"}, status_code=500)

    elapsed = (time.perf_counter() - t0) * 1000
    log.info("OKF: bundle ready in %.0fms — %d bytes", elapsed, len(bundle_bytes))

    return Response(
        content=bundle_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="report-okf-bundle.zip"'},
    )
