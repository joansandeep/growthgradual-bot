"""Regression tests for report search-source diversity."""
import asyncio
from utils.intent import RequestIntent
from routes.report import _build_multi_angle_search_queries


def run(coro):
    return asyncio.run(coro)


def main():
    intent = RequestIntent(
        resolved_topic="Tata Motors",
        is_followup=False,
        intent_label="report request",
        evidence_needed=["general_knowledge"],
        wants_report=True,
        notes="",
        source="llm",
    )
    queries = run(_build_multi_angle_search_queries(
        "Give me a research report on Tata Motors",
        "",
        "finance",
        intent,
    ))
    assert len(queries) >= 4, queries
    lowered = " || ".join(queries).lower()
    assert "latest developments" in lowered
    assert "financial results" in lowered
    assert "strategy" in lowered
    print("PASS: finance/company source-diversity search produces multiple independent angles")


if __name__ == "__main__":
    main()
