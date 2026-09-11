"""Heavy-document regression test for the dynamic offline PDF path."""
from __future__ import annotations

import io
import time

from pypdf import PdfReader

from routes.pdf import _strip_non_printing_runtime, build_pdf
from utils.presentation_schema import default_spec_for_domain


def main() -> None:
    browser_html = '''<!doctype html><html><head>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700">
    <style>@import url('https://example.com/remote.css'); .x{color:red}</style>
    </head><body><img src="https://example.com/image.jpg"><div class="gg-report-sections">Hello</div></body></html>'''
    compiled = _strip_non_printing_runtime(browser_html, {}, default_spec_for_domain("financial").to_dict(), fast_mode=True)
    low = compiled.lower()
    assert "fonts.googleapis.com" not in low
    assert "@import" not in low
    assert '<link' not in low
    assert 'src="https://example.com' not in low
    assert 'https://example.com/image.jpg' not in low
    assert '<style id="gg-pdf-compiled">' in low

    charts = []
    for i in range(9):
        ctype = "bar" if i % 3 == 0 else ("line" if i % 3 == 1 else "doughnut")
        charts.append({
            "type": ctype,
            "title": f"Chart {i + 1}",
            "series": [{
                "name": "Value",
                "data": [
                    {"label": "Q1", "value": 100 + i},
                    {"label": "Q2", "value": 112 + i},
                    {"label": "Q3", "value": 108 + i},
                    {"label": "Q4", "value": 121 + i},
                ],
            }],
        })

    report = "\n\n".join(
        f"## Section {i + 1}\nA research section with context, findings, implications, and risks.\n[CHART_{i + 1}]"
        for i in range(9)
    )
    sources = [
        {"title": f"Source {i + 1}", "publisher": "Example", "kind": "Web source", "url": f"https://example.com/{i + 1}"}
        for i in range(31)
    ]

    t0 = time.perf_counter()
    pdf = build_pdf(
        report,
        "Heavy Dynamic PDF Smoke Test",
        "HDFC Bank Q1 FY27",
        "Executive summary.",
        [{"label": "Net Profit", "value": "20,383"}],
        charts,
        presentation=default_spec_for_domain("financial").to_dict(),
        sources=sources,
    )
    elapsed = time.perf_counter() - t0
    assert pdf.startswith(b"%PDF-")
    pages = len(PdfReader(io.BytesIO(pdf)).pages)
    assert pages >= 5
    assert elapsed < 20, f"heavy dynamic PDF took {elapsed:.2f}s"
    print(f"PASS: heavy dynamic offline PDF rendered in {elapsed:.2f}s ({pages} pages)")


if __name__ == "__main__":
    main()
