"""
Open Knowledge Format (OKF) bundle builder.

Implements the producer side of Google Cloud's OKF v0.1 spec:
  https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing

An OKF bundle is a directory of markdown files ("concepts"), each carrying a
YAML frontmatter block with a small set of agreed fields:
    type        (required) — concept category, e.g. "report", "chart", "image", "source-document"
    title       (optional) — human-readable title
    description (optional) — 1-2 sentence summary
    resource    (optional) — pointer to an external/binary asset (URL or relative path)
    tags        (optional) — list of free-text tags
    timestamp   (optional) — ISO-8601 generation/update time

A concept's identity is its file path (relative to the bundle root) with the
.md suffix removed. Concepts cross-link via standard markdown links, which
turns the directory into a navigable knowledge graph.

This module is used in two places:
  1. routes/okf.py    — turns a generated research report into a downloadable
                         OKF bundle (.zip) instead of a PDF.
  2. rag-service       — Paperly stores each indexed document (and a per-
                         session bundle manifest) as OKF concepts on disk,
                         alongside the FAISS vector index, so the underlying
                         file context is held in an open, inspectable format
                         rather than only living inside the vector store.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str, fallback: str = "concept") -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:80] or fallback


def _yaml_scalar(v: Any) -> str:
    """Render a single YAML scalar value, quoting strings that need it."""
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace('"', '\\"').replace("\n", " ").strip()
    return f'"{s}"'


def _yaml_frontmatter(fields: dict[str, Any]) -> str:
    """Render a YAML frontmatter block for an OKF concept document.

    `type` is required by the spec; everything else is optional and omitted
    when empty so the frontmatter stays minimal and readable.
    """
    lines = ["---"]
    # `type` always first, always present (spec requirement)
    lines.append(f"type: {_yaml_scalar(fields.get('type', 'concept'))}")
    for key in ("title", "description", "resource", "timestamp"):
        val = fields.get(key)
        if val:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    tags = fields.get("tags")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {_yaml_scalar(t)}")
    lines.append("---")
    return "\n".join(lines)


def make_concept(
    *,
    path: str,
    type: str,
    title: str = "",
    description: str = "",
    resource: str = "",
    tags: Iterable[str] | None = None,
    timestamp: str = "",
    body: str = "",
) -> tuple[str, str]:
    """Build one OKF concept document.

    Returns (relative_path, file_text). `path` should be a forward-slash
    relative path WITHOUT the .md suffix (e.g. "charts/01-nifty-trend") —
    the .md suffix is the on-disk file extension; the concept identifier
    per the spec is the path with that suffix stripped.
    """
    fm = _yaml_frontmatter({
        "type": type,
        "title": title,
        "description": description,
        "resource": resource,
        "tags": list(tags) if tags else None,
        "timestamp": timestamp or _now_iso(),
    })
    text = f"{fm}\n\n{body.strip()}\n"
    rel_path = path if path.endswith(".md") else f"{path}.md"
    return rel_path, text


def split_report_sections(report_md: str) -> list[tuple[str, str]]:
    """Split a markdown report into (heading, body) sections on H1/H2 boundaries.

    Falls back to a single ("Report", full_text) section if no headings found.
    """
    if not report_md.strip():
        return []
    # Split on lines that are H1/H2 headers, keeping the header text
    parts = re.split(r"(?m)^(#{1,2}\s+.+)$", report_md)
    if len(parts) <= 1:
        return [("Report", report_md.strip())]
    sections: list[tuple[str, str]] = []
    # parts[0] is any preamble before the first heading
    preamble = parts[0].strip()
    if preamble:
        sections.append(("Overview", preamble))
    for i in range(1, len(parts), 2):
        heading_line = parts[i].strip()
        heading = re.sub(r"^#{1,2}\s+", "", heading_line).strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if heading or body:
            sections.append((heading or "Section", body))
    return sections


def build_report_okf_bundle(
    *,
    title: str,
    question: str,
    summary: str,
    report_md: str,
    key_stats: list[dict] | None = None,
    charts: list[dict] | None = None,
    images: list[dict] | None = None,
    file_images: list[dict] | None = None,
    source_documents: list[dict] | None = None,
) -> bytes:
    """Build a complete OKF bundle for a research report and return it as
    in-memory zip bytes, ready to stream as a download.

    Bundle layout:
      report.md                       — root concept, links to every other concept
      sections/<n>-<slug>.md          — one concept per report section
      stats/key-stats.md              — key metrics table, if present
      charts/<n>-<slug>.md            — one concept per chart, resource = Datawrapper/PNG url
      images/<n>-<slug>.md            — one concept per web image used in the report
      sources/<n>-<slug>.md           — one concept per uploaded/attached source file
    """
    key_stats = key_stats or []
    charts = charts or []
    images = images or []
    file_images = file_images or []
    source_documents = source_documents or []

    now = _now_iso()
    files: dict[str, str] = {}

    sections = split_report_sections(report_md)

    # ── Root concept: report.md ────────────────────────────────────────
    root_links = []
    for i, (heading, _body) in enumerate(sections, 1):
        slug = _slugify(heading, f"section-{i}")
        root_links.append(f"- [{heading}](sections/{i:02d}-{slug}.md)")
    if key_stats:
        root_links.append("- [Key Statistics](stats/key-stats.md)")
    for i, ch in enumerate(charts, 1):
        ch_title = ch.get("title") or f"Chart {i}"
        slug = _slugify(ch_title, f"chart-{i}")
        root_links.append(f"- [{ch_title}](charts/{i:02d}-{slug}.md)")
    for i, im in enumerate(images, 1):
        im_title = im.get("caption") or f"Image {i}"
        slug = _slugify(im_title, f"image-{i}")
        root_links.append(f"- [{im_title}](images/{i:02d}-{slug}.md)")
    for i, doc in enumerate(source_documents, 1):
        doc_title = doc.get("name") or f"Source {i}"
        slug = _slugify(doc_title, f"source-{i}")
        root_links.append(f"- [{doc_title}](sources/{i:02d}-{slug}.md)")

    root_body_parts = []
    if summary:
        root_body_parts.append(f"## Executive Summary\n\n{summary.strip()}")
    if question:
        root_body_parts.append(f"## Original Question\n\n{question.strip()}")
    if root_links:
        root_body_parts.append("## Contents\n\n" + "\n".join(root_links))
    root_body = "\n\n".join(root_body_parts) if root_body_parts else report_md.strip()

    rel, text = make_concept(
        path="report",
        type="report",
        title=title or question or "Research Report",
        description=summary[:280] if summary else "",
        tags=["growth-gradual", "research-report"],
        timestamp=now,
        body=root_body,
    )
    files[rel] = text

    # ── Section concepts ────────────────────────────────────────────────
    for i, (heading, body) in enumerate(sections, 1):
        slug = _slugify(heading, f"section-{i}")
        rel, text = make_concept(
            path=f"sections/{i:02d}-{slug}",
            type="report-section",
            title=heading,
            tags=["section"],
            timestamp=now,
            body=f"[← Back to report](../report.md)\n\n{body}",
        )
        files[rel] = text

    # ── Key stats concept ───────────────────────────────────────────────
    if key_stats:
        rows = ["| Metric | Value | Change |", "|---|---|---|"]
        for s in key_stats:
            rows.append(f"| {s.get('label','')} | {s.get('value','')} | {s.get('change','') or '—'} |")
        rel, text = make_concept(
            path="stats/key-stats",
            type="metric-table",
            title="Key Statistics",
            tags=["stats", "metrics"],
            timestamp=now,
            body="[← Back to report](../report.md)\n\n" + "\n".join(rows),
        )
        files[rel] = text

    # ── Chart concepts ──────────────────────────────────────────────────
    for i, ch in enumerate(charts, 1):
        ch_title = ch.get("title") or f"Chart {i}"
        slug = _slugify(ch_title, f"chart-{i}")
        dw = ch.get("datawrapper") or {}
        resource = dw.get("publicUrl") or dw.get("embedUrl") or dw.get("pngUrl") or ""
        series = ch.get("series") or []
        body_lines = [f"[← Back to report](../report.md)", "", f"**Chart type:** {ch.get('type','')}"]
        if resource:
            body_lines.append(f"\n**Live chart:** [{resource}]({resource})")
        for s in series:
            body_lines.append(f"\n### {s.get('name','Series')}")
            body_lines.append("| Label | Value |")
            body_lines.append("|---|---|")
            for d in s.get("data", []):
                body_lines.append(f"| {d.get('label','')} | {d.get('value','')} |")
        rel, text = make_concept(
            path=f"charts/{i:02d}-{slug}",
            type="chart",
            title=ch_title,
            resource=resource,
            tags=["chart", ch.get("type", "")],
            timestamp=now,
            body="\n".join(body_lines),
        )
        files[rel] = text

    # ── Web image concepts ──────────────────────────────────────────────
    for i, im in enumerate(images, 1):
        im_title = im.get("caption") or f"Image {i}"
        slug = _slugify(im_title, f"image-{i}")
        url = im.get("url", "")
        rel, text = make_concept(
            path=f"images/{i:02d}-{slug}",
            type="image",
            title=im_title,
            resource=url,
            description=im_title,
            tags=["image", "web-image"],
            timestamp=now,
            body=f"[← Back to report](../report.md)\n\n![{im_title}]({url})",
        )
        files[rel] = text

    # ── Attached source-document concepts (and their embedded images) ───
    for i, doc in enumerate(source_documents, 1):
        doc_name = doc.get("name") or f"Source {i}"
        slug = _slugify(doc_name, f"source-{i}")
        excerpt = (doc.get("text") or doc.get("extractedText") or "").strip()
        if len(excerpt) > 4000:
            excerpt = excerpt[:4000].rstrip() + "\n\n…[truncated — see original file]"
        body_lines = [f"[← Back to report](../report.md)", ""]
        if doc.get("file_type"):
            body_lines.append(f"**File type:** {doc['file_type']}")
        if excerpt:
            body_lines.append(f"\n## Extracted Content\n\n{excerpt}")
        rel, text = make_concept(
            path=f"sources/{i:02d}-{slug}",
            type="source-document",
            title=doc_name,
            description=f"Uploaded file used as report context: {doc_name}",
            tags=["source-document", "attachment"],
            timestamp=now,
            body="\n".join(body_lines),
        )
        files[rel] = text

    # ── Images embedded in attached files (file_images: charts/figures the
    #    model extracted from uploaded PDFs/docs via vision) ─────────────
    for i, fi in enumerate(file_images, 1):
        fi_name = fi.get("name") or f"file-image-{i}"
        slug = _slugify(fi_name, f"file-image-{i}")
        mime = fi.get("mimeType", "")
        rel, text = make_concept(
            path=f"sources/images/{i:02d}-{slug}",
            type="image",
            title=fi_name,
            description="Image/chart extracted from an uploaded source file",
            tags=["image", "extracted-from-file"],
            timestamp=now,
            body=(
                f"[← Back to report](../../report.md)\n\n"
                f"**MIME type:** {mime}\n\n"
                f"Original base64 image data is embedded in this bundle's "
                f"`resource` field; render with any markdown/image tool "
                f"that supports `data:` URIs."
            ),
        )
        files[rel] = text

    # ── Zip it up ────────────────────────────────────────────────────────
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path, content in files.items():
            zf.writestr(rel_path, content)
    buf.seek(0)
    return buf.getvalue()
