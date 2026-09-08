"""Focused regression checks for report latency/relevance fixes."""
from routes.report import (
    _extract_company_candidates,
    _filter_image_candidates,
    _image_prompt_is_specific,
    _force_fallback_images,
    _top_up_images,
)
from utils.presentation_schema import ReportPresentationSpec


def main():
    q = "Create a detailed comparison report of Tata Motors vs Mahindra & Mahindra covering business mix, financial performance"
    companies = _extract_company_candidates(q)
    assert companies[:2] == ["Tata Motors", "Mahindra & Mahindra"], companies
    assert all(c.lower() not in {"create", "use", "suv", "financial"} for c in companies)

    imgs = [
        {"url": "https://example.com/tata-suv.jpg", "description": "Tata Motors electric SUV"},
        {"url": "https://example.com/random-office.jpg", "description": "Generic modern office"},
        {"url": "https://example.com/tcs-logo.svg", "description": "TCS logo"},
    ]
    filtered = _filter_image_candidates(imgs, subject=q)
    assert filtered and "tata" in filtered[0]["description"].lower(), filtered
    assert len(filtered) == 1, filtered

    assert _image_prompt_is_specific(
        "Documentary photograph of an H5N1 virology laboratory with researchers examining sample tubes",
        "H5N1 avian influenza human transmission",
    )
    assert not _image_prompt_is_specific("A modern office with a laptop and coffee cup", "Tata Motors")

    assert _force_fallback_images([], filtered)[0] == []
    assert _top_up_images([], filtered)[0] == []

    spec, warnings = ReportPresentationSpec.from_llm_output({
        "domain": "regulatory",
        "sections": [{
            "id": "rules", "title": "Rules", "section_type": "compliance",
            "blocks": [{"kind": "bullets", "items": ["Rule A", "Rule B"]}],
        }],
    })
    assert spec.to_dict()["sections"][0]["blocks"][0]["kind"] == "bullets"
    assert not warnings
    print("PASS: report quality regression checks")


if __name__ == "__main__":
    main()
