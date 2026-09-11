from routes.pdf import _strip_non_printing_runtime, _pdf_with_weasyprint


def main():
    html = '''<!doctype html><html><head><style>body{font-family:sans-serif}</style></head>
    <body><h1>Offline PDF Test</h1>
    <img src="https://example.com/never-fetch-this.png" />
    <a href="https://example.com/source">source</a>
    <p>Network resources must never be dereferenced by WeasyPrint.</p></body></html>'''
    compiled = _strip_non_printing_runtime(html, {})
    assert 'never-fetch-this.png' not in compiled
    assert 'https://example.com/source' in compiled
    pdf = _pdf_with_weasyprint(compiled, timeout_s=30)
    assert pdf.startswith(b'%PDF-')
    print('PASS: PDF offline-resource guard blocks remote fetches and preserves source links')


if __name__ == '__main__':
    main()
