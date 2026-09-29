"""M5b-1: exact arXiv links from private reference entries to discovery metadata.

A link means only "the citing paper lists this work in its bibliography"; it
is never evidence and never enters a keyword/vector index or provider payload.
The matcher links only when a raw citation carries exactly one arXiv ID in an
explicit arXiv context (``arXiv:``, arxiv.org URL, arXiv DOI, ``abs/`` path).
A context must start a token, and a URL host must be exactly an arXiv host;
look-alikes ("fake-arXiv:", "evil-arxiv.org/abs/") only yield a bare ID.
Bare numbers, several IDs, malformed IDs and conflicting versions stay
unlinked; titles are never used.

Concurrency: link writers from ingest and the backfill hold a *shared*
transaction advisory lock while they read discovery state; the discovery page
writer holds it *exclusive* while it re-targets links. Under READ COMMITTED,
whichever side acquires second sees the other's committed rows, so a reference
and its discovery record arriving together cannot both miss each other.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import CitationPolicy
from app.models.citation import ReferenceCitationLink
from app.models.discovery import DiscoveryDeletedRecord, DiscoveryPaper
from app.models.ingest import ReferenceEntry

MATCHER_VERSION = "arxiv-exact-v2"
METHOD = "arxiv_id_exact"
LINK_LOCK = 0x5B1C17E
EXACT = ("linked", "no_discovery_record", "discovery_record_deleted")

# A marker must begin a token: start of text, whitespace or opening/separating
# punctuation. ``\b`` is not enough, since "fake-arXiv:" and "evil-arxiv.org"
# have a word boundary after "-" (v1 bug).
START = r"(?<![^\s(\[{<,;\"'\u201c\u2018\u00ab])"
# A URL may also follow "URL:"; its host is verified exactly, so ":" is safe there.
URL_START = r"(?<![^\s(\[{<,;:\"'\u201c\u2018\u00ab])"
ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
# Explicit arXiv contexts. ``abs/`` covers DBLP/Semantic Scholar style
# ("CoRR abs/1810.04805", "ArXiv, abs/2005.14165"). URL hosts are checked
# against ARXIV_HOSTS after matching; DOIs may carry a doi.org resolver.
MARKER = re.compile(rf"""
    (?P<arxiv_prefix>{START}arxiv[ \t]*:[ \t]*(?:\r?\n[ \t]*)?)
  | (?P<arxiv_url>{URL_START}(?:https?://)?(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+)/(?:abs|pdf)/)
  | (?P<arxiv_doi>{START}(?:doi[ \t]*:[ \t]*|(?:https?://)?(?:dx\.)?doi\.org/)?10\.48550/arxiv\.)
  | (?P<abs_path>{START}(?:corr|arxiv)[ \t]*,?[ \t]*abs/)
""", re.I | re.X)
# Anything that merely looks like one of those contexts. An ID after a
# look-alike that is not a trusted marker counts as a bare identifier.
LOOKALIKE = re.compile(r"arxiv[ \t]*:[ \t]*|(?:abs|pdf)/|10\.48550/arxiv\.", re.I)
TOKEN = re.compile(r"[^\s,;()\[\]{}<>\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]+")
NEW_ID = re.compile(r"(?P<yymm>\d{4})\.(?P<num>\d{4,5})(?P<ver>v[1-9]\d*)?")
OLD_ID = re.compile(r"(?P<archive>[a-z]+(?:-[a-z]+)?)(?:\.[A-Z]{2})?/(?P<yymm>\d{4})(?P<num>\d{3})(?P<ver>v[1-9]\d*)?")
BARE_NEW = re.compile(r"(?<![\w./:-])\d{4}\.\d{4,5}(?:v\d+)?(?![\w/])")
ARCHIVE_START = re.compile(r"[a-z]+(?:-[a-z]+)?(?:\.[A-Za-z]{2})?/")


@dataclass(frozen=True)
class ArxivParse:
    """``status`` is ``exact`` or one of the unlinked statuses of the link table."""
    status: str
    arxiv_id: str | None = None
    cited_version: str | None = None
    id_form: str | None = None
    start: int | None = None
    end: int | None = None


def normalize_arxiv_id(token: str) -> tuple[str, str | None] | None:
    """Return (base ID as arXiv OAI names it, version) or None if malformed."""
    match = NEW_ID.fullmatch(token)
    if match:
        yy, mm = int(match["yymm"][:2]), int(match["yymm"][2:])
        # New scheme began 0704; four-digit numbers until 1412, five after.
        if not 1 <= mm <= 12 or (yy, mm) < (7, 4) or int(match["num"]) == 0:
            return None
        if len(match["num"]) != (4 if (yy, mm) <= (14, 12) else 5):
            return None
        return f"{match['yymm']}.{match['num']}", match["ver"]
    match = OLD_ID.fullmatch(token)
    if match:
        yy, mm = int(match["yymm"][:2]), int(match["yymm"][2:])
        if not 1 <= mm <= 12 or not (yy >= 91 or (yy, mm) <= (7, 3)) or int(match["num"]) == 0:
            return None
        # OAI identifiers drop the subject class: math.GT/0309136 -> math/0309136.
        return f"{match['archive']}/{match['yymm']}{match['num']}", match["ver"]
    return None


def _clean(token: str, form: str) -> str:
    if form == "arxiv_url":
        token = re.split(r"[?#]", token, maxsplit=1)[0]
    token = token.rstrip(".:")
    if form == "arxiv_url":
        token = re.sub(r"\.pdf$", "", token.rstrip("/"), flags=re.I).rstrip("/")
    return token


def parse_citation(raw: str) -> ArxivParse:
    found: list[tuple[str, str | None, str, int, int]] = []
    invalid = False
    covered: list[tuple[int, int]] = []
    trusted_ends: set[int] = set()
    for marker in MARKER.finditer(raw):
        form = next(name for name in ("arxiv_prefix", "arxiv_url", "arxiv_doi", "abs_path") if marker.group(name))
        if form == "arxiv_url" and marker.group("host").lower() not in ARXIV_HOSTS:
            continue  # e.g. evil-arxiv.org, arxiv.org.evil.com: handled as a look-alike below
        trusted_ends.add(marker.end())
        token_match = TOKEN.match(raw, marker.end())
        if token_match is None:
            invalid = invalid or form != "arxiv_prefix"
            continue
        token = token_match.group()
        # "arXiv: https://arxiv.org/abs/..." — the URL marker handles it.
        if form == "arxiv_prefix" and not (token[0].isdigit() or ARCHIVE_START.match(token)):
            continue
        cleaned = _clean(token, form)
        start = token_match.start()
        covered.append((marker.start(), token_match.end()))
        parsed = normalize_arxiv_id(cleaned)
        if parsed is None:
            invalid = True
            continue
        found.append((parsed[0], parsed[1], form, start, start + len(cleaned)))
    bare: list[tuple[str, str | None]] = []
    for match in BARE_NEW.finditer(raw):
        if any(a <= match.start() < b for a, b in covered):
            continue
        parsed = normalize_arxiv_id(match.group())
        if parsed is not None:
            bare.append(parsed)
    for lookalike in LOOKALIKE.finditer(raw):
        if lookalike.end() in trusted_ends:
            continue
        token_match = TOKEN.match(raw, lookalike.end())
        parsed = normalize_arxiv_id(_clean(token_match.group(), "arxiv_url")) if token_match else None
        if parsed is not None:
            bare.append(parsed)
    if invalid:
        return ArxivParse("invalid_arxiv_id")
    ids = {base for base, *_ in found} | {base for base, _ in bare}
    if len(ids) > 1:
        return ArxivParse("multiple_arxiv_ids")
    if not found:
        return ArxivParse("bare_identifier" if bare else "no_arxiv_id")
    versions = {v for _, v, *_ in found if v} | {v for _, v in bare if v}
    if len(versions) > 1:
        return ArxivParse("conflicting_versions")
    base, _, form, start, end = found[0]
    return ArxivParse("exact", base, next(iter(versions), None), form, start, end)


def citation_sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sql_citation_sha256(column):
    """Same digest in SQL, for freshness checks before count/pagination."""
    return func.encode(func.sha256(func.convert_to(column, "UTF8")), "hex")


def hidden_discovery_clause():
    """The M5a discovery listing hides a paper behind a newer-or-equal tombstone."""
    return select(DiscoveryDeletedRecord.source).where(
        DiscoveryDeletedRecord.source == DiscoveryPaper.source,
        DiscoveryDeletedRecord.remote_id == DiscoveryPaper.remote_id,
        DiscoveryDeletedRecord.oai_datestamp >= DiscoveryPaper.oai_datestamp).exists()


def _lock(db: Session, *, exclusive: bool) -> None:
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    db.execute(text(f"SELECT {fn}(:key)"), {"key": LINK_LOCK})


def _targets(db: Session, arxiv_ids: set[str]) -> dict[str, tuple[str, uuid.UUID | None]]:
    if not arxiv_ids:
        return {}
    papers = {r.remote_id: (r.id, r.oai_datestamp) for r in db.execute(
        select(DiscoveryPaper.remote_id, DiscoveryPaper.id, DiscoveryPaper.oai_datestamp)
        .where(DiscoveryPaper.source == "arxiv", DiscoveryPaper.remote_id.in_(arxiv_ids),
               DiscoveryPaper.content_status == "metadata_only"))}
    stones = dict(db.execute(select(DiscoveryDeletedRecord.remote_id, DiscoveryDeletedRecord.oai_datestamp)
                  .where(DiscoveryDeletedRecord.source == "arxiv",
                         DiscoveryDeletedRecord.remote_id.in_(arxiv_ids))).all())
    result = {}
    for arxiv_id in arxiv_ids:
        paper = papers.get(arxiv_id)
        stone = stones.get(arxiv_id)
        if paper is not None and (stone is None or stone < paper[1]):
            result[arxiv_id] = ("linked", paper[0])
        elif stone is not None:
            result[arxiv_id] = ("discovery_record_deleted", None)
        else:
            result[arxiv_id] = ("no_discovery_record", None)
    return result


def reconcile_references(db: Session, *, reference_ids: Iterable[uuid.UUID] | None = None,
                         source_file_id: uuid.UUID | None = None, pipeline_version: str | None = None,
                         now: datetime | None = None) -> int:
    """(Re)compute links for references in the caller's transaction. Idempotent."""
    now = now or datetime.now(UTC)
    stmt = select(ReferenceEntry.id, ReferenceEntry.raw_citation)
    if reference_ids is not None:
        stmt = stmt.where(ReferenceEntry.id.in_(list(reference_ids)))
    if source_file_id is not None:
        stmt = stmt.where(ReferenceEntry.source_file_id == source_file_id)
    if pipeline_version is not None:
        stmt = stmt.where(ReferenceEntry.pipeline_version == pipeline_version)
    _lock(db, exclusive=False)
    rows = db.execute(stmt.order_by(ReferenceEntry.id)).all()
    parsed = {row.id: (parse_citation(row.raw_citation), citation_sha256(row.raw_citation)) for row in rows}
    targets = _targets(db, {p.arxiv_id for p, _ in parsed.values() if p.arxiv_id})
    table = ReferenceCitationLink.__table__
    for reference_id, (p, digest) in parsed.items():
        status, paper_id = targets[p.arxiv_id] if p.arxiv_id else (p.status, None)
        values = dict(matcher_version=MATCHER_VERSION, raw_citation_sha256=digest, match_status=status,
                      match_method=METHOD if p.arxiv_id else None, id_form=p.id_form, arxiv_id=p.arxiv_id,
                      cited_version=p.cited_version, id_start_offset=p.start, id_end_offset=p.end,
                      discovery_paper_id=paper_id)
        stmt = insert(table).values(reference_id=reference_id, reconciled_at=now, **values)
        changed = tuple_(*(table.c[k] for k in values)).is_distinct_from(
            tuple_(*(stmt.excluded[k] for k in values)))
        db.execute(stmt.on_conflict_do_update(index_elements=["reference_id"],
                                              set_={**values, "reconciled_at": now}, where=changed))
    return len(parsed)


def reconcile_arxiv_ids(db: Session, arxiv_ids: Iterable[str], now: datetime | None = None) -> int:
    """Re-target existing exact links after discovery rows or tombstones change."""
    ids = set(arxiv_ids)
    if not ids:
        return 0
    now = now or datetime.now(UTC)
    _lock(db, exclusive=True)
    targets = _targets(db, ids)
    changed = 0
    for link in db.scalars(select(ReferenceCitationLink).where(
            ReferenceCitationLink.arxiv_id.in_(ids), ReferenceCitationLink.match_status.in_(EXACT))
            .order_by(ReferenceCitationLink.reference_id)).all():
        status, paper_id = targets[link.arxiv_id]
        if (link.match_status, link.discovery_paper_id) != (status, paper_id):
            link.match_status, link.discovery_paper_id, link.reconciled_at = status, paper_id, now
            changed += 1
    db.flush()
    return changed


RELATION_NOTE = ("A citation link only means the citing paper lists the cited work in its bibliography; "
                 "it does not mean agreement with, or confirmation of, the cited work's content.")


def fresh_linked_clause():
    """SQL gate for a link that may be shown as a live target right now."""
    link = ReferenceCitationLink
    return (link.match_status == "linked") & (link.matcher_version == MATCHER_VERSION) \
        & (link.raw_citation_sha256 == sql_citation_sha256(ReferenceEntry.raw_citation)) \
        & (DiscoveryPaper.content_status == "metadata_only") & ~hidden_discovery_clause()


def citation_link_out(reference: ReferenceEntry, link: ReferenceCitationLink | None,
                      paper: DiscoveryPaper | None, target_hidden: bool | None) -> dict:
    """Owner-facing link state. Stale rows and tombstoned targets never show a target."""
    base = {"relation": "cites", "matcher_version": MATCHER_VERSION, "target": None}
    if (link is None or link.matcher_version != MATCHER_VERSION
            or link.raw_citation_sha256 != citation_sha256(reference.raw_citation)):
        return base | {"status": "not_reconciled", "linked": False, "method": None, "id_form": None,
                       "arxiv_id": None, "cited_version": None, "id_span": None}
    status = link.match_status
    if status == "linked" and (paper is None or target_hidden or paper.content_status != "metadata_only"):
        status = "discovery_record_deleted"
    out = base | {"status": status, "linked": status == "linked", "method": link.match_method,
                  "id_form": link.id_form, "arxiv_id": link.arxiv_id, "cited_version": link.cited_version,
                  "id_span": None if link.id_start_offset is None else {
                      "start": link.id_start_offset, "end": link.id_end_offset,
                      "offset_unit": "raw_citation_codepoint"}}
    if status == "linked":
        out["target"] = {
            "discovery_paper_id": str(paper.id), "source": paper.source, "arxiv_id": paper.remote_id,
            "title": paper.title, "original_url": paper.original_url,
            # The cited version is what the citation says, not the latest metadata version.
            "cited_version_url": f"https://arxiv.org/abs/{paper.remote_id}{link.cited_version}"
                                 if link.cited_version else None,
            "latest_metadata_version": paper.latest_version,
            "content_status": paper.content_status, "label": "metadata-only"}
    return out


def pending_references_clause():
    link = ReferenceCitationLink
    return or_(link.reference_id.is_(None), link.matcher_version != MATCHER_VERSION,
               link.raw_citation_sha256 != sql_citation_sha256(ReferenceEntry.raw_citation))


def reconcile_pending(engine: Engine, policy: CitationPolicy, now: datetime | None = None) -> dict[str, int]:
    """Bounded backfill for references without a fresh link row."""
    if not policy.enabled:
        return {"batches": 0, "references": 0}
    batches = total = 0
    for _ in range(policy.reconcile_max_batches):
        with Session(engine) as db:
            ids = db.scalars(select(ReferenceEntry.id).outerjoin(
                ReferenceCitationLink, ReferenceCitationLink.reference_id == ReferenceEntry.id)
                .where(pending_references_clause()).order_by(ReferenceEntry.id)
                .limit(policy.reconcile_batch_size)).all()
            if not ids:
                break
            total += reconcile_references(db, reference_ids=ids, now=now)
            db.commit()
            batches += 1
            if len(ids) < policy.reconcile_batch_size:
                break
    return {"batches": batches, "references": total}
