"""
Planner-level check proving the presentation spec is correctly wired into
backend/routes/report.py's report-planning result.

This does NOT re-test presentation_schema.py itself (see
backend/utils/test_presentation_schema.py for that) — it only checks the
integration point in the report planner: `_attach_presentation_to_plan`
(used by `_plan_report_structure`) and `_validate_report_plan`.

Run directly:
    python3 backend/routes/test_report_planning_presentation.py

Covers:
  1. A structurally-valid plan WITH a valid "presentation" field is accepted
     and its presentation is validated/normalized through the existing
     ReportPresentationSpec schema module (never trusted as raw JSON).
  2. A plan with an INVALID/malformed "presentation" field is sanitized to a
     safe fallback rather than rejecting the whole plan.
  3. A plan with NO "presentation" field at all (the pre-existing shape)
     still passes validation and is left untouched — full backward
     compatibility with plans that predate this field.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from routes.report import _validate_report_plan, _attach_presentation_to_plan  # noqa: E402
from utils.presentation_schema import ReportPresentationSpec  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _base_plan(extra: dict | None = None) -> dict:
    plan = {
        "depth": "standard",
        "depth_reason": "test",
        "sections": [
            {
                "heading": "Overview",
                "purpose": "Set the scene",
                "format": "prose",
                "format_reason": "qualitative material",
            },
            {
                "heading": "Key Metrics",
                "purpose": "Show the numbers",
                "format": "table",
                "format_reason": "multi-column data",
            },
        ],
        "notes": "",
    }
    if extra:
        plan.update(extra)
    return plan


def test_plan_without_presentation_still_works():
    plan = _base_plan()
    _assert(_validate_report_plan(plan), "existing plan shape (no presentation) must still validate")

    result = _attach_presentation_to_plan(dict(plan))
    _assert("presentation" not in result, "no presentation field must be added when none was given")
    _assert(result["sections"] == plan["sections"], "existing planner fields must be preserved untouched")
    _assert(result["depth"] == plan["depth"], "existing planner fields must be preserved untouched")


def test_plan_with_valid_presentation_is_accepted_and_validated():
    plan = _base_plan({
        "presentation": {
            "domain": "financial",
            "cover": {"treatment": "data_driven", "title": "Acme Q3 Review"},
            "sections": [
                {"id": "metrics", "title": "Key Metrics", "section_type": "metrics_dashboard", "order": 0},
            ],
        },
    })
    _assert(_validate_report_plan(plan), "structural plan validation must not reject an unknown-but-optional key")

    result = _attach_presentation_to_plan(dict(plan))
    _assert("presentation" in result, "valid presentation field must be preserved")
    pres = result["presentation"]
    _assert(isinstance(pres, dict), "presentation must be serialized to a plain JSON-safe dict")
    _assert(pres.get("domain") == "financial", "valid presentation values must round-trip correctly")
    _assert(pres["cover"]["title"] == "Acme Q3 Review", "valid nested presentation values must round-trip correctly")

    # Confirm it is genuinely validated through the schema module, not just
    # copied verbatim: re-hydrating the stored dict must satisfy strict
    # validation (proves it's a fully-formed, schema-shaped spec).
    spec, _warnings = ReportPresentationSpec.from_llm_output(pres)
    spec.validate_strict()  # must not raise


def test_plan_with_invalid_presentation_is_sanitized_not_rejected():
    plan = _base_plan({
        "presentation": {
            "domain": "not-a-real-domain",
            "cover": {"title": "<script>alert(1)</script>Evil Title"},
            "sections": "this should be a list, not a string",
        },
    })
    # The base plan (sections/depth) is still structurally fine, so the
    # overall plan is NOT rejected just because presentation is malformed.
    _assert(_validate_report_plan(plan), "malformed presentation must not fail the base plan's structural check")

    result = _attach_presentation_to_plan(dict(plan))
    _assert("presentation" in result, "malformed presentation must be sanitized, not dropped/rejected")
    pres = result["presentation"]
    _assert(pres.get("domain") == "generic", "unknown domain must fall back to a safe default")
    _assert("<script>" not in pres["cover"]["title"], "markup must be stripped from presentation free-text fields")
    _assert(isinstance(pres.get("sections"), list), "invalid sections must fall back to a safe (empty) list")

    # Must still safely re-parse through the schema module without raising
    # (an empty sections list is a valid, if minimal, fallback outcome —
    # strict validation is intentionally not required here, since the whole
    # point of from_llm_output is to tolerate this kind of malformed input).
    spec, _warnings = ReportPresentationSpec.from_llm_output(pres)
    _assert(isinstance(spec, ReportPresentationSpec), "sanitized presentation must re-parse to a valid spec object")


def test_plan_with_presentation_raising_is_defused():
    # Simulate a value the schema module can't coerce at all (e.g. a plain
    # string instead of an object) — from_llm_output() handles this itself,
    # but _attach_presentation_to_plan must be robust even if that guarantee
    # ever changes: the whole plan must never be lost over this field.
    plan = _base_plan({"presentation": "just a string, not an object"})
    _assert(_validate_report_plan(plan), "structural check does not depend on presentation shape")

    result = _attach_presentation_to_plan(dict(plan))
    _assert(result["sections"] == plan["sections"], "base plan fields must survive even a bad presentation value")
    if "presentation" in result:
        _assert(isinstance(result["presentation"], dict), "if kept, presentation must be a safe dict")


def run_all():
    tests = [
        test_plan_without_presentation_still_works,
        test_plan_with_valid_presentation_is_accepted_and_validated,
        test_plan_with_invalid_presentation_is_sanitized_not_rejected,
        test_plan_with_presentation_raising_is_defused,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures.append((t.__name__, e))
            print(f"FAIL: {t.__name__}: {e}")

    print()
    if failures:
        print(f"{len(failures)} of {len(tests)} test(s) FAILED")
        sys.exit(1)
    print(f"All {len(tests)} test(s) passed.")


if __name__ == "__main__":
    run_all()
