"""Human review sheet for generated answers, plus structural machine checks.

A machine may check that a citation's chunk ID was in the evidence pack, that
its quote is verbatim chunk text and whether its locator contains a whole gold span.
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
    "observed_status", "response_claims", "claim_no", "claim_text", "claim_citations", "citation_no",
    "citation_item", "citation_locator",
    "citation_quote", "machine_id_in_evidence", "machine_quote_verbatim", "machine_locator_covers_gold",
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
            "machine_locator_covers_gold": "yes" if any(covers(span, hit) for side in question.evidence
                                                          for span in side.spans) else "no"}


def answer_rows(question: Question, mode: str, response: dict, evidence: dict[str, dict],
                item_key_of) -> list[dict]:
    """Rows for one generated response: one per citation, or one answer row if there are no claims."""
    base = {column: "" for column in COLUMNS} | _base(question, mode)
    base["observed_status"] = response.get("status", "")
    claims = response.get("claims", [])
    base["response_claims"] = str(len(claims))
    rows = []
    for claim_no, claim in enumerate(claims, 1):
        for citation_no, citation in enumerate(claim["citations"], 1):
            key = item_key_of(citation["item_id"])
            where = (f"p{citation['start_page']}" if citation["locator_kind"] == "pdf"
                     else f"L{citation['start_line']}-{citation['end_line']}")
            rows.append(base | {"claim_no": str(claim_no), "claim_text": claim["text"],
                                "claim_citations": str(len(claim["citations"])),
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


def _numbered(values: list[str], expected: str) -> bool:
    """Row numbers are exactly 1..n, n as recorded when the sheet was generated."""
    try:
        return sorted(int(value) for value in values) == list(range(1, int(expected) + 1))
    except ValueError:
        return False


def _graded(row: dict) -> bool:
    """A claim/citation row is graded when a named reviewer filled every judgement it needs."""
    return bool(row.get("reviewer") and row.get("human_claim_supported")
                and (not row.get("citation_no") or row.get("human_citation_correct")))


def grounded_status(rows: list[dict]) -> tuple[str, bool | None]:
    """Classify one answered response as complete, incomplete, conflicting or unreviewed.

    Only a complete response has a grounded verdict: every claim and every citation row
    the sheet generated is present and graded, and repeated rows of one claim agree.
    """
    if not any(row.get("reviewer") or any(row.get(column) for column in HUMAN_VALUES) for row in rows):
        return "unreviewed", None
    claims: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("claim_no"):
            claims.setdefault(row["claim_no"], []).append(row)
    for claim_rows in claims.values():
        verdicts = {row["human_claim_supported"] for row in claim_rows if row["human_claim_supported"]}
        citations = [row["citation_no"] for row in claim_rows]
        if (len({row["claim_text"] for row in claim_rows}) > 1 or len(verdicts) > 1
                or len(set(citations)) != len(citations)
                or len({row["claim_citations"] for row in claim_rows}) > 1):
            return "conflicting", None
    if len({row["response_claims"] for row in rows}) > 1:
        return "conflicting", None
    if (not claims or not _numbered(list(claims), rows[0]["response_claims"])
            or any(not _numbered([r["citation_no"] for r in claim_rows], claim_rows[0]["claim_citations"])
                   for claim_rows in claims.values())
            or any(not _graded(row) for claim_rows in claims.values() for row in claim_rows)):
        return "incomplete", None
    return "complete", all(claim_rows[0]["human_claim_supported"] == "yes" for claim_rows in claims.values())


def score(rows: list[dict]) -> dict:
    """Human-reviewed metrics with denominators; partial or contradictory sheets are reported, never scored.

    - grounded_answer_rate: complete responses with status ``answered`` (see ``grounded_status``)
      whose every claim is ``human_claim_supported=yes``. Incomplete, conflicting and unreviewed
      responses are counted separately and excluded from the denominator.
    - citation_accuracy: citation rows with a reviewer and ``human_citation_correct=yes``.
    - abstention_appropriateness: responses to questions expected not to be answered with one
      consistent reviewed ``human_abstention_appropriate`` verdict marked ``yes``.
    """
    errors = [f"line {number}: {column}={row.get(column)!r}"
              for number, row in enumerate(rows, 2)
              for column, allowed in HUMAN_VALUES.items() if row.get(column, "") not in allowed]
    if errors:
        raise ValueError("; ".join(errors))
    responses: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        responses.setdefault((row["question_id"], row["mode"]), []).append(row)
    states: dict[str, list[str]] = {"complete": [], "incomplete": [], "conflicting": [], "unreviewed": []}
    grounded = 0
    for (question_id, mode), group in sorted(responses.items()):
        if group[0]["observed_status"] != "answered":
            continue
        state, verdict = grounded_status(group)
        states[state].append(f"{question_id}/{mode}")
        grounded += bool(verdict)
    citations = [row for row in rows if row.get("citation_no") and row.get("reviewer")
                 and row.get("human_citation_correct")]
    abstention = {"graded": [], "conflicting": [], "unreviewed": []}
    appropriate = 0
    for (question_id, mode), group in sorted(responses.items()):
        if group[0]["expected_status"] == "answered":
            continue
        verdicts = {row["human_abstention_appropriate"] for row in group if row.get("reviewer")
                    and row["human_abstention_appropriate"] in ("yes", "no")}
        state = "unreviewed" if not verdicts else "conflicting" if len(verdicts) > 1 else "graded"
        abstention[state].append(f"{question_id}/{mode}")
        appropriate += verdicts == {"yes"}
    reviewed = sum(bool(row.get("reviewer")) for row in rows)
    return {
        "grounded_answer_rate": _rate(grounded, len(states["complete"])),
        "answered_responses": {name: len(ids) for name, ids in states.items()}
                              | {"incomplete_ids": states["incomplete"], "conflicting_ids": states["conflicting"]},
        "citation_accuracy": _rate(sum(row["human_citation_correct"] == "yes" for row in citations), len(citations)),
        "abstention_appropriateness": _rate(appropriate, len(abstention["graded"])),
        "abstention_responses": {name: len(ids) for name, ids in abstention.items()}
                                | {"conflicting_ids": abstention["conflicting"]},
        "rows": len(rows), "reviewed_rows": reviewed, "unreviewed_rows": len(rows) - reviewed,
    }
