"""Schema, checksum and gold-span checks for the committed evaluation set."""
from __future__ import annotations

from pathlib import Path

from app.config import IngestPolicy, PdfBodyThreshold
from app.evaluation.locator import LocatorError, Source, annotate, check_non_evidence, check_span, load_source
from app.evaluation.schema import Manifest, QuestionSet, check_manifest, check_questions, load


def load_sources(manifest: Manifest, root: Path, ingest: IngestPolicy | None = None,
                 pdf_body: PdfBodyThreshold | None = None) -> dict[str, Source]:
    """Extracted text keyed by item; items that share a paper share one extraction."""
    by_paper = {paper.id: load_source(paper, root, ingest, pdf_body) for paper in manifest.papers}
    return {item.key: by_paper[item.paper] for item in manifest.items if item.paper is not None}


def annotate_questions(manifest: Manifest, qset: QuestionSet, sources: dict[str, Source]) -> QuestionSet:
    """Fill offsets, lines and parser version from each quote's single occurrence."""
    questions = []
    for question in qset.questions:
        sides = [side.model_copy(update={"spans": [annotate(span, sources[span.item]) for span in side.spans]})
                 for side in question.evidence]
        questions.append(question.model_copy(update={"evidence": sides}))
    return qset.model_copy(update={"questions": questions})


def check_locators(qset: QuestionSet, sources: dict[str, Source]) -> list[str]:
    errors = []
    for question in qset.questions:
        for side in question.evidence:
            for span in side.spans:
                if span.item not in sources:
                    continue  # reported by check_questions
                errors += [f"{question.id}: {e}" for e in check_span(span, sources[span.item])]
        for entry in question.non_evidence:
            if entry.item in sources:
                errors += [f"{question.id}: {e}" for e in check_non_evidence(entry, sources[entry.item])]
    return errors


def validate(root: Path, ingest: IngestPolicy | None = None,
             pdf_body: PdfBodyThreshold | None = None) -> list[str]:
    manifest, qset = load(root)
    errors = check_manifest(manifest, root)
    errors += check_questions(manifest, qset)
    if errors:
        return errors  # locator checks need intact files and references
    try:
        sources = load_sources(manifest, root, ingest, pdf_body)
    except LocatorError as exc:
        return [str(exc)]
    return check_locators(qset, sources)
