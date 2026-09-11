"""Focused regression check for browser-only CSS leaking into PDF input."""
from routes.pdf import _strip_non_printing_runtime


def main() -> None:
    html = """<html><head><style>
    @media screen and (max-width: 760px) { .x { color:red; } }
    @media print { .y { print-color-adjust: exact; } }
    .x { width: 100vw; position: sticky; overflow-x: auto; place-items: center; }
    </style></head><body><p>hello</p></body></html>"""
    out = _strip_non_printing_runtime(html)
    low = out.lower()
    assert "@media screen" not in low
    assert "@media print" not in low
    assert "print-color-adjust" not in low
    assert "100vw" not in low
    assert "position: sticky" not in low
    assert "overflow-x: auto" not in low
    assert "place-items: center" not in low
    assert "auto-fit" not in low
    assert "auto-fill" not in low
    assert "color-mix(" not in low
    assert "box-shadow:" not in low
    assert "@font-face" not in low
    assert "<link" not in low
    print("PASS: print CSS sanitizer removes browser-only constructs")


if __name__ == "__main__":
    main()
