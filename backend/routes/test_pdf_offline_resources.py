"""Regression tests for the single-pass, fully offline PDF renderer."""
from __future__ import annotations

import io
import time

from pypdf import PdfReader

from routes.pdf import _finalize_strict_offline_pdf_html, _pdf_with_weasyprint


def main() -> None:
    html = """<html><head>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Inter">
    <style>
      @import url(https://example.com/fonts.css);
      .hero { background-image: url('https://example.com/hero.png'); }
    </style>
    </head><body>
      <img src="https://example.com/remote.png">
      <h1>Offline PDF</h1><p>Remote assets must never block rendering.</p>
    </body></html>"""

    strict = _finalize_strict_offline_pdf_html(html)
    assert "https://fonts.googleapis.com" not in strict
    assert "https://example.com" not in strict

    start = time.perf_counter()
    pdf = _pdf_with_weasyprint(strict, timeout_s=10)
    elapsed = time.perf_counter() - start
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert pdf.startswith(b"%PDF-")
    assert "Offline PDF" in text
    # This is a guard against regression to the old network-fetch path.
    assert elapsed < 10, f"offline PDF render unexpectedly slow: {elapsed:.2f}s"
    print(f"PDF offline resource regression: PASS ({elapsed:.2f}s)")


if __name__ == "__main__":
    main()
