"""
Orchestrator — one bounded LLM tool-calling loop
================================================
This is the single decision point that replaced the old fixed pipeline. The
previous ``/api/chat`` handler always ran the same three fetches (web search +
cached headlines + company-fundamentals lookup) for every non-greeting message,
and only consulted the document index when the *frontend* set a ``hasRag``
flag. Which persona answered was decided by a keyword classifier that could
only ever return "finance" or "general".

Here, instead, Groq is asked — through native OpenAI-style function calling —
which of the registered tools (if any) it actually needs for THIS message. It
may call none (answer from model knowledge), one, or several; it may look at
what came back and call another. There is no query taxonomy anywhere in this
module, and no tool runs unless it was chosen.

Shape:

    plan  ->  execute (concurrently)  ->  optionally plan again  ->  hand the
    gathered context blocks + sources back to the caller for synthesis

Guarantees the caller can rely on:

* ``orchestrate()`` never raises and never returns ``None``.
* If Groq tool-calling is unavailable (no keys, all keys banned, malformed
  response, network failure), it degrades to ``_heuristic_plan`` — a
  deterministic, domain-neutral plan — and says so in ``planner``.
* Every tool failure is recorded in ``limitations`` rather than propagated, so
  a broken tool downgrades the answer instead of breaking the request.
* Synthesis itself is NOT done here — the caller keeps using the existing
  streaming Groq → Gemini path, so the SSE contract is untouched.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

from utils.keys import get_groq_keys, is_rate_limited, mark_rate_limited, round_robin
from utils.tools import (
    ToolContext,
    ToolResult,
    build_toolset,
    execute_tools,
    tool_schemas,
)

log = logging.getLogger("orchestrator")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Same model family the streaming path already uses, overridable for
# deployments that enable a different tool-calling model on their Groq tier.
# llama-3.3-70b-versatile was decommissioned by Groq on 2026-08-16 (404
# model_not_found). Default updated to Groq's recommended successor, which
# also supports tool/function calling (required for the planner). Override
# via GROQ_TOOL_MODEL if needed.
PLANNER_MODEL = os.environ.get("GROQ_TOOL_MODEL", "openai/gpt-oss-120b").strip()

# Hard bound on planning rounds. 2 is enough for "search, look, refine once";
# more rounds mostly buy latency. Configurable but clamped.
MAX_ROUNDS = max(1, min(4, int(os.environ.get("ORCHESTRATOR_MAX_ROUNDS", "2") or 2)))
PLANNER_TIMEOUT = httpx.Timeout(connect=8, read=25, write=15, pool=10)


@dataclass
class OrchestrationResult:
    context_blocks: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    rounds: int = 0
    planner: str = "none"          # groq-tool-calling | heuristic | skipped
    planner_note: str = ""

    @property
    def tools_used(self) -> list[str]:
        return [r.name for r in self.results]

    def result_for(self, name: str) -> ToolResult | None:
        return next((r for r in self.results if r.name == name), None)

    def context(self) -> str:
        return "".join(self.context_blocks)

    def as_meta(self) -> list[dict]:
        return [r.as_meta() for r in self.results]


def _planner_system_prompt(toolset_names: list[str]) -> str:
    today = date.today().strftime("%A, %B %d, %Y")
    return f"""You are the planning stage of a general-purpose research assistant. Today is {today}.

Your ONLY job right now is to decide which tools — if any — are needed to answer the user's latest message accurately. You must NOT answer the question yourself.

You serve every subject equally: science, technology, programming, history, health, politics, sport, culture, business and finance. Do not assume any particular domain. Decide purely from what the message needs.

How to decide:
- **Call no tools at all** when the answer is timeless, conceptual or self-contained and you are confident from your own knowledge: definitions, explanations of how something works, mathematics, reasoning about code or an error message, writing/editing help, translation, or plain conversation. Retrieval adds latency and noise for these — skip it.
- **Call web_search** when the answer depends on information that changes, is recent, or that you cannot verify from memory: news and current events, "latest"/"today"/"this week"/"{date.today().year}" questions, prices and market levels, who currently holds a position, release versions, statistics, or any claim the user would expect to be up to date. Also call it when you are genuinely unsure whether your knowledge is still current — it is much worse to answer stale than to search.
- **Call document_search** when the message refers to the user's own uploaded material ("my file", "the PDF", "this document", "the attachment") or asks about content that would only exist in their upload.
- **Call the data tools** (company_fundamentals, market_data) only when a specific named company's real figures or a live market level actually matter to the answer.
- You may call several tools in one go when they cover genuinely different needs.
- Write short, focused search queries — not the user's whole sentence. Omit `region` unless the question is specifically about one country.

Available tools this turn: {', '.join(toolset_names) or 'none'}.

If no tool is needed, reply with exactly: NONE"""


def _messages_for_planner(messages: list[dict], user_message: str) -> list[dict]:
    """Last few turns only — the planner needs intent, not the whole transcript."""
    trimmed: list[dict] = []
    for m in [m for m in messages if m.get("role") in ("user", "assistant")][-5:]:
        content = str(m.get("content") or "")[:1500]
        if content:
            trimmed.append({"role": m["role"], "content": content})
    if not trimmed or trimmed[-1]["role"] != "user":
        trimmed.append({"role": "user", "content": user_message[:1500]})
    return trimmed


def _parse_tool_calls(message: dict) -> list[tuple[str, dict, str]]:
    """Extract (name, args, call_id) triples from a Groq assistant message."""
    calls = []
    for call in (message.get("tool_calls") or []):
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args or "{}")
            except (TypeError, ValueError):
                log.warning("Planner sent unparseable arguments for %s — using empty args", name)
                args = {}
        calls.append((name, args if isinstance(args, dict) else {}, call.get("id") or name))
    return calls


async def _groq_plan(payload_messages: list[dict], schemas: list[dict]) -> dict | None:
    """
    One non-streaming Groq call with tools attached. Returns the assistant
    message dict, or None when every key/attempt is exhausted. Never raises.
    """
    keys = get_groq_keys()
    if not keys:
        log.info("Planner: no Groq keys configured — using heuristic plan")
        return None

    candidates = [k for k in round_robin(keys) if not is_rate_limited(k)] or keys[:1]
    for key in candidates[:3]:
        try:
            async with httpx.AsyncClient(timeout=PLANNER_TIMEOUT) as client:
                res = await client.post(
                    GROQ_URL,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json={
                        "model": PLANNER_MODEL,
                        "messages": payload_messages,
                        "tools": schemas,
                        "tool_choice": "auto",
                        "temperature": 0.1,
                        "max_tokens": 512,
                    },
                )
            if res.status_code == 429:
                retry_ms = 30_000
                try:
                    retry_ms = int(float(res.headers.get("retry-after", "30")) * 1000)
                except (TypeError, ValueError):
                    pass
                log.warning("Planner: Groq 429 on key ...%s — backing off %dms", key[-4:], retry_ms)
                mark_rate_limited(key, retry_ms)
                continue
            if res.status_code in (401, 403):
                log.warning("Planner: Groq auth error on key ...%s — banning 24h", key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            if not res.is_success:
                log.warning("Planner: Groq HTTP %d — %s", res.status_code, res.text[:180])
                continue
            choices = (res.json().get("choices") or [])
            if not choices:
                log.warning("Planner: Groq returned no choices")
                continue
            return choices[0].get("message") or {}
        except Exception as exc:
            log.warning("Planner: Groq call failed on key ...%s: %s: %s",
                        key[-4:], type(exc).__name__, exc)
            continue
    return None


def _heuristic_plan(ctx: ToolContext, toolset_names: list[str], search_queries: list[str]) -> list[tuple[str, dict]]:
    """
    Deterministic fallback used only when the planner LLM is unreachable.

    Deliberately domain-neutral: it asks the existing, subject-agnostic
    ``needs_web_search`` helper whether the message looks like it needs
    external information, and offers the document index when the session has
    documents. ``company_fundamentals`` is included when its store is
    configured because that lookup is self-gating — it matches on a company
    name actually present in the message and returns nothing otherwise — so it
    cannot force an unrelated question ("what is quantum computing?") down a
    financial path.
    """
    from routes.chat import needs_web_search

    calls: list[tuple[str, dict]] = []
    if "document_search" in toolset_names:
        calls.append(("document_search", {"query": search_queries[0] if search_queries else ctx.user_message}))
    if "web_search" in toolset_names and needs_web_search(ctx.user_message, has_files=ctx.has_files):
        calls.append(("web_search", {"queries": search_queries[:3] or [ctx.user_message]}))
    if "company_fundamentals" in toolset_names:
        calls.append(("company_fundamentals", {"companies": []}))
    return calls


async def orchestrate(
    *,
    user_message: str,
    messages: list[dict],
    session_id: str = "",
    has_rag: bool = False,
    has_files: bool = False,
    search_queries: list[str] | None = None,
    max_rounds: int | None = None,
    skip: bool = False,
) -> OrchestrationResult:
    """
    Decide which tools this message needs, run them, and return the gathered
    context. Never raises.

    ``skip=True`` short-circuits everything (used for greetings/small talk):
    no planner call, no tools, no latency.

    ``search_queries`` is only a hint for the heuristic fallback — when the
    planner LLM is working it writes its own queries.
    """
    out = OrchestrationResult()
    ctx = ToolContext(
        session_id=session_id,
        user_message=user_message,
        has_rag=has_rag,
        has_files=has_files,
    )

    if skip:
        out.planner = "skipped"
        out.planner_note = "conversational turn — no retrieval attempted"
        log.info("Orchestrator: skipped (conversational turn)")
        return out

    toolset = build_toolset(ctx)
    if not toolset:
        out.planner = "skipped"
        out.planner_note = "no tools are configured/available for this request"
        out.limitations.append(
            "No retrieval tools were available for this request — the answer relies on model knowledge only."
        )
        log.warning("Orchestrator: no tools available — answering from model knowledge")
        return out

    toolset_names = [t.name for t in toolset]
    schemas = tool_schemas(toolset)
    rounds = max(1, min(4, max_rounds or MAX_ROUNDS))
    queries = [q for q in (search_queries or []) if q and q.strip()] or [user_message]

    convo: list[dict] = [{"role": "system", "content": _planner_system_prompt(toolset_names)}]
    convo += _messages_for_planner(messages, user_message)

    t_plan = time.perf_counter()
    used_names: set[str] = set()
    planner_failed = False

    for round_no in range(1, rounds + 1):
        assistant_msg = await _groq_plan(convo, schemas)

        if assistant_msg is None:
            planner_failed = True
            break

        calls = _parse_tool_calls(assistant_msg)
        if not calls:
            note = (assistant_msg.get("content") or "").strip()[:200]
            log.info(
                "Orchestrator round %d: planner chose no tools (%s) — answering from model knowledge",
                round_no, note or "no note",
            )
            out.planner = "groq-tool-calling"
            out.rounds = round_no
            if round_no == 1:
                out.planner_note = "planner decided no external retrieval was needed"
            break

        # Drop repeats of a tool already run this request — the planner
        # occasionally re-requests an identical call after seeing its output.
        fresh_calls = [c for c in calls if c[0] not in used_names]
        if not fresh_calls:
            log.info("Orchestrator round %d: planner only re-requested already-run tools — stopping",
                     round_no)
            out.planner = "groq-tool-calling"
            out.rounds = round_no
            break

        log.info(
            "Orchestrator round %d: planner selected %s",
            round_no, ", ".join(f"{n}({', '.join(sorted(a))})" for n, a, _ in fresh_calls),
        )

        results = await execute_tools([(n, a) for n, a, _ in fresh_calls], ctx)

        # Record the assistant's tool_calls turn, then one tool message per
        # call — required by the OpenAI/Groq protocol for the next round.
        convo.append({
            "role": "assistant",
            "content": assistant_msg.get("content") or "",
            "tool_calls": assistant_msg.get("tool_calls") or [],
        })
        # Any call we filtered out still needs a tool message, or the protocol
        # breaks on the next round (every tool_call id must be answered).
        answered: dict[str, str] = {}
        for (name, _args, call_id), result in zip(fresh_calls, results):
            used_names.add(name)
            out.results.append(result)
            if result.context:
                out.context_blocks.append(result.truncated_context())
            out.sources.extend(result.sources)
            if not result.ok and result.error:
                out.limitations.append(f"{name}: {result.error}")
            answered[call_id] = result.summary or (result.error or "no output")
        for name, _args, call_id in calls:
            convo.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": answered.get(call_id, "Skipped — this tool already ran for this request."),
            })

        out.planner = "groq-tool-calling"
        out.rounds = round_no

        # Nothing usable came back and we still have a round left — let the
        # planner see that and try a different angle.
        if round_no >= rounds:
            break

    if planner_failed:
        out.planner = "heuristic"
        out.planner_note = "planner LLM unavailable — used deterministic fallback plan"
        out.limitations.append(
            "Tool selection fell back to a heuristic plan because the planning model was unavailable."
        )
        calls = _heuristic_plan(ctx, toolset_names, queries)
        log.warning(
            "Orchestrator: planner unavailable — heuristic plan = %s",
            ", ".join(n for n, _ in calls) or "no tools",
        )
        if calls:
            for result in await execute_tools(calls, ctx):
                out.results.append(result)
                if result.context:
                    out.context_blocks.append(result.truncated_context())
                out.sources.extend(result.sources)
                if not result.ok and result.error:
                    out.limitations.append(f"{result.name}: {result.error}")
            out.rounds = 1

    # Dedupe sources by URL, preserving first-seen order.
    seen: set[str] = set()
    deduped: list[dict] = []
    for s in out.sources:
        url = s.get("url") or ""
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        deduped.append(s)
    out.sources = deduped

    log.info(
        "Orchestrator done: planner=%s rounds=%d tools=[%s] sources=%d context=%dch "
        "limitations=%d in %dms",
        out.planner, out.rounds, ", ".join(out.tools_used) or "none",
        len(out.sources), len(out.context()), len(out.limitations),
        int((time.perf_counter() - t_plan) * 1000),
    )
    return out

