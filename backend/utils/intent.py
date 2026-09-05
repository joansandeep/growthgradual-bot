"""
Generic request-intent resolver
================================
Single decision point for the questions that used to be answered by a pile
of topic-specific regexes scattered through routes/report.py ("is this a
market-news digest?", "is this a stock question?", "is this a tax
question?", ...). Each of those was a hard-coded branch for ONE kind of
request; a brand-new kind of question (e.g. "what are analysts saying about
the RBI's rate decision") matched none of them and fell back to generic,
undifferentiated handling no matter how well the branch pattern worked for
the cases it *did* cover.

This module asks the model instead: for ANY request, in ANY domain —

  1. what is the user actually asking (a short free-text intent label),
  2. what topic/entity does it refer to, resolving pronouns/follow-ups
     ("give it as a report", "make that a report", "analyze this") against
     the recent conversation,
  3. whether it's a follow-up at all,
  4. which KINDS of evidence would actually help answer it, drawn from a
     small controlled vocabulary (news, expert/analyst opinion, financials,
     market data, regulatory information, historical data, comparisons,
     uploaded documents, or none — general knowledge is enough), and
  5. whether the user wants a formal report vs. a quick answer.

Nothing here is specific to finance, or to news, or to any named company —
new question types are handled by the SAME call deciding a different
`evidence_needed` set, not by adding another branch to this file.

Callers (chat orchestrator, report pipeline) use `evidence_needed` to decide
which tools/searches to run and `resolved_topic`/`is_followup` to ground a
short follow-up before researching. Nothing downstream is required to use
every field — e.g. a report request only cares about evidence types that
map to a web search angle, chat may only care about resolved_topic.

Degrades gracefully: if no Groq key is configured, every key is rate
limited, or the model returns something unparseable, `resolve_request_intent`
falls back to `_heuristic_intent` — a deterministic, keyword-based, still
domain-neutral guess — and marks `source="heuristic"` so callers/logs can
tell the difference. It never raises and never returns None.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

from utils.keys import get_groq_keys, is_rate_limited, mark_rate_limited, round_robin

log = logging.getLogger("intent")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Small/fast model — this is one short JSON completion, not a report body.
_INTENT_MODEL = "openai/gpt-oss-20b"

# Controlled vocabulary of evidence *kinds*, not topics. New question types
# are handled by the model picking a different subset of these — never by
# adding a new entry here per-topic.
EVIDENCE_TYPES = [
    "news",             # recent headlines / current events
    "expert_opinion",   # analyst/expert commentary, ratings, opinions
    "financials",       # company/entity financial figures, fundamentals
    "market_data",      # live prices, index levels, OHLC, market moves
    "regulatory",       # laws, policy, compliance, regulator actions
    "historical",       # trends / time series / past performance
    "comparison",       # explicit comparison across named items
    "documents",        # the user's own uploaded file(s) / indexed docs
    "general_knowledge",  # no retrieval needed — model knowledge suffices
]

_VALID_EVIDENCE = set(EVIDENCE_TYPES)


@dataclass
class RequestIntent:
    resolved_topic: str = ""
    is_followup: bool = False
    intent_label: str = ""
    evidence_needed: list[str] = field(default_factory=lambda: ["general_knowledge"])
    wants_report: bool = False
    notes: str = ""
    source: str = "heuristic"  # "llm" | "heuristic" | "none"

    def needs(self, evidence_type: str) -> bool:
        return evidence_type in self.evidence_needed

    def as_meta(self) -> dict:
        return {
            "resolvedTopic": self.resolved_topic,
            "isFollowup": self.is_followup,
            "intent": self.intent_label,
            "evidenceNeeded": self.evidence_needed,
            "wantsReport": self.wants_report,
            "source": self.source,
        }


_SYSTEM_PROMPT = f"""You are the intent-resolution stage of a general-purpose research/report assistant that must handle ANY topic — finance, business, science, technology, policy, health, sport, culture, or anything else — with no fixed list of question types.

Given the user's latest message and, if provided, the recent conversation, decide:

1. "resolved_topic": the actual subject the request is about, in a short phrase. If the message is a short follow-up that leans on a pronoun or has no subject of its own ("give it as a report", "make that a report", "analyze this", "what about its peers", "give me more detail"), resolve it from the conversation — name the real company/person/topic/event being discussed. If the message already names its own subject, just restate that subject; do not go looking for a different one in the conversation.
2. "is_followup": true only if you had to lean on the conversation to know what the message is about.
3. "intent_label": a short free-text label for what the user wants (e.g. "latest news", "expert/analyst commentary", "financial analysis", "comparison", "explainer", "report request", "casual conversation"). Do not pick from a fixed list — describe it in a few words.
4. "evidence_needed": pick ONLY the evidence kinds from this exact set that this SPECIFIC request genuinely needs — {sorted(_VALID_EVIDENCE)}. Be conservative and literal:
   - Include "news" only if the user is asking about recent headlines/current events/what's happening.
   - Include "expert_opinion" ONLY if the user explicitly asks for expert, analyst, or third-party opinions/commentary/ratings/views — never add it just because the topic is finance or business. A plain "latest news on X" must NOT include expert_opinion.
   - Include "financials" / "market_data" only if real figures, prices, or fundamentals matter to the answer.
   - Include "regulatory" only if laws/policy/compliance/regulators are actually relevant.
   - Include "historical" only if trend/time-series/past-performance matters.
   - Include "comparison" only if the request explicitly compares two or more named items.
   - Include "documents" only if the request refers to the user's own uploaded file/attachment.
   - If nothing above genuinely applies and your own knowledge is enough, use exactly ["general_knowledge"].
   - Never include every category "to be safe" — an unnecessary evidence type leads to sections/searches the user didn't ask for.
5. "wants_report": true if the user is asking for a report/document/write-up rather than a short conversational answer.
6. "notes": one short sentence explaining the evidence_needed choice, or empty string.

Respond with ONLY this JSON shape, no other text:
{{"resolved_topic": "...", "is_followup": true|false, "intent_label": "...", "evidence_needed": ["..."], "wants_report": true|false, "notes": "..."}}"""


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1], strict=False)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _coerce_intent(parsed: dict, fallback_question: str) -> RequestIntent | None:
    if not isinstance(parsed, dict):
        return None
    resolved_topic = str(parsed.get("resolved_topic") or "").strip() or fallback_question
    evidence = parsed.get("evidence_needed")
    if not isinstance(evidence, list):
        evidence = []
    evidence = [e for e in evidence if isinstance(e, str) and e in _VALID_EVIDENCE]
    if not evidence:
        evidence = ["general_knowledge"]
    return RequestIntent(
        resolved_topic=resolved_topic,
        is_followup=bool(parsed.get("is_followup")),
        intent_label=str(parsed.get("intent_label") or "").strip(),
        evidence_needed=evidence,
        wants_report=bool(parsed.get("wants_report")),
        notes=str(parsed.get("notes") or "").strip(),
        source="llm",
    )


# ─── Heuristic fallback ──────────────────────────────────────────────────────
# Deterministic and keyword-based, but keyed on generic EVIDENCE KINDS, not
# on any specific topic/company/domain — the same fallback shape applies
# whether the request is about a stock, a drug approval, or a sports league.
_FOLLOWUP_WORD_RE = re.compile(
    r"\b(it|its|that|this|those|these|they|them|the same|"
    r"give it|make that|turn (?:it|that|this)|analyze this|analyse this)\b",
    re.IGNORECASE,
)
_REPORT_WORD_RE = re.compile(r"\b(report|write[- ]up|document|detailed analysis|deep[- ]dive)\b", re.IGNORECASE)
_NEWS_RE = re.compile(r"\b(news|headlines?|latest|breaking|today'?s|recent(?:ly)?\s+(?:developments?|events?))\b", re.IGNORECASE)
# Deliberately NOT anchored to finance-specific titles ("expert"/"analyst")
# alone — a domain-neutral "what do <some group> think/say" shape catches
# any profession (virologists, economists, historians, engineers, ...)
# asking for third-party opinion, without a per-domain word list.
_EXPERT_RE = re.compile(
    r"\b(experts?|analysts?|commentary|viewpoints?|opinions?|ratings?|"
    r"what\s+(?:do|does|are|is)\s+\w+(?:\s+\w+){0,2}\s+"
    r"(?:think|say|believe|expect|predict|forecast|warn|argue))\b",
    re.IGNORECASE,
)
# "margin[s]" alone is ambiguous (leveraged "margin trading" vs. profit
# "margins") — only treat it as a financials signal alongside another
# genuinely financial-figures word, not on its own.
_FINANCIALS_RE = re.compile(r"\b(revenue|profit|earnings|financials?|fundamentals?|valuation|p/?e ratio|balance sheet|profit margins?)\b", re.IGNORECASE)
_MARKET_DATA_RE = re.compile(r"\b(price|prices|stock price|share price|index|market cap|nifty|sensex|ohlc|quote)\b", re.IGNORECASE)
_REGULATORY_RE = re.compile(
    r"\b(regulat\w*|complian\w*|sebi|rbi|polic(?:y|ies)|law|laws|legislation|"
    r"\bban\b|banned|banning|approval|approvals|licen[cs]e[sd]?)\b",
    re.IGNORECASE,
)
_HISTORICAL_RE = re.compile(r"\b(trend|historical|history|over time|past \d+|since \d{4}|quarter[- ]on[- ]quarter|year[- ]on[- ]year|yoy|qoq)\b", re.IGNORECASE)
_COMPARISON_RE = re.compile(r"\bvs\.?\b|\bversus\b|\bcompar", re.IGNORECASE)


def _heuristic_intent(
    question: str, conversation_context: str, has_rag: bool, has_files: bool,
) -> RequestIntent:
    q = question or ""
    is_followup = bool(conversation_context.strip()) and (
        len(q.split()) <= 8 and bool(_FOLLOWUP_WORD_RE.search(q))
    )
    resolved_topic = q
    last_assistant = ""
    if is_followup:
        # Same lightweight grounding technique used elsewhere in the
        # pipeline: pull a short topic phrase out of the last assistant
        # turn rather than leaving the follow-up subject-less.
        turns = [t.strip() for t in conversation_context.split("\n\n") if t.strip()]
        for t in reversed(turns):
            if t.startswith("Assistant:"):
                last_assistant = t[len("Assistant:"):].strip()
                break
        if last_assistant:
            first_sentence = re.split(r"[.\n]", last_assistant, maxsplit=1)[0].strip()
            # Truncate on a word boundary, not mid-word — a hard [:80] slice
            # can cut a query-bound phrase in half (e.g. "...its c").
            topic_phrase = first_sentence[:80]
            if len(first_sentence) > 80:
                topic_phrase = topic_phrase.rsplit(" ", 1)[0]
            if topic_phrase:
                resolved_topic = f"{q} — {topic_phrase}" if q.lower() not in topic_phrase.lower() else topic_phrase

    # Scan for evidence signals over the FULL last answer (capped, but much
    # more than the short topic_phrase used for the search-query string
    # above) — a follow-up like "turn this into a report" carries no
    # evidence signal of its own, and the keyword that reveals what kind of
    # evidence the follow-up still needs (e.g. "news", "virologists...
    # think") often sits later in the prior answer than the first ~80
    # characters, which is all resolved_topic keeps for query brevity.
    scan_text = f"{q} {last_assistant[:800]}" if is_followup else q

    evidence: list[str] = []
    if _NEWS_RE.search(scan_text):
        evidence.append("news")
    if _EXPERT_RE.search(scan_text):
        evidence.append("expert_opinion")
    if _FINANCIALS_RE.search(scan_text):
        evidence.append("financials")
    if _MARKET_DATA_RE.search(scan_text):
        evidence.append("market_data")
    if _REGULATORY_RE.search(scan_text):
        evidence.append("regulatory")
    if _HISTORICAL_RE.search(scan_text):
        evidence.append("historical")
    if _COMPARISON_RE.search(scan_text):
        evidence.append("comparison")
    if has_rag or has_files:
        evidence.append("documents")
    if not evidence:
        evidence = ["general_knowledge"]

    return RequestIntent(
        resolved_topic=resolved_topic,
        is_followup=is_followup,
        intent_label="report request" if _REPORT_WORD_RE.search(q) else "",
        evidence_needed=evidence,
        wants_report=bool(_REPORT_WORD_RE.search(q)),
        notes="heuristic fallback — keyword-based evidence detection",
        source="heuristic",
    )


async def resolve_request_intent(
    question: str,
    conversation_context: str = "",
    has_rag: bool = False,
    has_files: bool = False,
    timeout: float = 8.0,
) -> RequestIntent:
    """Never raises, never returns None. Falls back to a deterministic
    heuristic (see module docstring) when the LLM call is unavailable or
    fails."""
    question = (question or "").strip()
    if not question:
        return RequestIntent(source="none")

    keys = get_groq_keys()
    if keys:
        user_content = question[:800]
        if conversation_context.strip():
            user_content = (
                f"RECENT CONVERSATION (context only, for resolving a pronoun or "
                f"subject-less follow-up):\n{conversation_context[-1500:]}\n\n"
                f"LATEST MESSAGE: {question[:800]}"
            )
        for key in round_robin(keys)[:3]:
            if is_rate_limited(f"{key}:{_INTENT_MODEL}"):
                continue
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    res = await client.post(
                        GROQ_URL,
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                        json={
                            "model": _INTENT_MODEL,
                            "messages": [
                                {"role": "system", "content": _SYSTEM_PROMPT},
                                {"role": "user", "content": user_content},
                            ],
                            "max_tokens": 400,
                            "temperature": 0.1,
                            "response_format": {"type": "json_object"},
                        },
                    )
                if res.status_code == 429:
                    mark_rate_limited(f"{key}:{_INTENT_MODEL}", 60_000)
                    continue
                if not res.is_success:
                    continue
                content = res.json()["choices"][0]["message"]["content"]
                parsed = _extract_json(content)
                intent = _coerce_intent(parsed, question) if parsed else None
                if intent:
                    log.info(
                        "Intent resolved via LLM: topic=%r followup=%s evidence=%s wants_report=%s",
                        intent.resolved_topic[:80], intent.is_followup, intent.evidence_needed, intent.wants_report,
                    )
                    return intent
            except Exception as exc:
                log.debug("Intent-resolution Groq call failed on key ...%s: %s", key[-4:], exc)
                continue

    log.info("Intent resolution: LLM unavailable/exhausted — using heuristic fallback")
    return _heuristic_intent(question, conversation_context, has_rag, has_files)
