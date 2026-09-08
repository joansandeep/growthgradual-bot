
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from types import SimpleNamespace
from routes.report import _filter_relevant_sources, _build_strict_subject_search_queries


def intent(topic, evidence, label):
    return SimpleNamespace(resolved_topic=topic, evidence_needed=evidence, intent_label=label, is_followup=False)


def test_h5n1_relevance_gate_removes_unrelated_sources():
    i = intent("H5N1 avian influenza human transmission risk", ["news", "expert_opinion"], "scientific")
    sources = [
        {"title":"Ebola outbreak update", "snippet":"DRC response funding", "url":"https://example.com/ebola"},
        {"title":"Alzheimer biomarker trial", "snippet":"new dementia research", "url":"https://example.com/alzheimers"},
        {"title":"CDC H5N1 current situation", "snippet":"H5N1 avian influenza human cases and surveillance", "url":"https://cdc.gov/bird-flu"},
        {"title":"WHO H5N1 risk assessment", "snippet":"human transmission, clinical cases and influenza surveillance", "url":"https://who.int/influenza"},
    ]
    kept = _filter_relevant_sources(sources, "Prepare a deep scientific research report on H5N1 avian influenza and human transmission risk", i)
    titles = [x["title"] for x in kept]
    assert titles == ["WHO H5N1 risk assessment", "CDC H5N1 current situation"]


def test_strict_scientific_queries_are_subject_anchored():
    i = intent("H5N1 avian influenza human transmission risk", ["news"], "scientific")
    qs = _build_strict_subject_search_queries.__wrapped__("x", i) if hasattr(_build_strict_subject_search_queries, "__wrapped__") else None
    # Function is async and deterministic; inspect source expectations through a direct async run.
    import asyncio
    qs = asyncio.run(_build_strict_subject_search_queries("Prepare a deep scientific research report on H5N1 avian influenza", i))
    assert all("H5N1" in q or "h5n1" in q.lower() for q in qs)
    assert all("human" in q.lower() or "clinical" in q.lower() or "genomic" in q.lower() for q in qs)


if __name__ == "__main__":
    test_h5n1_relevance_gate_removes_unrelated_sources()
    test_strict_scientific_queries_are_subject_anchored()
    print("PASS: report relevance/query grounding checks")
