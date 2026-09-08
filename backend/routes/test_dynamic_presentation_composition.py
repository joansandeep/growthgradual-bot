"""Regression checks for planner-to-renderer presentation preservation."""
from routes.report import _attach_presentation_to_plan


class Intent:
    def __init__(self, label, evidence):
        self.intent_label = label
        self.evidence_needed = evidence


def _plan(*headings):
    return {
        "depth": "detailed",
        "sections": [
            {"heading": h, "purpose": "test", "format": "mixed", "format_reason": "test"}
            for h in headings
        ],
    }


def main():
    financial = _attach_presentation_to_plan(
        _plan("Financial Performance", "Valuation", "Risk Factors"),
        question="Tata Motors investment analysis",
        intent=Intent("financial analysis", ["financials"]),
    )["presentation"]
    regulatory = _attach_presentation_to_plan(
        _plan("Regulatory Timeline", "Compliance Requirements", "Enforcement Risks"),
        question="SEBI margin trading regulations",
        intent=Intent("regulatory analysis", ["regulatory"]),
    )["presentation"]
    scientific = _attach_presentation_to_plan(
        _plan("Virology and Molecular Findings", "Evidence and Uncertainty", "Research Gaps"),
        question="H5N1 human transmission risk scientific review",
        intent=Intent("scientific review", ["scientific", "expert_opinion"]),
    )["presentation"]

    assert financial["domain"] == "financial"
    assert regulatory["domain"] == "regulatory"
    assert scientific["domain"] == "scientific"
    assert financial["sections"] and regulatory["sections"] and scientific["sections"]
    assert [s["title"] for s in financial["sections"]] == ["Financial Performance", "Valuation", "Risk Factors"]
    assert [s["title"] for s in regulatory["sections"]] == ["Regulatory Timeline", "Compliance Requirements", "Enforcement Risks"]
    assert [s["title"] for s in scientific["sections"]] == ["Virology and Molecular Findings", "Evidence and Uncertainty", "Research Gaps"]
    assert financial["cover"]["treatment"] != scientific["cover"]["treatment"]
    assert financial["executive_summary"]["placement"] != regulatory["executive_summary"]["placement"] or financial["source_appendix"]["placement"] != regulatory["source_appendix"]["placement"]

    # A partial LLM presentation must be supplemented from the planner rather
    # than collapsing to a legacy/default section list.
    partial_plan = _plan("Business Mix", "Financial Performance", "Risks")
    partial_plan["presentation"] = {
        "domain": "comparison",
        "cover": {"enabled": True, "treatment": "classic"},
        "sections": [{"id": "business", "title": "Business Mix", "section_type": "comparison", "layout": "two_column", "order": 0}],
    }
    out = _attach_presentation_to_plan(partial_plan, question="Company A vs Company B", intent=Intent("comparison", ["comparison"]))["presentation"]
    assert [s["title"] for s in out["sections"]] == ["Business Mix", "Financial Performance", "Risks"]

    print("dynamic presentation composition checks passed")


if __name__ == "__main__":
    main()
