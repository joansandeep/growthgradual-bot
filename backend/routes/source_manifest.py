"""Canonical, export-safe source manifest helpers.

The report writer only needs a curated subset of the retrieved pages in its
prompt.  The reader, however, must be able to see *every* source that
contributed to a report.  This module keeps those two concerns separate: it
creates a small, safe manifest for display and never limits the number of
entries.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _clean_text(value: object, limit: int = 300) -> str:
    """Return a single-line display string without control characters."""
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _safe_http_url(value: object) -> str:
    """Keep only absolute HTTP(S) URLs and drop fragments for de-duplication."""
    raw = _clean_text(value, 2_000)
    if not _HTTP_URL_RE.match(raw):
        return ""
    try:
        parsed = urlparse(raw)
        if not parsed.netloc:
            return ""
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, parsed.query, ""))
    except Exception:
        return ""


def _publisher_for(url: str, fallback: str) -> str:
    if url:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    return _clean_text(fallback, 80)


def normalise_source_manifest(sources: object, source_documents: object = None) -> list[dict]:
    """Build an ordered, deduplicated manifest for report viewers and exports.

    There is deliberately no maximum length here.  If discovery produced 100
    distinct sources, the manifest contains 100 entries (plus any uploaded
    documents).  Full page content/snippets are intentionally excluded: the
    appendix needs provenance, not another copy of the research corpus.
    """
    result: list[dict] = []
    seen: set[str] = set()

    def add(entry: object, default_kind: str = "Web source") -> None:
        if not isinstance(entry, dict):
            return
        title = _clean_text(entry.get("title") or entry.get("name"), 300)
        url = _safe_http_url(entry.get("url"))
        kind = _clean_text(entry.get("kind") or entry.get("sourceType") or default_kind, 80)
        publisher = _publisher_for(url, entry.get("publisher") or kind)
        if not title:
            title = publisher or "Untitled source"

        # A URL is a stronger identity than a display title.  Sources without
        # URLs (uploaded documents and internal verified feeds) still get a
        # stable title/kind identity so repeated entries don't flood exports.
        identity = ("url:" + url.lower().rstrip("/")) if url else (
            "label:" + title.casefold() + "|" + kind.casefold()
        )
        if identity in seen:
            return
        seen.add(identity)
        result.append({
            "title": title,
            "url": url,
            "publisher": publisher,
            "kind": kind,
        })

    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and str(source.get("url") or "").startswith("internal://"):
                # Internal sources are real verified feeds, but their private
                # transport URL must never be exposed in an exported report.
                source = {**source, "url": "", "kind": source.get("kind") or "Verified data"}
            add(source)

    if isinstance(source_documents, list):
        for document in source_documents:
            if not isinstance(document, dict):
                continue
            name = _clean_text(document.get("name") or document.get("title"), 300)
            if name:
                add({"title": name, "kind": "Uploaded document", "publisher": "User-provided"}, "Uploaded document")

    return result
