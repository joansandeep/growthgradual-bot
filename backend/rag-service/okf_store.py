"""
OKF (Open Knowledge Format) storage layer for Paperly — Supabase Storage backed.

Paperly's rag-service runs on Hugging Face Spaces, which has NO durable disk
by default (the container filesystem resets on every restart/redeploy). The
FAISS vector index is already in-memory-only and accepted as session-scoped,
but OKF concept files are meant to be a durable, inspectable record of what
was indexed — so they're written to Supabase Storage (the same project/bucket
already used elsewhere in this codebase for uploaded chat images) instead of
local disk.

Bucket layout (bucket: paperly-uploads, same bucket the backend already
writes chat images to — see backend/routes/chat.py _store_image_extractions):
  paperly-okf/<session_id>/bundle.md
  paperly-okf/<session_id>/documents/<slug>-<doc_id>.md

Frontmatter schema matches backend/utils/okf.py (Google Cloud OKF v0.1):
  type, title, description, resource, tags, timestamp

If SUPABASE_URL / SUPABASE_ANON_KEY aren't configured, every function here
degrades to a no-op (logged once) so indexing/retrieval still works locally
or in dev without Supabase configured — only the OKF persistence is skipped.
"""
import os
import re
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("rag-okf")

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
_BUCKET = os.environ.get("OKF_SUPABASE_BUCKET", "paperly-uploads")
_PREFIX = "paperly-okf"

_warned_unconfigured = False


def _configured() -> bool:
    global _warned_unconfigured
    if _SUPABASE_URL and _SUPABASE_KEY:
        return True
    if not _warned_unconfigured:
        log.warning(
            "OKF: SUPABASE_URL/SUPABASE_ANON_KEY not set — OKF bundle "
            "persistence disabled (FAISS indexing/retrieval still works)."
        )
        _warned_unconfigured = True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str, fallback: str = "doc") -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:80] or fallback


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return '""'
    s = str(v).replace('"', '\\"').replace("\n", " ").strip()
    return f'"{s}"'


def _frontmatter(fields: Dict[str, Any]) -> str:
    lines = ["---", f"type: {_yaml_scalar(fields.get('type', 'concept'))}"]
    for key in ("title", "description", "resource", "timestamp"):
        val = fields.get(key)
        if val:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    tags = fields.get("tags")
    if tags:
        lines.append("tags:")
        for t in tags:
            if t:
                lines.append(f"  - {_yaml_scalar(t)}")
    lines.append("---")
    return "\n".join(lines)


async def _put_object(path: str, text: str) -> bool:
    """Upload a text file to Supabase Storage at `<bucket>/<path>` (upsert).

    `x-upsert: true` makes Supabase Storage perform an UPDATE under the hood
    when an object already exists at `path`, instead of a plain INSERT. If
    the bucket's RLS policies only grant the INSERT privilege to this role
    (a common setup when a policy was written/tested against first-time
    uploads only), that upsert-as-update is rejected with the Postgres RLS
    error "new row violates row-level security policy" — even though the
    very same payload would have succeeded as a fresh insert. This is exactly
    the failure mode seen for `bundle.md` (rewritten every time a doc is
    indexed, so it already exists after the first write) while sibling
    `documents/<slug>.md` files (each written exactly once) never hit it.

    Root cause fix: grant UPDATE (or ALL) — not just INSERT — to this role
    on storage.objects for the `paperly-uploads` bucket, e.g. in the
    Supabase SQL editor:

        create policy "okf storage rw"
        on storage.objects for all
        to anon
        using (bucket_id = 'paperly-uploads')
        with check (bucket_id = 'paperly-uploads');

    Until/unless that policy is in place, recover in-process: delete the
    stale object (DELETE is normally covered by the same broad policy even
    when UPDATE isn't) and re-POST as a clean insert.
    """
    if not _configured():
        return False
    url = f"{_SUPABASE_URL}/storage/v1/object/{_BUCKET}/{path}"
    base_headers = {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={
                    **base_headers,
                    "Content-Type": "text/markdown; charset=utf-8",
                    "x-upsert": "true",
                },
                content=text.encode("utf-8"),
            )
            if resp.is_success:
                return True

            is_rls_denial = (
                resp.status_code in (400, 403)
                and "row-level security" in resp.text.lower()
            )
            if not is_rls_denial:
                log.warning("OKF: Supabase Storage upload %d for %s: %s", resp.status_code, path, resp.text[:150])
                return False

            log.warning(
                "OKF: upsert RLS-denied for %s (likely missing UPDATE policy on "
                "storage.objects — see _put_object docstring); retrying as delete+insert",
                path,
            )
            try:
                await client.request(
                    "DELETE",
                    f"{_SUPABASE_URL}/storage/v1/object/{_BUCKET}",
                    headers={**base_headers, "Content-Type": "application/json"},
                    json={"prefixes": [path]},
                )
            except Exception as del_exc:
                log.debug("OKF: pre-retry delete for %s failed: %s", path, del_exc)

            retry = await client.post(
                url,
                headers={
                    **base_headers,
                    "Content-Type": "text/markdown; charset=utf-8",
                    "x-upsert": "false",
                },
                content=text.encode("utf-8"),
            )
            if retry.is_success:
                log.info("OKF: delete+insert retry succeeded for %s", path)
                return True
            log.warning(
                "OKF: delete+insert retry also failed %d for %s: %s — "
                "RLS policy on storage.objects needs an UPDATE/DELETE grant for this role",
                retry.status_code, path, retry.text[:150],
            )
            return False
    except Exception as exc:
        log.warning("OKF: Supabase Storage upload error for %s: %s", path, exc)
        return False


async def _get_object(path: str) -> Optional[bytes]:
    if not _configured():
        return None
    url = f"{_SUPABASE_URL}/storage/v1/object/{_BUCKET}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={"apikey": _SUPABASE_KEY, "Authorization": f"Bearer {_SUPABASE_KEY}"},
            )
            return resp.content if resp.is_success else None
    except Exception as exc:
        log.warning("OKF: Supabase Storage fetch error for %s: %s", path, exc)
        return None


async def _list_objects(prefix: str) -> List[dict]:
    """List objects under a prefix via Supabase Storage's list endpoint."""
    if not _configured():
        return []
    url = f"{_SUPABASE_URL}/storage/v1/object/list/{_BUCKET}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={
                    "apikey": _SUPABASE_KEY,
                    "Authorization": f"Bearer {_SUPABASE_KEY}",
                    "Content-Type": "application/json",
                },
                json={"prefix": prefix, "limit": 1000},
            )
            if resp.is_success:
                return resp.json()
            log.warning("OKF: Supabase Storage list %d for prefix %s: %s", resp.status_code, prefix, resp.text[:150])
            return []
    except Exception as exc:
        log.warning("OKF: Supabase Storage list error for prefix %s: %s", prefix, exc)
        return []


async def _delete_objects(paths: List[str]) -> None:
    if not _configured() or not paths:
        return
    url = f"{_SUPABASE_URL}/storage/v1/object/{_BUCKET}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                "DELETE",
                url,
                headers={
                    "apikey": _SUPABASE_KEY,
                    "Authorization": f"Bearer {_SUPABASE_KEY}",
                    "Content-Type": "application/json",
                },
                json={"prefixes": paths},
            )
            if not resp.is_success:
                log.warning("OKF: Supabase Storage delete %d: %s", resp.status_code, resp.text[:150])
    except Exception as exc:
        log.warning("OKF: Supabase Storage delete error: %s", exc)


# ── Public API ──────────────────────────────────────────────────────────────

async def write_document_concept(session_id: str, doc: Dict[str, Any]) -> Optional[str]:
    """Write one OKF concept file for an indexed document to Supabase Storage.

    Returns the relative path (within the session's OKF folder) of the
    written concept, or None if the doc had no usable text or the upload
    failed (callers should treat this as non-fatal — FAISS indexing
    already succeeded by the time this is called).
    """
    doc_id = str(doc.get("id", "")).strip()
    name = doc.get("name", "Unknown")
    text = (doc.get("text") or "").strip()
    if not doc_id or not text:
        return None

    excerpt = text[:6000].rstrip()
    if len(text) > 6000:
        excerpt += "\n\n…[truncated — full text held in the vector index]"

    fm = _frontmatter({
        "type": "source-document",
        "title": name,
        "description": f"Indexed document: {name}",
        "tags": ["source-document", "paperly", doc.get("file_type", "")],
        "timestamp": _now_iso(),
    })
    slug = _slugify(name, doc_id[:8])
    rel_path = f"documents/{slug}-{doc_id[:8]}.md"
    full_path = f"{_PREFIX}/{session_id}/{rel_path}"
    body = f"[← Back to bundle](../bundle.md)\n\n## Extracted Content\n\n{excerpt}\n"

    ok = await _put_object(full_path, f"{fm}\n\n{body}")
    return rel_path if ok else None


async def write_bundle_manifest(session_id: str, doc_concepts: List[Dict[str, str]]) -> None:
    """(Re)write the session-level bundle.md that links every document concept."""
    links = "\n".join(f"- [{d['name']}]({d['rel_path']})" for d in doc_concepts)
    fm = _frontmatter({
        "type": "concept-bundle",
        "title": f"Paperly Session {session_id[:8]}",
        "description": "OKF bundle of all documents indexed in this Paperly session.",
        "tags": ["paperly", "session-bundle"],
        "timestamp": _now_iso(),
    })
    body = f"## Documents\n\n{links}\n" if links else "## Documents\n\n_No documents indexed yet._\n"
    full_path = f"{_PREFIX}/{session_id}/bundle.md"
    await _put_object(full_path, f"{fm}\n\n{body}")


async def get_session_bundle_files(session_id: str) -> List[Tuple[str, bytes]]:
    """Fetch every OKF file for a session as (relative_path, bytes) — used to
    build a downloadable zip on demand."""
    prefix = f"{_PREFIX}/{session_id}"
    out: List[Tuple[str, bytes]] = []

    bundle_bytes = await _get_object(f"{prefix}/bundle.md")
    if bundle_bytes:
        out.append(("bundle.md", bundle_bytes))

    doc_entries = await _list_objects(f"{prefix}/documents")
    for entry in doc_entries:
        name = entry.get("name", "")
        if not name or not name.endswith(".md"):
            continue
        content = await _get_object(f"{prefix}/documents/{name}")
        if content:
            out.append((f"documents/{name}", content))

    return out


async def delete_session_bundle(session_id: str) -> None:
    """Remove all OKF concept files for a session from Supabase Storage
    (mirrors FAISS index deletion on session cleanup)."""
    prefix = f"{_PREFIX}/{session_id}"
    paths = [f"{prefix}/bundle.md"]
    doc_entries = await _list_objects(f"{prefix}/documents")
    for entry in doc_entries:
        name = entry.get("name", "")
        if name:
            paths.append(f"{prefix}/documents/{name}")
    await _delete_objects(paths)
