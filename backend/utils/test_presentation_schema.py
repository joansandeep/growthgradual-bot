"""
Self-contained validation check for backend/utils/presentation_schema.py.

Run directly:
    python3 backend/utils/test_presentation_schema.py

Or with pytest, if available:
    python3 -m pytest backend/utils/test_presentation_schema.py -v

Covers:
  1. A valid financial-style presentation spec.
  2. A valid regulatory-style presentation spec.
  3. An invalid/malformed spec that must safely fall back (lenient path)
     and must fail strict validation (strict path).
  4. A markup-injection attempt is stripped rather than rendered as-is.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from presentation_schema import (  # noqa: E402
    ReportPresentationSpec,
    ReportDomain,
    SectionType,
    LayoutVariant,
    ExecutiveSummaryPlacement,
    SourcePlacement,
    TitleTreatment,
    SchemaValidationError,
    default_spec_for_domain,
    contains_markup,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_valid_financial_spec():
    payload = {
        "domain": "financial",
        "cover": {
            "enabled": True,
            "title": "Acme Corp — Q3 Earnings Review",
            "subtitle": "Prepared for the Investment Committee",
            "treatment": "data_driven",
        },
        "executive_summary": {
            "placement": "after_cover",
            "body": "Revenue grew 12% YoY driven by strong cloud segment performance.",
            "key_metrics": [
                {"label": "Revenue", "value": "$1.2B", "trend": "up", "change_pct": 12.0},
                {"label": "Net Margin", "value": "18%", "trend": "flat"},
            ],
        },
        "sections": [
            {
                "id": "metrics",
                "title": "Key Metrics",
                "section_type": "metrics_dashboard",
                "layout": "grid",
                "order": 0,
                "blocks": [
                    {
                        "kind": "metrics",
                        "title": "Headline KPIs",
                        "items": [
                            {"label": "Revenue", "value": "$1.2B", "trend": "up"},
                            {"label": "EPS", "value": "$2.13", "trend": "up"},
                        ],
                    }
                ],
            },
            {
                "id": "financials",
                "title": "Financial Performance",
                "section_type": "financials",
                "layout": "two_column",
                "order": 1,
                "blocks": [
                    {
                        "kind": "table",
                        "title": "Income Statement",
                        "columns": ["Line Item", "Q3 2026", "Q3 2025"],
                        "rows": [["Revenue", "$1.2B", "$1.07B"], ["Net Income", "$220M", "$190M"]],
                    },
                    {
                        "kind": "chart",
                        "title": "Revenue Trend",
                        "chart_type": "bar",
                        "x_labels": ["Q1", "Q2", "Q3"],
                        "series": [{"name": "Revenue ($M)", "values": [1000, 1100, 1200]}],
                    },
                ],
            },
            {
                "id": "risks",
                "title": "Risk Factors",
                "section_type": "risk_assessment",
                "order": 2,
                "blocks": [
                    {
                        "kind": "risk_matrix",
                        "title": "Key Risks",
                        "risks": [
                            {"name": "FX exposure", "likelihood": "medium", "impact": "high",
                             "mitigation": "Hedging program in place"},
                        ],
                    }
                ],
            },
        ],
        "source_appendix": {"placement": "end_of_report"},
    }

    spec, warnings = ReportPresentationSpec.from_llm_output(payload)
    _assert(warnings == [], f"expected no warnings for a well-formed spec, got: {warnings}")
    _assert(spec.domain == ReportDomain.FINANCIAL, "domain should be FINANCIAL")
    _assert(spec.cover.treatment == TitleTreatment.DATA_DRIVEN, "cover treatment should round-trip")
    _assert(len(spec.sections) == 3, "expected 3 sections")
    _assert(spec.sections[0].section_type == SectionType.METRICS_DASHBOARD, "first section should be metrics dashboard")
    _assert(spec.sections[1].layout == LayoutVariant.TWO_COLUMN, "financials section should be two_column")
    _assert(spec.executive_summary.placement == ExecutiveSummaryPlacement.AFTER_COVER, "exec summary placement")
    _assert(spec.source_appendix.placement == SourcePlacement.END_OF_REPORT, "source placement")

    # Must validate strictly without raising.
    spec.validate_strict()

    # Round-trips to a plain, JSON-safe dict (structured data only).
    as_dict = spec.to_dict()
    _assert(isinstance(as_dict, dict), "to_dict should return a dict")
    _assert(as_dict["domain"] == "financial", "serialized domain should be the enum value")
    print("PASS: valid financial-style spec")


def test_valid_regulatory_spec():
    payload = {
        "domain": "regulatory",
        "cover": {"title": "Regulatory Compliance Report", "treatment": "classic"},
        "executive_summary": {"placement": "top_of_body", "body": "This report summarizes compliance posture."},
        "sections": [
            {
                "id": "compliance",
                "title": "Compliance Overview",
                "section_type": "compliance",
                "order": 0,
                "density": "dense",
                "blocks": [
                    {"kind": "prose", "heading": "Overview", "body": "All controls were assessed against Reg XYZ."},
                    {
                        "kind": "evidence",
                        "tone": "warning",
                        "heading": "Open Finding",
                        "body": "Control 4.2 requires remediation by Q4.",
                    },
                ],
            },
            {
                "id": "findings",
                "title": "Findings",
                "section_type": "findings",
                "order": 1,
                "blocks": [
                    {
                        "kind": "timeline",
                        "title": "Remediation Timeline",
                        "events": [
                            {"date_label": "2026-10-01", "title": "Remediation plan submitted"},
                            {"date_label": "2026-12-01", "title": "Follow-up audit"},
                        ],
                    }
                ],
            },
            {
                "id": "appendix",
                "title": "Appendix",
                "section_type": "appendix",
                "order": 2,
            },
        ],
        "source_appendix": {"placement": "appendix", "include_appendix": True},
    }

    spec, warnings = ReportPresentationSpec.from_llm_output(payload)
    _assert(warnings == [], f"expected no warnings for a well-formed spec, got: {warnings}")
    _assert(spec.domain == ReportDomain.REGULATORY, "domain should be REGULATORY")
    _assert(spec.source_appendix.include_appendix is True, "include_appendix should be True")
    _assert(spec.sections[0].density.value == "dense", "compliance section density should be dense")
    spec.validate_strict()
    print("PASS: valid regulatory-style spec")


def test_malformed_spec_falls_back_safely():
    payload = {
        "domain": "not-a-real-domain",          # invalid enum -> should default
        "cover": {"enabled": "yes-please", "treatment": 12345},  # invalid bool/enum
        "executive_summary": {"placement": "somewhere-invalid"},
        "sections": [
            "not-a-section-object",              # should be skipped
            {"title": "Untitled with weird order", "order": "not-a-number"},
            {
                "id": "dup", "title": "First", "section_type": "overview", "order": 1,
                "blocks": [
                    {"kind": "not-a-real-block-kind", "foo": "bar"},   # unknown kind -> skipped
                    {"kind": "prose", "body": "fine content"},
                    {"kind": "table", "rows": "not-a-list"},           # malformed rows -> []
                ],
            },
            {"id": "dup", "title": "Second (duplicate id)", "section_type": "overview", "order": 2},
        ],
        "source_appendix": {"placement": "nowhere"},
    }

    # Lenient path must NOT raise, and must collect warnings.
    spec, warnings = ReportPresentationSpec.from_llm_output(payload)
    _assert(len(warnings) > 0, "expected warnings for a malformed spec")
    _assert(spec.domain == ReportDomain.GENERIC, "invalid domain should fall back to GENERIC")
    _assert(spec.cover.enabled is True, "invalid bool should fall back to default True")
    _assert(spec.cover.treatment == TitleTreatment.CLASSIC, "invalid treatment should fall back to CLASSIC")
    _assert(
        spec.executive_summary.placement == ExecutiveSummaryPlacement.AFTER_COVER,
        "invalid placement should fall back to AFTER_COVER",
    )
    _assert(spec.source_appendix.placement == SourcePlacement.END_OF_REPORT, "invalid placement should fall back")

    # "not-a-section-object" skipped; 3 dict sections remain, one gets a deduped id.
    _assert(len(spec.sections) == 3, f"expected 3 valid sections, got {len(spec.sections)}")
    ids = [s.id for s in spec.sections]
    _assert(len(ids) == len(set(ids)), "section ids must be unique after dedup")

    section_with_blocks = next(s for s in spec.sections if s.title == "First")
    _assert(len(section_with_blocks.blocks) == 2, "unknown block kind should be dropped, valid ones kept")
    table_block = next(b for b in section_with_blocks.blocks if getattr(b, "kind", "") == "table")
    _assert(table_block.rows == [], "malformed rows should safely become an empty list")

    # This spec IS valid enough by lenient standards, so strict validation
    # should now pass (everything has been normalized to valid enum members).
    spec.validate_strict()
    print(f"PASS: malformed spec falls back safely ({len(warnings)} warnings recorded)")


def test_non_dict_root_and_hard_strict_failure():
    # Completely non-dict input must not raise -- it becomes a fully default spec.
    spec, warnings = ReportPresentationSpec.from_llm_output("this is not even a dict")
    _assert(any("expected an object" in w for w in warnings), "should warn about non-dict root")
    _assert(spec.domain == ReportDomain.GENERIC, "default domain")

    # An empty-sections spec is a case where lenient construction succeeds
    # (with a warning) but strict validation is expected to fail, because a
    # report needs at least one section to be renderable.
    _assert(spec.sections == [], "no sections expected")
    raised = False
    try:
        spec.validate_strict()
    except SchemaValidationError as e:
        raised = True
        _assert(any("sections" in issue for issue in e.issues), "strict validation should flag empty sections")
    _assert(raised, "validate_strict() must raise SchemaValidationError for an empty-sections spec")
    print("PASS: non-dict root falls back safely, and strict validation correctly rejects it")


def test_markup_injection_is_stripped_not_executed():
    payload = {
        "domain": "financial",
        "cover": {"title": "<script>alert(1)</script>Evil Title", "treatment": "classic"},
        "sections": [
            {
                "id": "s1",
                "title": "Section",
                "section_type": "overview",
                "order": 0,
                "blocks": [
                    {"kind": "prose", "heading": "H", "body": "<img src=x onerror=alert(1)>Hello <b>world</b>"},
                ],
            }
        ],
    }
    spec, warnings = ReportPresentationSpec.from_llm_output(payload)
    _assert(not contains_markup(spec.cover.title), "cover title must not contain markup after cleaning")
    _assert("script" not in spec.cover.title.lower() or "<" not in spec.cover.title, "script tag must be neutralized")
    body = spec.sections[0].blocks[0].body
    _assert(not contains_markup(body), "prose body must not contain markup after cleaning")
    _assert(any("markup detected" in w for w in warnings), "should warn about stripped markup")
    spec.validate_strict()  # cleaned spec must still pass strict validation
    print("PASS: markup injection attempts are stripped, never preserved as raw markup")


def test_default_spec_presets_are_valid_for_every_domain():
    for domain in ReportDomain:
        spec = default_spec_for_domain(domain)
        spec.validate_strict()
    print("PASS: default_spec_for_domain produces a strictly-valid spec for every ReportDomain")


def run_all():
    tests = [
        test_valid_financial_spec,
        test_valid_regulatory_spec,
        test_malformed_spec_falls_back_safely,
        test_non_dict_root_and_hard_strict_failure,
        test_markup_injection_is_stripped_not_executed,
        test_default_spec_presets_are_valid_for_every_domain,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failures.append((t.__name__, e))
            print(f"FAIL: {t.__name__}: {e}")

    print()
    if failures:
        print(f"{len(failures)} of {len(tests)} checks FAILED")
        sys.exit(1)
    else:
        print(f"All {len(tests)} checks PASSED")


if __name__ == "__main__":
    run_all()
