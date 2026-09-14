from routes.source_manifest import normalise_source_manifest

def test_source_manifest_assigns_stable_ids_and_dedupes_urls():
    sources = [
        {"title": "A", "url": "https://example.com/a", "publisher": "Example"},
        {"title": "A duplicate", "url": "https://example.com/a#fragment", "publisher": "Example"},
        {"title": "B", "url": "https://example.com/b", "publisher": "Example"},
    ]
    out = normalise_source_manifest(sources)
    assert [x["id"] for x in out] == ["S1", "S2"]
    assert out[0]["url"] == "https://example.com/a"

def test_uploaded_documents_get_ids_after_web_sources():
    out = normalise_source_manifest(
        [{"title": "Web", "url": "https://example.com"}],
        [{"name": "client-notes.pdf"}],
    )
    assert [x["id"] for x in out] == ["S1", "S2"]
    assert out[1]["kind"] == "Uploaded document"
