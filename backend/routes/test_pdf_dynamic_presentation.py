"""Focused checks for the presentation-driven PDF renderer."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow execution as `python3 -m routes.test_pdf_dynamic_presentation` from backend/.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfReader

from routes.pdf import build_pdf
from utils.presentation_schema import default_spec_for_domain


def _sample_report() -> str:
    return """## Overview
A concise research overview.

## Findings
The evidence indicates a meaningful change in the observed outcome.

[CHART_1]

## Risks
Key uncertainties and limitations should be monitored."""


def _chart() -> list[dict]:
    return [{
        "type": "bar",
        "title": "Observed Trend",
        "series": [{
            "name": "Value",
            "data": [
                {"label": "2024", "value": 100},
                {"label": "2025", "value": 112},
            ],
        }],
    }]


def render_domain(domain: str) -> tuple[bytes, str]:
    spec = default_spec_for_domain(domain)
    pdf = build_pdf(
        _sample_report(), f"{domain.title()} Test Report", "Test topic", "Executive summary.",
        [{"label": "Value", "value": "112"}], _chart(),
        presentation=spec.to_dict(),
    )
    reader = PdfReader(__import__("io").BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return pdf, text


def main() -> None:
    financial_pdf, financial_text = render_domain("financial")
    regulatory_pdf, regulatory_text = render_domain("regulatory")
    scientific_pdf, scientific_text = render_domain("scientific")

    assert financial_pdf.startswith(b"%PDF-") and regulatory_pdf.startswith(b"%PDF-") and scientific_pdf.startswith(b"%PDF-")
    assert "Key Metrics" in financial_text
    assert "Compliance Overview" in regulatory_text
    assert "Methodology" in scientific_text
    assert "Observed Trend" in financial_text

    # The presentation presets produce different structural text/orderings,
    # proving that the PDF is consuming the presentation spec rather than a
    # single hard-coded section sequence.
    assert financial_text != regulatory_text != scientific_text

    # Legacy payloads without a presentation still render.
    legacy_pdf, legacy_text = build_pdf(
        "## Legacy Section\nLegacy content.", "Legacy Report", "Legacy", "", [], [], presentation=None
    ), ""
    legacy_text = "\n".join(page.extract_text() or "" for page in PdfReader(__import__("io").BytesIO(legacy_pdf)).pages)
    assert legacy_pdf.startswith(b"%PDF-") and "Legacy Section" in legacy_text

    # Malformed presentation must safely fall back instead of raising.
    malformed_pdf = build_pdf(
        "## Legacy Section\nLegacy content.", "Malformed Presentation", "Legacy", "", [], [],
        presentation={"sections": [{"layout": "not-a-real-layout"}]},
    )
    assert malformed_pdf.startswith(b"%PDF-")

    print("PDF dynamic presentation checks: PASS")


if __name__ == "__main__":
    main()
