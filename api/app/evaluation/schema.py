"""Evaluation manifest and question-set schema with cross-file checks.

Labels in the committed set are written by a coding agent. The schema only
accepts ``label_status="pending_human_review"`` so no file can claim a human
or expert review that has not happened.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.corpus import sha256_bytes

GROUP_MINIMUMS = {
    "single_paper": 20,
    "comparison": 15,
    "no_answer": 15,
    "partial_pdf": 10,
    "permission": 10,
    "references_conflict_injection": 10,
}
GROUP_PREFIX = {"single_paper": "SP", "comparison": "CMP", "no_answer": "NA", "partial_pdf": "PP",
                "permission": "ACL", "references_conflict_injection": "RCI"}
TOTAL_MINIMUM = 80
HOLDOUT_MINIMUM = 16
OWNER_PREFIX = {"user_a": "A", "user_b": "B", "admin": "ADM", "common": "C"}

Identity = Literal["user_a", "user_b", "admin"]
Owner = Literal["user_a", "user_b", "admin", "common"]
Group = Literal["single_paper", "comparison", "no_answer", "partial_pdf", "permission",
                "references_conflict_injection"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class License(Strict):
    kind: Literal["self_authored_synthetic"]
    redistribution_in_repo: bool
    statement: str = Field(min_length=1)


class Paper(Strict):
    id: str = Field(pattern=r"^P\d{2}$")
    title: str = Field(min_length=1)
    file: str = Field(min_length=1)
    format: Literal["markdown", "pdf"]
    version_label: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    pages: int | None = Field(default=None, gt=0)
    doi: str | None = None
    source: str = Field(min_length=1)
    license: License
    checked_on: date
    contains_third_party_text: Literal[False]
    expected_coverage: Literal["complete", "partial"]


class Item(Strict):
    key: str = Field(pattern=r"^(A|B|ADM|C)/[A-Z]\d{2}$")
    owner: Owner
    paper: str | None = None
    title: str | None = None
    doi: str | None = None
    acl_markers: list[str] = []


class Collection(Strict):
    key: str = Field(pattern=r"^[A-Za-z]+-[a-z-]+$")
    owner: Identity
    items: list[str] = Field(min_length=1)


class Representativeness(Strict):
    papers: int
    files: int
    private_items: int
    metadata_only_items: int
    target_papers: int
    statement: str = Field(min_length=1)


class Manifest(Strict):
    schema_version: Literal[1]
    corpus_id: str
    checked_on: date
    usage_scope: str = Field(min_length=1)
    representativeness: Representativeness
    papers: list[Paper]
    items: list[Item]
    collections: list[Collection]


class Span(Strict):
    item: str
    locator_kind: Literal["markdown", "pdf"]
    quote: str = Field(min_length=1)
    page: int | None = Field(default=None, gt=0)
    start_line: int | None = Field(default=None, gt=0)
    end_line: int | None = Field(default=None, gt=0)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)
    parser: str | None = None


class Side(Strict):
    label: str = Field(min_length=1)
    spans: list[Span] = Field(min_length=1)


class NonEvidence(Strict):
    item: str
    kind: Literal["bibliography_entry", "prompt_injection", "citing_statement", "conflicting_claim",
                  "not_extracted_region", "metadata_only", "forbidden_item"]
    quote: str | None = None
    page: int | None = Field(default=None, gt=0)
    why: str = Field(min_length=1)


class Scope(Strict):
    kind: Literal["item", "collection", "all"]
    item: str | None = None
    collection: str | None = None


class Expected(Strict):
    status: Literal["answered", "insufficient_evidence", "scope_not_found"]
    reason: Literal["no_supporting_evidence", "comparison_requires_multiple_papers", "not_extracted",
                    "bibliography_only", "unresolved_conflict", "forbidden_scope",
                    "not_a_paper_question"] | None = None
    coverage: Literal["complete", "partial"] | None = None


class Question(Strict):
    id: str = Field(pattern=r"^(SP|CMP|NA|PP|ACL|RCI)-\d{2}$")
    split: Literal["dev", "holdout"]
    group: Group
    tags: list[str] = []
    question: str = Field(min_length=1)
    identity: Identity
    scope: Scope
    expected: Expected
    evidence: list[Side] = []
    non_evidence: list[NonEvidence] = []
    forbidden_markers: list[str] = []
    supported_claim: str | None = None
    rationale: str = Field(min_length=1)
    label_status: Literal["pending_human_review"]
    label_author: Literal["coding_agent"]


class QuestionSet(Strict):
    schema_version: Literal[1]
    dataset_id: str
    created_on: date
    label_policy: str = Field(min_length=1)
    questions: list[Question]


def load(root: Path) -> tuple[Manifest, QuestionSet]:
    manifest = Manifest.model_validate_json((root / "manifest.json").read_text(encoding="utf-8"))
    questions = QuestionSet.model_validate_json((root / "questions.json").read_text(encoding="utf-8"))
    return manifest, questions


def save_questions(root: Path, questions: QuestionSet) -> None:
    data = questions.model_dump(mode="json", exclude_none=True)
    (root / "questions.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")


def _normal(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def allowed_items(manifest: Manifest, identity: str, scope: Scope) -> set[str]:
    """Items whose full text the identity may retrieve in this scope.

    Only private items of the caller have full text today; common items are
    metadata_only and a collection membership never grants access.
    """
    own = {item.key for item in manifest.items if item.owner == identity and item.paper is not None}
    if scope.kind == "item":
        return own & {scope.item}
    if scope.kind == "collection":
        collection = next((c for c in manifest.collections if c.key == scope.collection), None)
        if collection is None or collection.owner != identity:
            return set()
        return own & set(collection.items)
    return own


def scope_is_foreign(manifest: Manifest, identity: str, scope: Scope) -> bool:
    items = {item.key: item for item in manifest.items}
    if scope.kind == "item":
        item = items.get(scope.item)
        return item is None or item.owner != identity
    if scope.kind == "collection":
        collection = next((c for c in manifest.collections if c.key == scope.collection), None)
        return collection is None or collection.owner != identity
    return False


def check_manifest(manifest: Manifest, root: Path) -> list[str]:
    errors: list[str] = []
    papers = {paper.id: paper for paper in manifest.papers}
    items = {item.key: item for item in manifest.items}
    if len(papers) != len(manifest.papers):
        errors.append("manifest: duplicate paper id")
    if len(items) != len(manifest.items):
        errors.append("manifest: duplicate item key")
    for paper in manifest.papers:
        path = root / "corpus" / paper.file
        if not path.is_file():
            errors.append(f"{paper.id}: missing file {paper.file}")
            continue
        data = path.read_bytes()
        if sha256_bytes(data) != paper.sha256 or len(data) != paper.byte_size:
            errors.append(f"{paper.id}: checksum or size differs from manifest")
        if (paper.format == "pdf") != paper.file.endswith(".pdf") or (paper.format == "pdf") != (paper.pages is not None):
            errors.append(f"{paper.id}: format, extension and page count disagree")
        if not paper.license.redistribution_in_repo:
            errors.append(f"{paper.id}: full text committed without redistribution rights")
    for item in manifest.items:
        if not item.key.startswith(OWNER_PREFIX[item.owner] + "/"):
            errors.append(f"{item.key}: key prefix does not match owner {item.owner}")
        if item.owner == "common":
            if item.paper is not None or not item.title:
                errors.append(f"{item.key}: common items are metadata_only fixtures with a title and no file")
        elif item.paper not in papers:
            errors.append(f"{item.key}: unknown paper {item.paper}")
    for collection in manifest.collections:
        for key in collection.items:
            item = items.get(key)
            if item is None or item.owner not in (collection.owner, "common"):
                errors.append(f"collection {collection.key}: item {key} is missing or foreign")
    counts = manifest.representativeness
    if counts.papers != len(papers) or counts.files != len({p.file for p in manifest.papers}):
        errors.append("manifest: representativeness counts do not match papers")
    if counts.private_items != sum(i.owner != "common" for i in manifest.items) or \
            counts.metadata_only_items != sum(i.owner == "common" for i in manifest.items):
        errors.append("manifest: representativeness item counts do not match items")
    return errors


def check_questions(manifest: Manifest, qset: QuestionSet) -> list[str]:
    errors: list[str] = []
    questions = qset.questions
    items = {item.key: item for item in manifest.items}
    papers = {paper.id: paper for paper in manifest.papers}
    collections = {c.key for c in manifest.collections}
    ids = Counter(q.id for q in questions)
    errors += [f"{key}: duplicate question id" for key, n in ids.items() if n > 1]
    asked = Counter((_normal(q.question), q.identity, q.scope.kind, q.scope.item, q.scope.collection)
                    for q in questions)
    errors += [f"duplicate question: {key[0]!r} for {key[1]}" for key, n in asked.items() if n > 1]
    # The same wording may repeat only as an A/B/Admin pair with different rights.
    texts = Counter(_normal(q.question) for q in questions)
    errors += [f"{q.id}: repeated question text outside the permission group"
               for q in questions if texts[_normal(q.question)] > 1 and q.group != "permission"]
    groups = Counter(q.group for q in questions)
    for group, minimum in GROUP_MINIMUMS.items():
        if groups[group] < minimum:
            errors.append(f"group {group}: {groups[group]} questions, needs at least {minimum}")
    if len(questions) < TOTAL_MINIMUM:
        errors.append(f"{len(questions)} questions, needs at least {TOTAL_MINIMUM}")
    holdout = [q for q in questions if q.split == "holdout"]
    if len(holdout) < HOLDOUT_MINIMUM:
        errors.append(f"{len(holdout)} holdout questions, needs at least {HOLDOUT_MINIMUM}")
    for group in GROUP_MINIMUMS:
        if not any(q.group == group for q in holdout):
            errors.append(f"group {group}: no holdout question")
    markers = {marker: item for item in manifest.items for marker in item.acl_markers}
    for q in questions:
        where = q.id
        if not q.id.startswith(GROUP_PREFIX[q.group] + "-"):
            errors.append(f"{where}: id prefix does not match group {q.group}")
        scope = q.scope
        if (scope.kind == "item") != (scope.item is not None) or \
                (scope.kind == "collection") != (scope.collection is not None):
            errors.append(f"{where}: scope fields do not match scope kind")
            continue
        if scope.kind == "item" and scope.item not in items:
            errors.append(f"{where}: unknown scope item {scope.item}")
        if scope.kind == "collection" and scope.collection not in collections:
            errors.append(f"{where}: unknown scope collection {scope.collection}")
        foreign = scope_is_foreign(manifest, q.identity, scope)
        if foreign != (q.expected.status == "scope_not_found"):
            errors.append(f"{where}: scope_not_found must be expected exactly when the scope is not the caller's")
        answered = q.expected.status == "answered"
        if answered != bool(q.evidence):
            errors.append(f"{where}: evidence is required for answered questions and only for them")
        if answered and not q.supported_claim:
            errors.append(f"{where}: answered question needs supported_claim")
        if not answered and q.expected.reason is None:
            errors.append(f"{where}: a non-answer needs expected.reason")
        allowed = allowed_items(manifest, q.identity, scope)
        side_items = []
        for side in q.evidence:
            side_items.append({span.item for span in side.spans})
            for span in side.spans:
                item = items.get(span.item)
                if item is None or item.paper is None:
                    errors.append(f"{where}: evidence item {span.item} has no full text")
                    continue
                if span.item not in allowed:
                    errors.append(f"{where}: evidence item {span.item} is outside the caller's allowed scope")
                if papers[item.paper].format != span.locator_kind:
                    errors.append(f"{where}: locator kind does not match {item.paper} format")
                if span.locator_kind == "pdf" and span.page is None:
                    errors.append(f"{where}: PDF evidence needs a page")
                if span.start_offset is None or span.end_offset is None or span.parser is None or (
                        span.locator_kind == "markdown" and span.start_line is None):
                    errors.append(f"{where}: span in {span.item} is not annotated; run `annotate`")
                for marker in q.forbidden_markers:
                    if marker in span.quote:
                        errors.append(f"{where}: forbidden marker {marker} appears in evidence")
        if q.group == "comparison" and answered:
            distinct = {key for keys in side_items for key in keys}
            if len(q.evidence) < 2 or len(distinct) < 2:
                errors.append(f"{where}: an answered comparison needs sides from at least two papers")
        evidence_quotes = {(span.item, span.quote) for side in q.evidence for span in side.spans}
        for entry in q.non_evidence:
            if entry.item not in items:
                errors.append(f"{where}: non-evidence item {entry.item} unknown")
            if (entry.item, entry.quote) in evidence_quotes:
                errors.append(f"{where}: the same text is labelled evidence and {entry.kind}")
        for marker in q.forbidden_markers:
            owner = markers.get(marker)
            if owner is None:
                errors.append(f"{where}: forbidden marker {marker} is not an ACL marker in the manifest")
            elif owner.owner == q.identity:
                errors.append(f"{where}: forbidden marker {marker} belongs to the caller")
    return errors
