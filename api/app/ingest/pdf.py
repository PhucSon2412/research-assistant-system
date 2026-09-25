"""Bounded pypdf extraction and conservative, inspectable page classification.

Offsets refer to Python code points in the exact page text returned by pypdf's
layout extractor. They are not PDF byte offsets or geometric coordinates.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from app.config import Settings

PIPELINE_VERSION = f"pypdf-{version('pypdf')}-layout-v1"
BODY = re.compile(r"^\s*(?:\d+(?:\.\d+)*[. ]+)?(?:introduction|background|methods?|materials(?: and methods)?|results?|discussion|conclusion|related work)\s*$", re.I)
ABSTRACT = re.compile(r"^\s*abstract\s*$", re.I)
REFERENCES = re.compile(r"^\s*(?:references|bibliography|works cited|tài liệu tham khảo)\s*$", re.I)
ENTRY = re.compile(r"^\s*(?:\[\d+\]|\d+[.)])\s+")
DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


class PdfError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


@dataclass(frozen=True)
class Page:
    number: int
    text: str
    width: float
    height: float
    error: str | None = None
    layout_mode: str = "layout"


@dataclass(frozen=True)
class Segment:
    page: int
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Reference:
    page: int
    raw: str
    start: int
    end: int
    fields: dict
    confidence: str


@dataclass(frozen=True)
class Result:
    pages: list[Page]
    segments: list[Segment]
    references: list[Reference]
    coverage: str
    disposition: str
    warnings: list[str]
    body_pages: list[int]
    qualifying_pages: list[int]
    deficient_pages: list[int]
    failed_pages: list[int]
    total_body_chars: int
    uncertain: bool


def _lines(text: str):
    offset = 0
    for line in text.splitlines(keepends=True):
        yield offset, offset + len(line), line
        offset += len(line)


def _order_columns(text: str) -> tuple[str, str]:
    """Reorder a clear two-column pypdf layout; ambiguous layouts stay intact.

    pypdf layout joins rows horizontally. A stable inter-column whitespace
    gutter across many rows lets us recover left-then-right reading order.
    This is heuristic and never supplies geometric bounding boxes.
    """
    lines = text.splitlines()
    gaps = []
    for line in lines:
        candidates = [match for match in re.finditer(r" {8,}", line)
                      if 20 <= match.start() <= 85 and line[match.end():].strip()]
        if candidates:
            gaps.append(max(candidates, key=lambda match: len(match.group())).start())
    if len(gaps) < 8 or len(gaps) < len(lines) * .2:
        return text, "layout"
    pivot = int(statistics.median(gaps))
    if sum(abs(value - pivot) <= 10 for value in gaps) < len(gaps) * .75:
        return text, "layout_ambiguous"
    left, right = [], []
    for line in lines:
        match = next((m for m in re.finditer(r" {8,}", line)
                      if m.start() <= pivot + 10 and m.end() >= pivot + 8
                      and line[m.end():].strip()), None)
        if match:
            left.append(line[:match.start()].rstrip())
            right.append(line[match.end():].strip())
        elif len(line) - len(line.lstrip()) > pivot + 5:
            right.append(line.strip())
        else:
            left.append(line.rstrip())
    return "\n".join(left).strip() + "\n\n" + "\n".join(right).strip() + "\n", "columns_reordered"


def _references(page: int, text: str, start: int, end: int) -> list[Reference]:
    entries: list[Reference] = []
    current: list[tuple[int, int, str]] = []
    def flush() -> None:
        if current:
            first, last = current[0][0], current[-1][1]
            raw = text[first:last]
            if raw.strip():
                doi = DOI.search(raw)
                year = YEAR.search(raw)
                fields = {}
                if doi:
                    fields["doi"] = doi.group().rstrip(".,;)")
                if year:
                    fields["year"] = int(year.group())
                entries.append(Reference(page, raw, first, last, fields,
                                         "partial_fields" if fields else "raw_only"))

    for a, b, line in _lines(text[start:end]):
        a, b = a + start, b + start
        if a == start and REFERENCES.fullmatch(line.strip().lstrip("#").strip()):
            continue
        if (ENTRY.match(line) or not line.strip()) and current:
            flush()
            current = []
        if line.strip():
            current.append((a, b, line))
    flush()
    return entries


def classify(pages: list[Page], settings: Settings) -> Result:
    segments: list[Segment] = []
    references: list[Reference] = []
    warnings: list[str] = []
    failed = [p.number for p in pages if p.error]
    if failed:
        warnings.append("pages_unextractable")
    if any(p.layout_mode == "columns_reordered" for p in pages):
        warnings.append("two_column_order_heuristic")
    if any(p.layout_mode == "layout_ambiguous" for p in pages):
        warnings.append("layout_order_uncertain")
    # Only identical first/last extracted lines across pages are treated as
    # margin furniture. Their exclusion is recorded; other layout artifacts
    # remain uncertain and visible in the page text.
    edges: dict[str, set[int]] = {}
    edge_spans: dict[int, list[tuple[int, int, str]]] = {}
    for page in pages:
        lines = [(a, b, line.strip()) for a, b, line in _lines(page.text) if line.strip()]
        if not lines:
            continue
        chosen = [lines[0], lines[-1]] if len(lines) > 1 else [lines[0]]
        edge_spans[page.number] = chosen
        for _a, _b, value in chosen:
            if len(value) < 120 and not (BODY.fullmatch(value) or ABSTRACT.fullmatch(value) or REFERENCES.fullmatch(value)):
                edges.setdefault(value, set()).add(page.number)
    removable = {value for value, numbers in edges.items() if len(numbers) >= 2}
    excluded = {number: [(a, b) for a, b, value in spans
                         if value in removable or re.fullmatch(r"\d{1,4}", value)]
                for number, spans in edge_spans.items()}
    if any(excluded.values()):
        warnings.append("margin_lines_excluded")
    # State crosses page boundaries. Until an explicit body heading, front
    # matter is excluded; uncertain papers go to review rather than indexing.
    state = "cover"
    seen_body = False
    seen_references = False
    body_page_candidates: set[int] = set()
    for page in pages:
        if page.error:
            if state == "body":
                # A failed page between body headings is still in the body
                # denominator. We cannot infer its text or pretend it passed.
                segments.append(Segment(page.number, "body", "", 0, 0))
                body_page_candidates.add(page.number)
            continue
        boundaries = [(0, state)]
        for a, _b, line in _lines(page.text):
            stripped = line.strip()
            if REFERENCES.fullmatch(stripped):
                state, seen_references = "references", True
                boundaries.append((a, state))
            elif ABSTRACT.fullmatch(stripped) and not seen_body and not seen_references:
                state = "abstract"
                boundaries.append((a, state))
            elif BODY.fullmatch(stripped) and not seen_references:
                state, seen_body = "body", True
                boundaries.append((a, state))
        for i, (start, kind) in enumerate(boundaries):
            end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(page.text)
            if start >= end or not page.text[start:end].strip():
                continue
            if kind == "body":
                body_page_candidates.add(page.number)
            if kind in ("body", "abstract", "references"):
                intervals = [(start, end)]
                if kind == "body":
                    for cut_start, cut_end in excluded.get(page.number, []):
                        intervals = ([(a, min(b, cut_start)) for a, b in intervals if a < cut_start]
                                     + [(max(a, cut_end), b) for a, b in intervals if b > cut_end])
                for a, b in intervals:
                    if a < b and page.text[a:b].strip():
                        segments.append(Segment(page.number, kind, page.text[a:b], a, b))
            if kind == "references":
                references.extend(_references(page.number, page.text, start, end))
    counts: dict[int, int] = {number: 0 for number in body_page_candidates}
    for segment in segments:
        if segment.kind == "body":
            counts[segment.page] = counts.get(segment.page, 0) + sum(not c.isspace() for c in segment.text)
    body_pages = sorted(counts)
    good = [n for n in body_pages if counts[n] >= settings.pdf_body.min_chars_per_body_page]
    bad = [n for n in body_pages if n not in good]
    total = sum(counts.values())
    uncertain = not seen_body
    if uncertain:
        warnings.append("body_boundary_uncertain")
    if bad:
        warnings.append("body_pages_below_min_chars")
    if not seen_references:
        warnings.append("references_boundary_unknown")
    if any(not p.text.strip() for p in pages if not p.error):
        warnings.append("pages_without_text")
    ratio_ok = bool(body_pages) and len(good) / len(body_pages) >= settings.pdf_body.min_body_page_ratio
    enough = ratio_ok and total >= settings.pdf_body.min_total_body_chars
    if uncertain:
        disposition = "needs_review"
    elif not enough:
        disposition = "fulltext_rejected"
    else:
        disposition = "awaiting_index"
    coverage = "none" if not good else "partial" if (failed or bad or not enough) else "complete"
    if failed and disposition == "awaiting_index":
        coverage = "partial"
    return Result(pages, segments, references, coverage, disposition, warnings,
                  body_pages, good, bad, failed, total, uncertain)


def parse(raw: bytes, expected_pages: int, settings: Settings) -> list[Page]:
    """The worker never imports/executes the PDF parser in its own process."""
    with tempfile.TemporaryDirectory(prefix="ras-pdf-") as directory:
        source, output = Path(directory) / "original.pdf", Path(directory) / "pages.json"
        source.write_bytes(raw)
        try:
            done = subprocess.run(
                [sys.executable, "-m", "app.ingest.pdf", "--child", str(source), str(output),
                 str(expected_pages), str(settings.ingest.pdf_parser_memory_bytes),
                 str(settings.ingest.pdf_parser_timeout_seconds), str(settings.ingest.pdf_parser_output_bytes)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=settings.ingest.pdf_parser_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise PdfError("pdf_parser_timeout", retryable=True) from None
        except OSError:
            raise PdfError("pdf_parser_unavailable", retryable=True) from None
        if done.returncode:
            code = {2: "pdf_invalid", 3: "pdf_page_mismatch", 4: "pdf_text_invalid",
                    5: "pdf_parser_limit", 6: "pdf_parser_crash"}.get(done.returncode, "pdf_parser_crash")
            raise PdfError(code, retryable=code == "pdf_parser_crash")
        if not output.exists() or output.stat().st_size > settings.ingest.pdf_parser_output_bytes:
            raise PdfError("pdf_parser_limit")
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
            pages = [Page(**entry) for entry in data]
            if len(pages) != expected_pages or [p.number for p in pages] != list(range(1, expected_pages + 1)):
                raise ValueError("page mismatch")
            return pages
        except (ValueError, TypeError, KeyError):
            raise PdfError("pdf_text_invalid") from None


def _child() -> int:
    try:
        if os.name != "nt":
            import resource
            memory, seconds = int(sys.argv[5]), int(sys.argv[6])
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        from pypdf import PdfReader
    except Exception:
        return 6
    try:
        reader = PdfReader(sys.argv[2], strict=True)
        if reader.is_encrypted:
            return 2
        if len(reader.pages) != int(sys.argv[4]):
            return 3
    except Exception:
        return 2
    try:
        pages = []
        for number, page in enumerate(reader.pages, 1):
            try:
                value = page.extract_text(extraction_mode="layout") or ""
                if "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                    return 4
                value, layout_mode = _order_columns(value)
                pages.append(dict(number=number, text=value, width=float(page.mediabox.width),
                                  height=float(page.mediabox.height), error=None, layout_mode=layout_mode))
            except Exception:
                pages.append(dict(number=number, text="", width=float(page.mediabox.width),
                                  height=float(page.mediabox.height), error="page_extract_failed", layout_mode="layout"))
        payload = json.dumps(pages, ensure_ascii=False)
        if len(payload.encode("utf-8")) > int(sys.argv[7]):
            return 5
        Path(sys.argv[3]).write_text(payload, encoding="utf-8")
        return 0
    except MemoryError:
        return 5
    except Exception:
        return 6


if __name__ == "__main__":
    raise SystemExit(_child())
