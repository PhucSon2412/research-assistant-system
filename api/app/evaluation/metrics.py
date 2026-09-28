"""Recall@k and failure attribution with explicit denominators.

Each gold *side* is one required piece of evidence (a comparison has one side
per paper). A side is found at rank r when the r-th retrieved chunk contains
the whole of one of its acceptable spans (``locator.covers``; a partial-overlap
rule exists only as a labelled diagnostic). Per question, the value is sides
found within k / sides: Recall@5 for a ranked search list, or coverage of the
whole evidence pack when k is the pack size. Group figures report the mean of
per-question values and the pooled ``sides_hit / sides_total`` so the
denominator is always visible.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.evaluation.locator import FULL_SPAN, Hit, covers
from app.evaluation.schema import Question

K = 5


def side_ranks(question: Question, hits: Sequence[Hit], minimum: float = FULL_SPAN) -> list[int | None]:
    """1-based rank of the first hit covering each side, or None."""
    ranks: list[int | None] = []
    for side in question.evidence:
        ranks.append(next((rank for rank, hit in enumerate(hits, 1)
                           if any(covers(span, hit, minimum) for span in side.spans)), None))
    return ranks


def sides_within(ranks: Sequence[int | None], k: int = K) -> int:
    return sum(rank is not None and rank <= k for rank in ranks)


def recall(ranks: Sequence[int | None], k: int = K) -> float:
    if not ranks:
        raise ValueError("recall needs at least one gold side")
    return sides_within(ranks, k) / len(ranks)


def failure(ranks: Sequence[int | None], indexed: Sequence[bool], *, acl_violation: bool,
            k: int = K, pool: str = "retrieval") -> str:
    """First failing stage for one answered question.

    ``indexed[i]`` says whether side i is covered by any active indexed chunk;
    ``ranks`` come from the returned list (the search result pool or the
    evidence pack). Order: ACL, extraction/indexing, not in pool, beyond k.
    """
    if acl_violation:
        return "acl"
    if not all(indexed):
        return "extraction"
    if any(rank is None for rank in ranks):
        return pool
    if any(rank > k for rank in ranks):
        return "rank"
    return "ok"


@dataclass(frozen=True)
class Row:
    question_id: str
    group: str
    split: str
    scope: str
    stage: str
    ranks: tuple[int | None, ...]
    outcome: str


def aggregate(rows: Iterable[Row], keys: Sequence[str], k: int = K) -> list[dict]:
    """Group rows by ``keys``; every figure carries its denominator."""
    groups: dict[tuple, list[Row]] = {}
    for row in rows:
        groups.setdefault(tuple(getattr(row, key) for key in keys), []).append(row)
    table = []
    for key in sorted(groups):
        members = groups[key]
        per_question = [recall(row.ranks, k) for row in members]
        multi = [row for row in members if len(row.ranks) > 1]
        entry = dict(zip(keys, key))
        entry |= {
            "questions": len(members),
            "mean_per_question": round(sum(per_question) / len(per_question), 4),
            "sides_hit": sum(sides_within(row.ranks, k) for row in members),
            "sides_total": sum(len(row.ranks) for row in members),
            "all_sides_hit": sum(sides_within(row.ranks, k) == len(row.ranks) for row in multi),
            "multi_side_questions": len(multi),
            "outcomes": {name: sum(row.outcome == name for row in members)
                         for name in sorted({row.outcome for row in members})},
        }
        table.append(entry)
    return table
