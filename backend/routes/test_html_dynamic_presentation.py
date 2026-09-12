"""Renderer smoke checks for structurally different presentation specs."""
from routes.html_report import build_html_report


def spec(domain, sections, cover="minimal", summary="none"):
    return {
        "domain": domain,
        "cover": {"enabled": True, "treatment": cover, "title": "Test Report", "subtitle": ""},
        "executive_summary": {"placement": summary, "heading": "Executive Summary", "key_metrics": []},
        "sections": sections,
        "source_appendix": {"placement": "end_of_report", "group_by_section": False, "include_appendix": False},
        "default_layout": "single_column", "default_density": "standard",
    }


def main():
    financial = spec("financial", [
        {"id":"f1","title":"Financials","section_type":"financials","layout":"two_column","density":"dense","emphasis":"high","order":0,"blocks":[{"kind":"metrics"},{"kind":"table"}]},
        {"id":"f2","title":"Valuation","section_type":"valuation","layout":"grid","density":"dense","emphasis":"normal","order":1,"blocks":[{"kind":"chart"}]},
    ], cover="data_driven", summary="after_cover")
    regulatory = spec("regulatory", [
        {"id":"r1","title":"Regulatory Timeline","section_type":"timeline","layout":"grid","density":"standard","emphasis":"high","order":0,"blocks":[{"kind":"timeline"}]},
        {"id":"r2","title":"Compliance","section_type":"compliance","layout":"sidebar_main","density":"standard","emphasis":"critical","order":1,"blocks":[{"kind":"bullets"},{"kind":"table"}]},
    ], cover="minimal", summary="sidebar")
    scientific = spec("scientific", [
        {"id":"s1","title":"Findings","section_type":"findings","layout":"single_column","density":"sparse","emphasis":"high","order":0,"blocks":[{"kind":"evidence"},{"kind":"prose"}]},
        {"id":"s2","title":"Research Questions","section_type":"recommendations","layout":"two_column","density":"standard","emphasis":"normal","order":1,"blocks":[{"kind":"bullets"}]},
    ], cover="classic", summary="end_summary")
    kwargs = dict(report="## One\nAlpha.\n\n| Metric | Value |\n|---|---|\n| Revenue | 100 |\n", title="Test Report", question="Q", summary="summary", key_stats=[{"label":"Revenue","value":"100"}], charts=[], images=[], sources=[])
    a=build_html_report(presentation=financial, **kwargs)
    b=build_html_report(presentation=regulatory, **kwargs)
    c=build_html_report(presentation=scientific, **kwargs)
    assert "gg-cover--data_driven" in a and "gg-section--two_column" in a
    assert "gg-cover--minimal" in b and "gg-section--sidebar_main" in b
    assert "gg-cover--classic" in c
    # Empty presentation plans must not produce heading-only sections.
    assert 'Research Questions' not in c
    # Markdown-table parsing is separately covered below; structured financial blocks
    # intentionally take precedence when the presentation requests metrics/table primitives.
    from routes.html_report import _markdown_to_html
    assert 'class="gg-table"' in _markdown_to_html("| Metric | Value |\n|---|---|\n| Revenue | 100 |", [], [])
    assert "<script" in a  # chart runtime remains in HTML; PDF strips it later
    print("PASS: HTML presentation smoke checks")

if __name__ == "__main__":
    main()
