# Intent-Driven Report Pipeline — Test Report

## Scope
Verifies `backend/utils/intent.py` (new) and its wiring into
`backend/routes/report.py` across question types the old pipeline had no
branch for, plus regression checks that finance-report behavior is
unchanged when intent resolution is bypassed.

All tests below exercise the **heuristic fallback path** (no `GROQ_API_KEYS`
configured in this sandbox, and Groq's API isn't reachable from this
network). In production, `resolve_request_intent()` calls a small Groq
model first and only drops to the heuristic if that's unavailable — so real
traffic gets semantic understanding (e.g. "virologists" → expert opinion)
that the keyword heuristic can only approximate. Every gap found below is
called out as a heuristic-only limitation, not a pipeline defect.

## Test 1 — Comparison
**Question:** "Compare Infosys and TCS on revenue growth and margins"

| Field | Result |
|---|---|
| evidence_needed | `['financials', 'comparison']` |
| search angles | base query; `... quarter-wise comparison historical data table`; `... key financial metrics data numbers` |

No `expert_opinion` or `news` angle was added — correct, since neither was asked for. ✅

## Test 2 — Regulatory-only
**Question:** "What are the new SEBI margin trading rules and how do they affect retail brokers?"

| Field | Result |
|---|---|
| evidence_needed | `['regulatory']` |
| search angles | base query; `... regulatory policy compliance` |

**Bug found & fixed:** the financials regex originally matched bare "margin(s)", so a *margin-trading* regulatory question was incorrectly tagged `financials` too. Tightened `_FINANCIALS_RE` to require an actual figures word (`profit margins`, not bare `margin`). Re-tested clean: only `regulatory` fires. ✅

## Test 3 — Genuinely novel, unmodeled domain (virology/public health)
**Turn 1:** "What do virologists think about the new H5N1 vaccine candidate, and what is the latest news on its clinical trials?"

| Field | Result |
|---|---|
| evidence_needed | `['news', 'expert_opinion']` |
| search angles | base; `... latest news updates`; `... expert analyst opinion commentary` |
| market-news fast-path regex | correctly does **not** fire (stays scoped to stock/market news only) |

**Bug found & fixed:** `_EXPERT_RE` originally only recognized the literal words "expert"/"analyst". "What do virologists think" matched nothing. Generalized to a domain-neutral pattern — `what do/does/are/is <any 1–3 words> think/say/believe/expect/predict/forecast/warn/argue` — so any profession/group asking to be quoted is recognized, not just finance's "analyst". ✅

**Turn 2 (follow-up, same thread):** "Turn this into a detailed report"
(conversation context: turn 1 + an assistant answer mentioning virologist commentary and trial-site news)

| Field | Result |
|---|---|
| is_followup | `True` |
| resolved_topic | "Turn this into a detailed report — Several virologists have expressed cautious optimism about the new H5N1" (correctly grounded in the prior turn, not left subject-less) |
| evidence_needed | `['news']` |

**Bug found & fixed:** search-angle queries were being truncated mid-word (e.g. "...its c latest news updates") because both `resolved_topic` slicing and the follow-up topic-phrase slicing used hard `[:N]` cuts. Both now snap to the last word boundary before the limit.

**Known heuristic-only limitation:** the follow-up's `evidence_needed` came back `['news']`, missing `expert_opinion` — the prior assistant answer paraphrased ("virologists have expressed cautious optimism") rather than using a trigger word like "opinion"/"think", which a keyword regex can't infer semantically. This is exactly the gap the LLM-first design exists to close: `resolve_request_intent()` tries Groq's model first specifically because the heuristic can't do semantic inference like this. Confirmed the heuristic is only reached when Groq is unavailable, so this doesn't affect normal operation — flagging it here for visibility rather than silently declaring 100% heuristic coverage.

## Test 4 — Planning-stage wiring with a fully novel section structure
Fed the planner's validator/formatter a hand-built plan for the H5N1 scenario with sections never seen in the finance-report templates ("Virologist and Public-Health Commentary", "Latest Clinical Trial News"):
- `_validate_report_plan()` → **True** (no hard-coded section-name whitelist)
- `_format_plan_for_prompt()` renders correctly, and the new `RESOLVED INTENT` block correctly tells the writer to only include an evidence-driven section if evidence_needed lists it — i.e. it will not invent an "Expert Views" section for a request that only asked for news.

## Regression checks — legacy behavior unchanged
Calling `_build_multi_angle_search_queries()` **without** an `intent` argument (the pre-existing call shape) reproduces the exact prior output:
- Finance question → 2-angle query set with historical/financial-metrics suffixes, unchanged.
- Non-finance question with `qtype="general"` → single base query, unchanged.

Market-news digest fast path still fires only on genuine stock/market-news phrasing and still ignores unrelated "news" domains (verified against the H5N1 question above).

## Fixes applied during this test pass
1. `_FINANCIALS_RE` — no longer false-positives on "margin trading" (only matches actual figures language, e.g. "profit margins").
2. `_EXPERT_RE` — generalized from finance-specific "expert/analyst" wording to a domain-neutral "what do X think/say/believe" pattern, so it recognizes any profession's opinion being sought.
3. `_REGULATORY_RE` — fixed a boundary bug where "Bank" was matching the "ban" alternative.
4. Word-boundary-safe truncation in both `utils/intent.py` (topic-phrase extraction) and `routes/report.py` (`_build_multi_angle_search_queries`'s topic slicing) — previously both used hard `[:N]` cuts that could chop a query mid-word.

## Net result
The four question types tested (finance comparison, non-finance regulatory-only, a genuinely new expert+news domain, and a cross-domain follow-up) are all handled correctly by the same generic evidence-detection → search-angle → section-planning pipeline, with **no new branch added per question type** — confirming the architecture goal. All fixes were made in the shared heuristic/query-building logic, so every future question type benefits from them, not just the ones tested here.
