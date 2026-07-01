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

    # ── Self-contained HTML — the human-readable entry point ─────────────
    # Users double-click index.html to read the full report in a browser.
    # The markdown files are the machine-readable OKF layer; this HTML is
    # the usable layer on top, built from the same data without any server.
    files["index.html"] = _build_html_report(
        title=title or question or "Research Report",
        question=question,
        summary=summary,
        report_md=report_md,
        key_stats=key_stats,
        charts=charts,
        images=images,
        source_documents=source_documents,
        timestamp=now,
    )

    # ── Zip it up ────────────────────────────────────────────────────────
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path, content in files.items():
            zf.writestr(rel_path, content)
    buf.seek(0)
    return buf.getvalue()


def _md_to_html(md: str) -> str:
    """Minimal markdown→HTML converter (no deps — just handles the subset
    Growth Gradual reports use: headings, bold, italic, bullets, tables,
    horizontal rules, inline code, and paragraphs)."""
    import html as _html
    lines = md.split("\n")
    out: list[str] = []
    in_ul = False
    in_table = False

    def flush_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def flush_table():
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    def inline(text: str) -> str:
        # Bold+italic, bold, italic, inline code, links
        import re
        text = _html.escape(text, quote=False)
        text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', text)
        text = re.sub(r'\*\*(.+?)\*\*',     r'<strong>\1</strong>', text)
        text = re.sub(r'\*(.+?)\*',          r'<em>\1</em>', text)
        text = re.sub(r'`(.+?)`',            r'<code>\1</code>', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank">\1</a>', text)
        return text

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Blank line
        if not stripped:
            flush_ul()
            flush_table()
            i += 1
            continue

        # Headings
        import re
        m = re.match(r'^(#{1,4})\s+(.*)', stripped)
        if m:
            flush_ul(); flush_table()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # Horizontal rule
        if re.match(r'^[-*_]{3,}$', stripped):
            flush_ul(); flush_table()
            out.append("<hr>")
            i += 1
            continue

        # Table row (starts with |)
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Skip separator rows like |---|---|
            if all(re.match(r'^[-:]+$', c) for c in cells if c):
                if not in_table:
                    pass  # will be handled as part of header
                i += 1
                continue
            if not in_table:
                out.append('<table><thead><tr>')
                out.extend(f"<th>{inline(c)}</th>" for c in cells)
                out.append('</tr></thead><tbody>')
                in_table = True
            else:
                out.append('<tr>')
                out.extend(f"<td>{inline(c)}</td>" for c in cells)
                out.append('</tr>')
            i += 1
            continue

        # Bullet list
        m2 = re.match(r'^[-*+]\s+(.*)', stripped)
        if m2:
            flush_table()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(m2.group(1))}</li>")
            i += 1
            continue

        # Numbered list
        m3 = re.match(r'^\d+\.\s+(.*)', stripped)
        if m3:
            flush_table(); flush_ul()
            out.append(f"<p>• {inline(m3.group(1))}</p>")
            i += 1
            continue

        # Plain paragraph
        flush_ul(); flush_table()
        out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    flush_ul()
    flush_table()
    return "\n".join(out)


def _build_html_report(
    *,
    title: str,
    question: str,
    summary: str,
    report_md: str,
    key_stats: list,
    charts: list,
    images: list,
    source_documents: list,
    timestamp: str,
) -> str:
    """Build a single self-contained HTML file that renders the full report.
    No external dependencies — works by double-clicking in any browser.
    """
    # Key stats cards
    stats_html = ""
    if key_stats:
        cards = ""
        for s in key_stats:
            chg = s.get("change", "") or ""
            colour = "#16a34a" if "+" in chg else ("#dc2626" if "-" in chg else "#64748b")
            cards += f"""<div class="stat-card">
  <div class="stat-value">{s.get('value','')}</div>
  <div class="stat-label">{s.get('label','')}</div>
  {f'<div class="stat-change" style="color:{colour}">{chg}</div>' if chg else ''}
</div>"""
        stats_html = f'<div class="stats-grid">{cards}</div>'

    # Charts section
    charts_html = ""
    for ch in charts:
        dw = ch.get("datawrapper") or {}
        url = dw.get("publicUrl") or dw.get("embedUrl") or ""
        png = dw.get("pngUrl") or ""
        ch_title = ch.get("title", "")
        if png:
            charts_html += f'<div class="chart-block"><h4>{ch_title}</h4><img src="{png}" alt="{ch_title}" style="max-width:100%;border-radius:8px"></div>'
        elif url:
            charts_html += f'<div class="chart-block"><h4>{ch_title}</h4><iframe src="{url}" width="100%" height="400" frameborder="0" style="border-radius:8px"></iframe></div>'

    # Web images
    images_html = ""
    for im in images:
        url = im.get("url", "")
        caption = im.get("caption", "")
        if url:
            images_html += f'<figure><img src="{url}" alt="{caption}" style="max-width:100%;border-radius:8px"><figcaption>{caption}</figcaption></figure>'

    # Source documents
    sources_html = ""
    if source_documents:
        items = ""
        for doc in source_documents:
            excerpt = (doc.get("text") or "")[:2000].strip()
            if len(doc.get("text") or "") > 2000:
                excerpt += "\n\n[… truncated — see original file]"
            import html as _html
            items += f"""<details class="source-doc">
  <summary><strong>{_html.escape(doc.get('name','File'))}</strong>
    <span class="badge">{_html.escape(doc.get('file_type','') or 'file')}</span>
  </summary>
  <pre class="source-text">{_html.escape(excerpt)}</pre>
</details>"""
        sources_html = f'<section class="sources-section"><h2>📎 Source Files</h2>{items}</section>'

    report_body_html = _md_to_html(report_md)
    safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
    safe_summary = summary.replace("<", "&lt;").replace(">", "&gt;")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title} — Growth Gradual</title>
<style>
  :root {{
    --navy: #0f1535; --gold: #f59e0b; --green: #15803d;
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.7; }}
  .top-bar {{ background: var(--navy); color: #fff; padding: 14px 32px; display: flex; align-items: center; gap: 12px; }}
  .top-bar .brand {{ font-size: 13px; font-weight: 700; letter-spacing: .08em; color: var(--gold); text-transform: uppercase; }}
  .top-bar .ts {{ margin-left: auto; font-size: 11px; opacity: .6; }}
  .hero {{ background: linear-gradient(135deg, var(--navy) 0%, #1a1f4e 100%); color: #fff; padding: 48px 32px 40px; }}
  .hero h1 {{ font-size: clamp(20px, 3vw, 30px); font-weight: 800; line-height: 1.25; margin-bottom: 14px; }}
  .hero .summary {{ font-size: 15px; opacity: .85; max-width: 760px; border-left: 3px solid var(--gold); padding-left: 16px; }}
  .container {{ max-width: 900px; margin: 0 auto; padding: 32px 24px 64px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; margin: 28px 0; }}
  .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .stat-value {{ font-size: 22px; font-weight: 800; color: var(--navy); }}
  .stat-label {{ font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: .06em; }}
  .stat-change {{ font-size: 12px; font-weight: 700; margin-top: 4px; }}
  .report-body {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 32px; margin: 24px 0; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  .report-body h1,.report-body h2 {{ color: var(--navy); margin: 28px 0 10px; border-bottom: 2px solid var(--gold); padding-bottom: 6px; }}
  .report-body h3,.report-body h4 {{ color: var(--navy); margin: 20px 0 8px; }}
  .report-body p {{ margin: 10px 0; }}
  .report-body ul {{ margin: 8px 0 8px 22px; }}
  .report-body li {{ margin: 4px 0; }}
  .report-body table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }}
  .report-body th {{ background: var(--navy); color: #fff; padding: 8px 12px; text-align: left; }}
  .report-body td {{ padding: 7px 12px; border-bottom: 1px solid var(--border); }}
  .report-body tr:nth-child(even) td {{ background: #f8fafc; }}
  .report-body code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
  .report-body hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
  .report-body a {{ color: var(--green); }}
  .chart-block {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin: 16px 0; }}
  .chart-block h4 {{ color: var(--navy); margin-bottom: 12px; font-size: 14px; }}
  figure {{ margin: 16px 0; text-align: center; }}
  figcaption {{ font-size: 12px; color: var(--muted); margin-top: 6px; }}
  .sources-section {{ margin-top: 32px; }}
  .sources-section h2 {{ color: var(--navy); font-size: 18px; margin-bottom: 14px; }}
  .source-doc {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin: 10px 0; overflow: hidden; }}
  .source-doc summary {{ padding: 12px 16px; cursor: pointer; font-size: 14px; display: flex; align-items: center; gap: 8px; user-select: none; }}
  .source-doc summary:hover {{ background: #f8fafc; }}
  .badge {{ font-size: 10px; background: #e2e8f0; color: var(--muted); padding: 2px 8px; border-radius: 99px; text-transform: uppercase; }}
  .source-text {{ padding: 16px; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; background: #f8fafc; border-top: 1px solid var(--border); color: var(--text); max-height: 400px; overflow-y: auto; }}
  .footer {{ text-align: center; font-size: 11px; color: var(--muted); margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--border); }}
  .okf-note {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #92400e; margin-bottom: 24px; }}
</style>
</head>
<body>
<div class="top-bar">
  <span class="brand">⚡ Growth Gradual — In The Money</span>
  <span class="ts">Generated {timestamp}</span>
</div>
<div class="hero">
  <h1>{safe_title}</h1>
  {f'<p class="summary">{safe_summary}</p>' if safe_summary else ''}
</div>
<div class="container">
  <div class="okf-note">
    📦 This report is part of an <strong>Open Knowledge Format (OKF)</strong> bundle.
    The <code>sections/</code>, <code>charts/</code>, <code>sources/</code> folders alongside this file
    contain machine-readable markdown concepts with YAML frontmatter for each section and data source.
  </div>
  {stats_html}
  {f'<div class="report-body">{report_body_html}</div>' if report_body_html else ''}
  {charts_html}
  {images_html}
  {sources_html}
  <div class="footer">
    Growth Gradual Research Report &nbsp;·&nbsp; {timestamp} &nbsp;·&nbsp; growth-gradual.com
  </div>
</div>
</body>
</html>"""

