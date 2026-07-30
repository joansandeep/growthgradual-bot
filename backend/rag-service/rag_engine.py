"""
Paperly RAG Engine v2
=====================
Architecture:
  Upload → Chunk (RecursiveCharacterTextSplitter) → Embed (sentence-transformers)
         → Store (FAISS IndexFlatIP, per-session) → Query → Adaptive Retrieval
         → Grounded Prompt → LLM (hallucination-free answer)

Fixes in v2:
  - Dedup by doc ID not hash (re-upload after delete works)
  - Adaptive threshold (0.15 → 0.09 → 0.05 → 0.0)
  - Query expansion for vague queries
  - Report mode: full-coverage retrieval with report-specific prompt
  - Session cleanup on delete
"""

import os
import re
import time
import logging
import threading
import asyncio
from typing import List, Dict, Any, Tuple

import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
from langchain_text_splitters import RecursiveCharacterTextSplitter

import okf_store

log = logging.getLogger("rag-engine")

MODEL_NAME          = os.getenv("EMBED_MODEL",            "all-MiniLM-L6-v2")
CHUNK_SIZE          = int(os.getenv("CHUNK_SIZE",         "1000"))
CHUNK_OVERLAP       = int(os.getenv("CHUNK_OVERLAP",      "150"))
RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.15"))


class SessionIndex:
    """Per-session FAISS index + chunk store."""

    def __init__(self, embed_dim: int):
        self.index      = faiss.IndexFlatIP(embed_dim)
        self.chunks:    List[Dict[str, Any]] = []
        self.doc_ids:   set = set()
        self.doc_names: Dict[str, str] = {}
        self.okf_concepts: List[Dict[str, str]] = []  # [{"name", "rel_path"}] for bundle.md
        self.lock       = threading.Lock()
        # Dynamically extracted entity names from indexed text
        self._entities: set = set()
        # Wall-clock time of last access — used by RAGEngine's idle-session
        # reaper. Without this, every session_id ever seen (one per browser
        # profile, since sessions aren't shared across browsers) stays in
        # this process's memory forever, since the only other removal path
        # is an explicit "new chat" delete call the frontend doesn't always
        # make. That's an unbounded memory leak on a long-running process —
        # exactly what would eventually force an OOM crash-and-restart on a
        # memory-constrained free-tier Space.
        self.last_active: float = time.time()

    def touch(self):
        self.last_active = time.time()

    def add(self, vectors: np.ndarray, chunks: List[Dict]):
        with self.lock:
            self.index.add(vectors)
            self.chunks.extend(chunks)

    def search(self, q_vec: np.ndarray, k: int) -> Tuple[np.ndarray, np.ndarray]:
        with self.lock:
            if self.index.ntotal == 0:
                return np.array([]), np.array([])
            k = min(k, self.index.ntotal)
            scores, idx = self.index.search(q_vec, k)
            return scores[0], idx[0]

    @property
    def total(self) -> int:
        return self.index.ntotal

    def has_doc(self, doc_id: str) -> bool:
        return bool(doc_id) and doc_id in self.doc_ids

    def register_doc(self, doc_id: str, name: str):
        if doc_id:
            self.doc_ids.add(doc_id)
            self.doc_names[doc_id] = name

    def register_entities(self, entities: set):
        """Merge newly extracted entity names into the session entity set."""
        self._entities.update(entities)

    @property
    def entities(self) -> set:
        return self._entities


class RAGEngine:
    """
    Manages per-session FAISS indexes.
    Full pipeline: chunk → embed → index → retrieve → grounded prompt.
    """

    def __init__(self):
        log.info(f"Loading embedding model: {MODEL_NAME} ...")
        self._model    = SentenceTransformer(MODEL_NAME)
        self._dim      = self._model.get_sentence_embedding_dimension()
        self._sessions: Dict[str, SessionIndex] = {}
        self._lock     = threading.Lock()
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", "! ", "? ", ", ", " ", ""],
            length_function=len,
        )
        log.info(f"✅ RAG Engine ready — model={MODEL_NAME} dim={self._dim}")

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    # ── Session management ──────────────────────────────────────

    def _sess(self, sid: str) -> SessionIndex:
        with self._lock:
            if sid not in self._sessions:
                self._sessions[sid] = SessionIndex(self._dim)
            sess = self._sessions[sid]
        sess.touch()
        return sess

    def list_sessions(self) -> List[str]:
        with self._lock:
            return list(self._sessions.keys())

    def get_session_info(self, sid: str) -> Dict[str, Any]:
        s = self._sessions.get(sid)
        if not s:
            return {"session_id": sid, "chunk_count": 0, "exists": False}
        return {
            "session_id":  sid,
            "chunk_count": s.total,
            "doc_count":   len(s.doc_ids),
            "docs":        list(s.doc_names.values()),
            "exists":      True,
        }

    async def delete_session(self, sid: str):
        """Remove all indexed data for a session — called when session is deleted."""
        with self._lock:
            self._sessions.pop(sid, None)
        await okf_store.delete_session_bundle(sid)
        log.info(f"Deleted FAISS index for session {sid[:8]}")

    def evict_idle_sessions(self, max_idle_seconds: float) -> int:
        """
        Drop any session untouched for longer than max_idle_seconds. Unlike
        delete_session(), this is a pure in-memory cleanup with no Supabase
        call — OKF concept files for evicted sessions are simply left as-is
        in storage (they're small, durable, and harmless to leave behind;
        they'll just no longer be retrievable via this session's FAISS
        index in this process). Called periodically by a background task
        in main.py so this process's memory doesn't grow without bound
        across every browser/session that's ever connected.
        """
        cutoff = time.time() - max_idle_seconds
        with self._lock:
            idle = [sid for sid, sess in self._sessions.items() if sess.last_active < cutoff]
            for sid in idle:
                del self._sessions[sid]
        if idle:
            log.info("Evicted %d idle session(s) (idle > %.0fs): %s",
                      len(idle), max_idle_seconds, [s[:8] for s in idle])
        return len(idle)

    # ── Indexing ────────────────────────────────────────────────

    async def index(self, session_id: str, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Index documents. Idempotent — skips already-indexed doc IDs."""
        sess = self._sess(session_id)
        chunks_added = 0

        for doc in documents:
            doc_id = str(doc.get("id", ""))
            name   = doc.get("name", "Unknown")
            text   = (doc.get("text") or "").strip()

            if not text or len(text) < 20:
                log.warning(f"Skipping empty doc: {name}")
                continue

            if sess.has_doc(doc_id):
                log.info(f"Already indexed: {name}")
                continue

            raw = self._splitter.split_text(text)
            if not raw:
                continue

            metas = [
                {
                    "text":         chunk,
                    "source_name":  name,
                    "source_type":  doc.get("source_type", "file"),
                    "file_type":    doc.get("file_type", ""),
                    "doc_id":       doc_id,
                    "chunk_index":  i,
                    "total_chunks": len(raw),
                }
                for i, chunk in enumerate(raw)
            ]
            # _embed() runs SentenceTransformer.encode() on CPU — a blocking,
            # CPU-bound call. Running it inline here (as before) freezes the
            # single-threaded asyncio event loop for its entire duration,
            # so nothing else — not even a trivial /ping health check —
            # can be served until it finishes. Offload it to a thread so
            # concurrent requests keep flowing.
            vecs = await asyncio.to_thread(self._embed, [m["text"] for m in metas])
            sess.add(vecs, metas)
            sess.register_doc(doc_id, name)
            # Extract and register entity names from this document's text
            entities = RAGEngine._extract_entities_from_text(text)
            sess.register_entities(entities)
            log.debug(f"Extracted {len(entities)} entities from '{name}'")
            chunks_added += len(metas)
            log.info(f"Indexed '{name}': {len(raw)} chunks")

            # ── OKF persistence: write this document as a concept file
            #    alongside the FAISS index, then refresh the session's
            #    bundle manifest so file context is stored in an open,
            #    inspectable format and not just inside the vector store.
            #    Runs against Supabase Storage (not local disk) since the
            #    rag-service's container filesystem on HF Spaces is
            #    ephemeral. Non-fatal if it fails — FAISS indexing above
            #    already succeeded, so Q&A/report retrieval still works.
            rel_path = await okf_store.write_document_concept(session_id, doc)
            if rel_path:
                sess.okf_concepts.append({"name": name, "rel_path": rel_path})

        if chunks_added > 0:
            await okf_store.write_bundle_manifest(session_id, sess.okf_concepts)

        return {"chunks_added": chunks_added, "total_chunks": sess.total}

    # ── Q&A Retrieval ───────────────────────────────────────────

    def retrieve(
        self,
        session_id: str,
        question:   str,
        top_k:      int   = 8,
        min_score:  float = RELEVANCE_THRESHOLD,
    ) -> Dict[str, Any]:
        """Retrieve relevant chunks for a Q&A query with adaptive thresholding."""
        sess = self._sessions.get(session_id)
        if not sess or sess.total == 0:
            return self._no_content(question, "no_index", "No documents indexed for this session.")
        sess.touch()

        scope_info = self._detect_scope(question, sess)
        # Always let scope detection drive top_k — caller's value is just a floor
        # top_k=0 means "let scope detection fully decide"
        top_k = scope_info["top_k_hint"] if top_k == 0 else max(top_k, scope_info["top_k_hint"])
        # General queries: always use zero threshold so we pull wide coverage
        if scope_info["scope"] == "general":
            min_score = 0.0

        expanded = self._expand_query(question, sess)
        q_vec    = self._embed([expanded])
        scores, indices = sess.search(q_vec, min(top_k * 2, sess.total))

        if len(scores) == 0:
            return self._no_content(question, "no_results", "Search returned no results.")

        # Adaptive threshold
        good, used_thr = [], min_score
        for thr in [min_score, min_score * 0.6, min_score * 0.35, 0.0]:
            good = [
                (float(scores[i]), int(indices[i]))
                for i in range(len(scores))
                if scores[i] >= thr and 0 <= int(indices[i]) < len(sess.chunks)
            ]
            if good:
                used_thr = thr
                break

        if not good:
            best = float(scores[0]) if len(scores) else 0
            return self._no_content(
                question, "low_relevance",
                f"No relevant content found (best score: {best:.3f}). "
                "The documents may not contain information about this topic."
            )

        if used_thr < min_score:
            log.info(f"Adaptive threshold: {used_thr:.3f} (base: {min_score:.3f})")

        seen, retrieved = set(), []
        for score, idx in good[:top_k]:
            chunk = sess.chunks[idx]
            sig   = chunk["text"][:80]
            if sig not in seen:
                seen.add(sig)
                retrieved.append({**chunk, "score": score})

        source_files = list(dict.fromkeys(c["source_name"] for c in retrieved))
        context      = self._fmt_context(retrieved)
        prompt       = self._qa_prompt(context, source_files, question, scope_info)

        log.info(f"Q&A: {len(retrieved)} chunks from {len(source_files)} source(s) scope={scope_info['scope']} (thr={used_thr:.3f})")
        return {
            "context":       context,
            "system_prompt": prompt,
            "retrieved":     len(retrieved),
            "source_files":  source_files,
            "has_content":   True,
        }

    # ── Report Retrieval ────────────────────────────────────────

    def retrieve_for_report(
        self,
        session_id:  str,
        report_spec: str,
        report_type: str = "comprehensive",
    ) -> Dict[str, Any]:
        """
        Full-coverage retrieval for report generation.
        Uses ALL chunks (no threshold filtering) to ensure complete coverage.
        Returns a report-specific system prompt.
        """
        sess = self._sessions.get(session_id)
        if not sess or sess.total == 0:
            return self._no_content(report_spec, "no_index",
                "No documents indexed. Upload files before generating a report.")
        sess.touch()

        # For reports: retrieve ALL chunks (no threshold) sorted by doc order
        all_chunks = sorted(
            [
                {**c, "score": 1.0}
                for c in sess.chunks
            ],
            key=lambda x: (x["source_name"], x["chunk_index"])
        )

        # Deduplicate by text signature
        seen, retrieved = set(), []
        for c in all_chunks:
            sig = c["text"][:80]
            if sig not in seen:
                seen.add(sig)
                retrieved.append(c)

        source_files = list(dict.fromkeys(c["source_name"] for c in retrieved))

        # Build context (larger limit for reports)
        context = self._fmt_context_report(retrieved, max_chars=150000)
        prompt  = self._report_prompt(context, source_files, report_spec, report_type)

        log.info(f"Report: {len(retrieved)} chunks from {len(source_files)} source(s)")
        return {
            "context":       context,
            "system_prompt": prompt,
            "retrieved":     len(retrieved),
            "source_files":  source_files,
            "has_content":   True,
        }

    # ── Scope detection ──────────────────────────────────────────

    def _detect_scope(self, question: str, sess: SessionIndex) -> Dict[str, Any]:
        """
        Determine whether the question is:
          - "specific": targets one named entity (fund, person, section…)
          - "general":  asks about everything / all entities in the docs

        Entity matching is 100% dynamic — derived from what was actually
        indexed in this session (via _extract_entities_from_text).
        No hardcoded names.

        Returns a dict with:
          scope       "specific" | "general"
          entity      the matched entity string (if specific), or None
          top_k_hint  suggested top_k for retrieval
        """
        q = question.strip().lower()

        # ── Step 1: Match against DYNAMICALLY extracted entities (highest priority) ──
        # Sort longest-first so "helios flexi cap fund" beats "flexi cap"
        known_entities = sorted(sess.entities, key=len, reverse=True)
        matched_entity = None
        for entity in known_entities:
            if entity in q:
                matched_entity = entity
                break

        if matched_entity:
            return {
                "scope":      "specific",
                "entity":     matched_entity,
                "top_k_hint": 15,
            }

        # ── Step 2: General/all-entity signals ──
        general_signals = [
            "all funds", "each fund", "every fund", "all schemes",
            "compare all", "comparison of all", "overview of all", "list all",
            "summarize all", "summarise all",
            "across all", "for all funds", "all the funds",
        ]
        vague_signals = [
            "explain this", "summarize this", "summarise this",
            "what is this", "describe this", "tell me about this",
            "what does this say", "what is in this",
            "what does this document", "what does this file",
        ]

        if any(s in q for s in general_signals):
            return {"scope": "general", "entity": None, "top_k_hint": 30}

        # Short/vague queries with no entity → general sweep
        if len(q.split()) <= 5 or any(p in q for p in vague_signals):
            return {"scope": "general", "entity": None, "top_k_hint": 30}

        # ── Step 3: Default — no entity detected, treat as specific query ──
        return {"scope": "specific", "entity": None, "top_k_hint": 15}

    # ── Query expansion ─────────────────────────────────────────

    def _expand_query(self, question: str, sess: SessionIndex) -> str:
        scope_info = self._detect_scope(question, sess)

        if scope_info["scope"] == "general":
            doc_context = " ".join(list(sess.doc_names.values())[:5])
            return (
                f"{question} overview all funds all schemes summary "
                f"introduction main points {doc_context}"
            )

        # Specific with named entity: reinforce entity name for better embedding match
        if scope_info.get("entity"):
            return f"{question} {scope_info['entity']}"

        return question


    # ── Dynamic entity extraction ────────────────────────────────

    @staticmethod
    def _extract_entities_from_text(text: str) -> set:
        """
        Dynamically extract named entities (fund names, scheme names, section
        headers, product names, etc.) from raw document text.

        Strategy:
          - Look for repeated Title Case noun phrases (≥2 words, ≥8 chars)
            that appear as headings or labels in the text.
          - Common financial patterns: "X Fund", "X Scheme", "X Plan",
            "X Cap Fund", "X Services Fund", etc.
          - Also capture anything that looks like a proper-noun section header.

        Returns a set of lower-cased entity strings.
        """
        entities = set()

        # Pattern 1: "<Name> Fund / Scheme / Plan / Trust / Portfolio"
        fund_pattern = re.compile(
            r"\b([A-Z][A-Za-z&\s]{3,50}?)"
            r"\s+(Fund|Scheme|Plan|Trust|Portfolio|ETF|Index|Account)\b"
        )
        for m in fund_pattern.finditer(text):
            full = (m.group(1).strip() + " " + m.group(2)).strip()
            if len(full) >= 8:
                entities.add(full.lower())
                # Also add just the prefix (e.g. "helios flexi cap" from "helios flexi cap fund")
                prefix = m.group(1).strip().lower()
                if len(prefix) >= 6:
                    entities.add(prefix)

        # Pattern 2: Standalone Title Case phrases on their own line (headers/labels)
        for line in text.splitlines():
            stripped = line.strip()
            # Must be a short title-case line (2-8 words, no sentence-ending punctuation)
            words = stripped.split()
            if (2 <= len(words) <= 8
                    and not stripped.endswith((".", "?", "!"))
                    and stripped == stripped.title()
                    and len(stripped) >= 8):
                entities.add(stripped.lower())

        # Deduplicate substrings — keep only the longest forms
        sorted_entities = sorted(entities, key=len, reverse=True)
        final = set()
        for e in sorted_entities:
            if not any(e != f and e in f for f in final):
                final.add(e)

        return final

    # ── Embedding ────────────────────────────────────────────────

    def _embed(self, texts: List[str]) -> np.ndarray:
        return self._model.encode(
            texts, batch_size=32,
            show_progress_bar=False,
            normalize_embeddings=True,
        ).astype(np.float32)

    # ── Context formatters ───────────────────────────────────────

    def _fmt_context(self, chunks: List[Dict]) -> str:
        parts = []
        for i, c in enumerate(chunks, 1):
            parts.append(
                f"[CHUNK {i} | Source: {c['source_name']} | "
                f"Part {c['chunk_index']+1}/{c.get('total_chunks','?')} | "
                f"Score: {c.get('score',0):.3f}]\n{c['text']}"
            )
        return "\n\n---\n\n".join(parts)

    def _fmt_context_report(self, chunks: List[Dict], max_chars: int = 150000) -> str:
        """Format context for reports — grouped by source, ordered."""
        by_source: Dict[str, List[Dict]] = {}
        for c in chunks:
            by_source.setdefault(c["source_name"], []).append(c)

        parts = []
        total = 0
        for src, src_chunks in by_source.items():
            header = f"\n\n{'='*60}\nSOURCE: {src}\n{'='*60}\n"
            if total + len(header) > max_chars:
                break
            parts.append(header)
            total += len(header)

            for c in src_chunks:
                if total + len(c["text"]) + 2 > max_chars:
                    parts.append("\n[...content truncated due to length limit...]\n")
                    return "".join(parts)
                parts.append(c["text"] + "\n")
                total += len(c["text"]) + 1

        return "".join(parts)

    # ── System prompts ───────────────────────────────────────────

    def _qa_prompt(self, context: str, sources: List[str], question: str,
                   scope_info: Dict[str, Any] = None) -> str:
        src_list = "\n".join(f"  • {s}" for s in sources)

        # Build scope-aware instruction
        if scope_info and scope_info.get("scope") == "general":
            scope_instruction = """**SCOPE: GENERAL / ALL-ENTITIES QUERY**
- The user is asking about ALL entities (funds, schemes, sections) in the documents.
- You MUST cover EVERY relevant entity found in the excerpts — do NOT limit to just one.
- Organise your answer with a sub-section or bullet per entity/fund/scheme.
- If some entities have the same field (e.g. Top 10 Stocks), list each one separately."""
        elif scope_info and scope_info.get("entity"):
            entity = scope_info["entity"]
            scope_instruction = f"""**SCOPE: SPECIFIC ENTITY — "{entity}"**
- The user is asking specifically about "{entity}".
- Focus your answer ONLY on "{entity}". Do not pad with other entities unless directly relevant.
- If information about "{entity}" is not in the excerpts, say so explicitly."""
        else:
            scope_instruction = """**SCOPE: SPECIFIC QUERY**
- Answer precisely for the entity/topic mentioned in the question.
- Do not pad with unrelated entities unless the user asked for comparisons."""

        return f"""You are Paperly, a precise document Q&A assistant.
Answer EXCLUSIVELY from the document excerpts provided below.

## SOURCES
{src_list}

## SCOPE INSTRUCTION
{scope_instruction}

## RETRIEVED EXCERPTS
{context}

## STRICT RULES
1. Only use the excerpts above. No prior knowledge.
2. Cite every claim: *(Source: filename)*
3. If not found: "The uploaded documents do not contain information about [topic]."
4. Multi-doc: separate by source, then synthesise.
5. Format: ## headers, bullet points, **bold** key terms.
6. End with `**→ Sources used:**` listing files referenced.
7. No filler preamble — answer directly.

## QUESTION
{question}

## ANSWER"""

    def _report_prompt(
        self,
        context:     str,
        sources:     List[str],
        report_spec: str,
        report_type: str,
    ) -> str:
        src_list = "\n".join(f"  • {s}" for s in sources)

        format_guide = {
            "comprehensive": """## REPORT FORMAT
# [Report Title]

## Executive Summary
[2-3 paragraph overview of key findings]

## 1. Introduction
[Background, purpose, scope]

## 2. Key Findings
[Numbered findings with evidence]

## 3. Analysis
[Detailed analysis, patterns, insights]

## 4. Data & Evidence
[Tables, statistics, specific quotes with citations]

## 5. Conclusions
[Summary of what the documents establish]

## 6. Recommendations
[Action items based on document content]

---
**→ Sources:** [list all files used]
**→ Report generated by Paperly** | [date]""",

            "summary": """## REPORT FORMAT
# [Summary Report Title]

## Overview
[Brief 1-paragraph summary]

## Key Points
- Point 1 *(Source: file)*
- Point 2 *(Source: file)*
...

## Conclusion
[1 paragraph]

**→ Sources:** [list files]""",

            "comparison": """## REPORT FORMAT
# Comparative Analysis Report

## Overview
[What is being compared and why]

## Comparison Table
| Aspect | Document A | Document B | ... |
|--------|-----------|-----------|-----|
| Topic  | ...       | ...       |     |

## Detailed Comparison
[Section per key dimension]

## Summary of Differences
[Key divergences]

## Summary of Similarities
[Common themes]

**→ Sources:** [list files]""",

            "technical": """## REPORT FORMAT
# Technical Report

## Abstract
[100-word technical summary]

## 1. Technical Overview
[System/method/process description]

## 2. Specifications & Details
[Technical specs, data, measurements]

## 3. Methodology
[How things work / were done]

## 4. Results & Findings
[Technical outcomes with data]

## 5. Discussion
[Implications, limitations]

## 6. Conclusion
[Technical conclusions]

**→ Sources:** [list files]""",
        }.get(report_type, "")

        return f"""You are Paperly, an expert report writer. Generate a professional {report_type} report
based EXCLUSIVELY on the document content provided below.

## DOCUMENTS USED
{src_list}

## FULL DOCUMENT CONTENT
{context}

## STRICT RULES
1. **Use ONLY the provided document content.** No outside knowledge, no invention.
2. **Every claim must be cited**: *(Source: filename)*
3. **If information is insufficient** for any section, write: "[Insufficient information in provided documents]"
4. **Do not fabricate data, statistics, quotes, or facts** not present in the documents.
5. **Use professional language** appropriate for a formal report.
6. **Structure exactly** as shown in the format guide below.

{format_guide}

## REPORT REQUEST
{report_spec}

## YOUR REPORT (follow format exactly, cite all claims)"""

    def _no_content(self, question: str, reason: str, message: str) -> Dict[str, Any]:
        prompt = f"""You are Paperly, a document Q&A assistant.
No relevant document content was found: {message}

Respond with ONLY this message:

"⚠️ **No relevant content found.**

{message}

**What you can do:**
- Upload the document(s) using the 📎 button (PDF, DOCX, images)
- Paste text directly into the chat input box
- Check that uploaded files show ✓ (indexed) status

Once content is added, ask again."

Question: {question}"""

        return {
            "context":       "",
            "system_prompt": prompt,
            "retrieved":     0,
            "source_files":  [],
            "has_content":   False,
            "reason":        reason,
            "message":       message,
        }
