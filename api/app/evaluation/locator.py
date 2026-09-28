"""Gold spans checked against the text the production pipeline really extracts.

Markdown offsets are code points in the UTF-8 decoded original and lines start
at 1, as in ``app.ingest.markdown``. PDF offsets are code points in the page
text returned by ``app.ingest.pdf`` (``page_text_codepoint``); they are not PDF
byte offsets or pixel boxes.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader

from app.config import IngestPolicy, PdfBodyThreshold
from app.evaluation.schema import NonEvidence, Paper, Span
from app.ingest import markdown, pdf

FULL_SPAN = 1.0
DIAGNOSTIC_OVERLAP = 0.5  # labelled diagnostic only, never a headline metric


class LocatorError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    paper: Paper
    parser: str
    text: str = ""
    pages: dict[int, str] = field(default_factory=dict)
    # (page or 0, start, end, kind) for every extracted section/segment.
    regions: tuple[tuple[int, int, int, str], ...] = ()
    deficient_pages: frozenset[int] = frozenset()
    failed_pages: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk, reduced to what locator matching needs."""
    item: str
    locator_kind: str
    page: int | None
    start_offset: int
    end_offset: int


def load_source(paper: Paper, root: Path, ingest: IngestPolicy | None = None,
                pdf_body: PdfBodyThreshold | None = None) -> Source:
    raw = (root / "corpus" / paper.file).read_bytes()
    if paper.format == "markdown":
        original = raw.decode("utf-8")
        regions = tuple((0, s.start_offset, s.end_offset, s.kind) for s in markdown.extract(original).sections)
        return Source(paper, markdown.PIPELINE_VERSION, text=original, regions=regions)
    # The parser only reads these two policies; run it in its usual subprocess.
    settings = SimpleNamespace(ingest=ingest or IngestPolicy(), pdf_body=pdf_body or PdfBodyThreshold())
    pages = pdf.parse(raw, len(PdfReader(io.BytesIO(raw)).pages), settings)
    result = pdf.classify(pages, settings)
    return Source(paper, pdf.PIPELINE_VERSION, pages={p.number: p.text for p in pages},
                  regions=tuple((s.page, s.start, s.end, s.kind) for s in result.segments),
                  deficient_pages=frozenset(result.deficient_pages),
                  failed_pages=frozenset(result.failed_pages))


def _text(source: Source, page: int | None) -> str:
    if source.paper.format == "markdown":
        return source.text
    if page not in source.pages:
        raise LocatorError(f"{source.paper.id}: page {page} does not exist")
    return source.pages[page]


def locate(source: Source, quote: str, page: int | None = None) -> tuple[int, int]:
    """Half-open offsets of the single occurrence of ``quote``."""
    text = _text(source, page)
    start = text.find(quote)
    if start < 0:
        raise LocatorError(f"{source.paper.id}: quote not found: {quote!r}")
    if text.find(quote, start + 1) >= 0:
        raise LocatorError(f"{source.paper.id}: quote is ambiguous: {quote!r}")
    return start, start + len(quote)


def region_kind(source: Source, page: int | None, start: int, end: int) -> str | None:
    """Kind of the extracted region that fully contains ``[start, end)``."""
    number = page or 0
    for region_page, a, b, kind in source.regions:
        if region_page == number and a <= start and end <= b:
            return kind
    return None


def annotate(span: Span, source: Source) -> Span:
    start, end = locate(source, span.quote, span.page)
    values = {"start_offset": start, "end_offset": end, "parser": source.parser}
    if span.locator_kind == "markdown":
        starts = markdown.line_starts(source.text)
        values |= {"start_line": markdown.line_at(starts, start), "end_line": markdown.line_at(starts, end - 1)}
    return span.model_copy(update=values)


def check_span(span: Span, source: Source) -> list[str]:
    """A gold evidence span must match the extraction exactly and lie in body/abstract text."""
    name = f"{span.item} {span.quote[:40]!r}"
    if span.start_offset is None or span.end_offset is None:
        return [f"{name}: missing offsets"]
    if span.parser != source.parser:
        return [f"{name}: labelled with parser {span.parser}, extraction now uses {source.parser}"]
    try:
        text = _text(source, span.page)
    except LocatorError as exc:
        return [str(exc)]
    errors = []
    if text[span.start_offset:span.end_offset] != span.quote:
        errors.append(f"{name}: offsets do not select the quote in the extracted text")
    if span.locator_kind == "markdown":
        starts = markdown.line_starts(source.text)
        if (span.start_line, span.end_line) != (markdown.line_at(starts, span.start_offset),
                                                 markdown.line_at(starts, span.end_offset - 1)):
            errors.append(f"{name}: line numbers do not match offsets")
    kind = region_kind(source, span.page, span.start_offset, span.end_offset)
    if kind not in ("body", "abstract"):
        errors.append(f"{name}: evidence lies in a {kind or 'non-indexed'} region, not body/abstract")
    return errors


def check_non_evidence(entry: NonEvidence, source: Source) -> list[str]:
    name = f"{entry.item} {entry.kind}"
    if entry.kind == "not_extracted_region":
        if entry.page is None or entry.page not in (source.deficient_pages | source.failed_pages):
            return [f"{name}: page {entry.page} is not a deficient or unextractable body page"]
        return []
    if entry.quote is None:
        return [] if entry.kind in ("metadata_only", "forbidden_item") else [f"{name}: quote is required"]
    try:
        start, end = locate(source, entry.quote, entry.page)
    except LocatorError as exc:
        return [str(exc)]
    kind = region_kind(source, entry.page, start, end)
    if entry.kind == "bibliography_entry" and kind != "references":
        return [f"{name}: quote is not inside a references section"]
    if entry.kind != "bibliography_entry" and kind == "references":
        return [f"{name}: quote is inside a references section; label it bibliography_entry"]
    return []


def valid_locator(start: int | None, end: int | None) -> bool:
    return isinstance(start, int) and isinstance(end, int) and 0 <= start < end


def covers(span: Span, hit: Hit, minimum: float = FULL_SPAN) -> bool:
    """Whether a chunk holds the gold span, on the same item/kind (and page for PDF).

    The default and headline rule is the whole span inside the chunk's
    half-open range. A smaller ``minimum`` (the share of span characters that
    must lie inside the chunk) exists only for the labelled diagnostic.
    """
    if hit.item != span.item or hit.locator_kind != span.locator_kind:
        return False
    if span.locator_kind == "pdf" and (span.page is None or hit.page != span.page):
        return False
    if not (valid_locator(span.start_offset, span.end_offset) and valid_locator(hit.start_offset, hit.end_offset)):
        return False
    if minimum >= FULL_SPAN:
        return hit.start_offset <= span.start_offset and span.end_offset <= hit.end_offset
    length = span.end_offset - span.start_offset
    shared = min(span.end_offset, hit.end_offset) - max(span.start_offset, hit.start_offset)
    return shared > 0 and shared >= minimum * length
