from __future__ import annotations

from app.ingest.markdown import chunk_hash, chunk_section, extract, has_body_text


def test_markdown_sections_and_chunk_locators_round_trip_original() -> None:
    original = "# Paper α\r\n\r\n## Abstract\r\nShort summary.\r\n\r\n## Methods\r\n" + "word " * 300 + "\r\n## References\r\n[1] Source.\r\n"
    parsed = extract(original)
    assert parsed.coverage == "complete"
    assert parsed.candidates[0].value == "Paper α"
    assert [section.kind for section in parsed.sections] == ["body", "abstract", "body", "references"]
    assert parsed.sections[-1].heading == "References"
    chunks = [span for section in parsed.sections for span in chunk_section(original, section, 120)]
    assert len(chunks) > 10
    for section in parsed.sections:
        assert original[section.start_offset:section.end_offset] == section.text
    for span in chunks:
        assert original[span.start_offset:span.end_offset] == span.text
        assert original.splitlines()[span.start_line - 1]
        assert len(chunk_hash(span.text)) == 64
        assert len(span.text) <= 120


def test_markdown_without_heading_is_body() -> None:
    original = "A paragraph.\n\nAnother."
    result = extract(original)
    assert len(result.sections) == 1
    assert result.sections[0].kind == "body"
    assert result.candidates == []


def test_heading_inside_code_fence_is_body_text() -> None:
    original = "# Paper\n\n```md\n## References\n[1] fake\n```\n\n## Methods\nActual body.\n"
    result = extract(original)
    assert [section.kind for section in result.sections] == ["body", "body"]
    assert result.sections[0].text.startswith("# Paper")


def test_abstract_and_references_alone_do_not_count_as_body() -> None:
    assert not has_body_text(extract("# Paper\n## Abstract\nSummary.\n## References\n[1] Citation.\n"))
    assert has_body_text(extract("# Paper\n\nA result.\n## References\n[1] Citation.\n"))


def test_bibliography_variants_and_uncertain_boundary() -> None:
    for heading in ("References and Notes", "References & Notes", "Notes and References",
                    "References Cited", "Literature Cited", "2. References and Notes"):
        result = extract(f"## Methods\nSynthetic finding.\n## {heading}\n[1] Source.\n### Further reading\n[2] Source.\n")
        assert [section.kind for section in result.sections] == ["body", "references", "references"]
        assert result.coverage == "complete"
    uncertain = extract("## Methods\nFinding.\n## References to Earlier Work\n[1] Source.\n")
    assert uncertain.coverage == "none"
    assert "bibliography_boundary_uncertain" in uncertain.warnings
    assert uncertain.sections[-1].kind == "references"
