"""Human review sheet for generated answers, plus structural machine checks.

A machine may check that a citation's chunk ID was in the evidence pack, that
its quote is verbatim chunk text and whether its locator overlaps a gold span.
Whether a claim is true and whether the cited text supports it are recorded
only by a human reader in the ``human_*`` columns; nothing here infers them,
and no LLM grades claims.
"""
from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from app.evaluation.locator import Hit, covers
from app.evaluation.schema import Question, QuestionSet

COLUMNS = [
    "question_id", "split", "group", "identity", "scope", "mode", "expected_status", "gold_locators",
    "observed_status", "claim_no", "claim_text", "citation_no", "citation_item", "citation_locator",
    "citation_quote", "machine_id_in_evidence", "machine_quote_verbatim", "machine_locator_overlaps_gold",
    "human_claim_supported", "human_citation_correct", "human_abstention_appropriate",
    "reviewer", "review_date", "notes",
]
HUMAN_VALUES = {
    "human_claim_supported": {"", "yes", "partial", "no"},
    "human_citation_correct": {"", "yes", "no"},
    "human_abstention_appropriate": {"", "yes", "no", "n/a"},
}


def gold_locators(question: Question) -> str:
    parts = []
    for side in question.evidence:
        for span in side.spans:
            where = f"p{span.page}" if span.locator_kind == "pdf" else f"L{span.start_line}-{span.end_line}"
            parts.append(f"{side.label}: {span.item} {where} [{span.start_offset}:{span.end_offset}]")
    return "; ".join(parts)


def _base(question: Question, mode: str) -> dict:
    scope = question.scope.item or question.scope.collection or question.scope.kind
    return {"question_id": question.id, "split": question.split, "group": question.group,
            "identity": question.identity, "scope": scope, "mode": mode,
            "expected_status": question.expected.status, "gold_locators": gold_locators(question)}


def template_rows(qset: QuestionSet, modes: Iterable[str]) -> list[dict]:
    """One answer-level row per question and mode; add claim/citation rows once answers exist."""
    return [{column: "" for column in COLUMNS} | _base(q, mode) for q in qset.questions for mode in modes]


def machine_check(citation: dict, evidence: dict[str, dict], question: Question, item_key: str) -> dict:
    """Structural checks only; they never decide whether the claim is correct."""
    row = evidence.get(citation["chunk_id"])
    detail = citation.get("locator_detail") or {}
    start = detail.get("start_offset") if citation["locator_kind"] == "pdf" else citation.get("start_offset")
    end = detail.get("end_offset") if citation["locator_kind"] == "pdf" else citation.get("end_offset")
    hit = Hit(item_key, citation["locator_kind"], citation.get("start_page"),
              -1 if start is None else start, -1 if end is None else end)
    return {"machine_id_in_evidence": "yes" if row is not None else "no",
            "machine_quote_verbatim": "yes" if row is not None and citation["quote"] in row["text"] else "no",
            "machine_locator_overlaps_gold": "yes" if any(covers(span, hit) for side in question.evidence
                                                          for span in side.spans) else "no"}


def answer_rows(question: Question, mode: str, response: dict, evidence: dict[str, dict],
                item_key_of) -> list[dict]:
    """Rows for one generated response: one per citation, or one answer row if there are no claims."""
    base = {column: "" for column in COLUMNS} | _base(question, mode)
    base["observed_status"] = response.get("status", "")
    rows = []
    for claim_no, claim in enumerate(response.get("claims", []), 1):
        for citation_no, citation in enumerate(claim["citations"], 1):
            key = item_key_of(citation["item_id"])
            where = (f"p{citation['start_page']}" if citation["locator_kind"] == "pdf"
                     else f"L{citation['start_line']}-{citation['end_line']}")
            rows.append(base | {"claim_no": str(claim_no), "claim_text": claim["text"],
                                "citation_no": str(citation_no), "citation_item": key,
                                "citation_locator": where, "citation_quote": citation["quote"]}
                        | machine_check(citation, evidence, question, key))
    return rows or [base]


def write_sheet(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_sheet(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": round(numerator / denominator, 4) if denominator else None}


def score(rows: list[dict]) -> dict:
    """Human-reviewed metrics with denominators; unreviewed rows are counted, never scored.

    - grounded_answer_rate: reviewed responses with status ``answered`` whose every claim row
      is ``human_claim_supported=yes``.
    - citation_accuracy: reviewed citation rows marked ``human_citation_correct=yes``.
    - abstention_appropriateness: reviewed responses to questions expected not to be answered
      that are marked ``human_abstention_appropriate=yes``.
    """
    errors = []
    for number, row in enumerate(rows, 2):
        for column, allowed in HUMAN_VALUES.items():
            if row.get(column, "") not in allowed:
                errors.append(f"line {number}: {column}={row.get(column)!r}")
        judged = any(row.get(column) for column in HUMAN_VALUES)
        if judged and not row.get("reviewer"):
            errors.append(f"line {number}: human judgement without reviewer")
    if errors:
        raise ValueError("; ".join(errors))
    reviewed = [row for row in rows if row.get("reviewer")]
    responses: dict[tuple[str, str], list[dict]] = {}
    for row in reviewed:
        responses.setdefault((row["question_id"], row["mode"]), []).append(row)
    answered = [group for group in responses.values() if group[0]["observed_status"] == "answered"]
    grounded = sum(all(row["human_claim_supported"] == "yes" for row in group if row["claim_no"]) and
                   any(row["claim_no"] for row in group) for group in answered)
    citations = [row for row in reviewed if row["citation_no"] and row["human_citation_correct"]]
    verdicts = [next((row["human_abstention_appropriate"] for row in group
                      if row["human_abstention_appropriate"] in ("yes", "no")), None)
                for group in responses.values() if group[0]["expected_status"] != "answered"]
    verdicts = [verdict for verdict in verdicts if verdict is not None]
    return {
        "grounded_answer_rate": _rate(grounded, len(answered)),
        "citation_accuracy": _rate(sum(row["human_citation_correct"] == "yes" for row in citations), len(citations)),
        "abstention_appropriateness": _rate(verdicts.count("yes"), len(verdicts)),
        "rows": len(rows), "reviewed_rows": len(reviewed), "unreviewed_rows": len(rows) - len(reviewed),
    }
