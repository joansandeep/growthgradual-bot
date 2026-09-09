"""Focused search and Selenium-fallback regression checks."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routes.chat import classify_query, optimize_search_query
from routes.report import _build_multi_angle_search_queries
import utils.selenium_fetch as selenium_fetch


def test_scientific_query_not_finance():
    q = "Prepare a deep scientific research report on H5N1 avian influenza and human transmission risk"
    assert classify_query(q) == "general"
    qs = optimize_search_query(q, qtype="general")
    assert qs and all("Prepare" not in item for item in qs)


def test_selenium_fallback_is_bounded_and_testable():
    old = selenium_fetch._fetch_sync
    try:
        selenium_fetch._fetch_sync = lambda url, max_chars: "JS-rendered article body " + ("x" * 500)
        result = asyncio.run(selenium_fetch.fetch_js_page("https://example.com", 200))
        assert result.startswith("JS-rendered article body")
        assert len(result) <= 200
    finally:
        selenium_fetch._fetch_sync = old


def test_report_search_builder_strips_instruction_prefix():
    q = "Prepare a deep scientific research report on H5N1 avian influenza and the current state of human transmission risk"
    built = asyncio.run(_build_multi_angle_search_queries(q, "", "general", None))
    assert built
    assert any("H5N1" in x for x in built)
    assert not any(x.lower().startswith("prepare a deep") for x in built)


if __name__ == "__main__":
    test_scientific_query_not_finance()
    test_selenium_fallback_is_bounded_and_testable()
    test_report_search_builder_strips_instruction_prefix()
    print("search/selenium checks passed")
