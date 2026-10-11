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

Lock order (C2d-5f1): item row -> reference row -> citation advisory ->
journal. A link write on a common item makes the 0024 fence take that item
``FOR KEY SHARE``, so every writer locks the common items first: extraction
already holds its item ``FOR UPDATE``; the backfill takes ``FOR KEY SHARE SKIP
LOCKED``; the discovery hook waits for its items *before* the exclusive
citation lock and, under it, only ``NOWAIT`` for an item that appeared since.
No citation-lock holder ever waits for an item row.

References of a revoked or tombstoned common copy are skipped by the backfill
and the discovery re-target: the DB refuses link writes for such a copy from
``t_rev`` until cleanup deletes its references (0024).
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

from app import auth_safety
from app.clock import utcnow
from app.config import CitationPolicy, get_settings
from app.ingest import runtime
from app.models.citation import ReferenceCitationLink
from app.models.discovery import DiscoveryDeletedRecord, DiscoveryPaper
from app.models.enums import PublicationState
from app.models.ingest import ReferenceEntry
from app.models.library import LibraryItem
from app.models.rights import DeletionTombstone

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


def _link_values(raw: str, targets: dict) -> dict:
    p, digest = parse_citation(raw), citation_sha256(raw)
    status, paper_id = targets[p.arxiv_id] if p.arxiv_id else (p.status, None)
    return dict(matcher_version=MATCHER_VERSION, raw_citation_sha256=digest, match_status=status,
                match_method=METHOD if p.arxiv_id else None, id_form=p.id_form, arxiv_id=p.arxiv_id,
                cited_version=p.cited_version, id_start_offset=p.start, id_end_offset=p.end,
                discovery_paper_id=paper_id)


def _upsert(db: Session, rows, now: datetime) -> tuple[dict, set]:
    """Under the citation lock: expected values per reference and the IDs actually inserted/updated.

    An identical existing row is left untouched (no ``reconciled_at`` bump) and
    is not counted as a mutation.
    """
    targets = _targets(db, {p.arxiv_id for p in (parse_citation(r.raw_citation) for r in rows) if p.arxiv_id})
    table = ReferenceCitationLink.__table__
    expected, written = {}, set()
    for row in rows:
        values = expected[row.id] = _link_values(row.raw_citation, targets)
        stmt = insert(table).values(reference_id=row.id, reconciled_at=now, **values)
        changed = tuple_(*(table.c[k] for k in values)).is_distinct_from(
            tuple_(*(stmt.excluded[k] for k in values)))
        if db.execute(stmt.on_conflict_do_update(index_elements=["reference_id"], set_={**values, "reconciled_at": now},
                                                 where=changed).returning(table.c.reference_id)).first():
            written.add(row.id)
    return expected, written


def reconcile_references(db: Session, *, reference_ids: Iterable[uuid.UUID] | None = None,
                         source_file_id: uuid.UUID | None = None, pipeline_version: str | None = None,
                         now: datetime | None = None) -> int:
    """(Re)compute links for references in the caller's transaction. Idempotent.

    Hook of the extraction transaction: the caller already holds its User/item/job
    locks and owns the final boundary. Never commits, opens a Session or takes the
    journal lock; only the shared citation lock (after the caller's item lock).
    """
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
    _upsert(db, rows, now)
    return len(rows)


def closed_item_clause(item_id):
    """A common copy that is revoked or has a tombstone: no new/updated link (0024 fence).

    Own aliases, so it never auto-correlates with an outer ``library_items``.
    """
    item, stone = aliased(LibraryItem), aliased(DeletionTombstone)
    return select(item.id).where(item.id == item_id, or_(
        item.publication_state == PublicationState.REVOKED.value,
        select(stone.id).where(stone.item_id == item.id).exists())).exists()


def not_revoked_reference_clause():
    """References whose item may still get links: not a revoked/tombstoned common copy (awaiting cleanup)."""
    return ~closed_item_clause(ReferenceEntry.item_id)


class RetargetBusy(Exception):
    """An item that appeared under the exclusive citation lock is locked: retry the page later."""

    def __init__(self):
        super().__init__("citation_retarget_busy")


class RetargetLimit(Exception):
    def __init__(self):
        super().__init__("retarget_limit_exceeded")


def _exact_links(ids: set[str]):
    return (select(ReferenceCitationLink.reference_id, ReferenceCitationLink.arxiv_id,
                   ReferenceCitationLink.match_status, ReferenceCitationLink.discovery_paper_id,
                   ReferenceEntry.item_id, LibraryItem.visibility)
            .join(ReferenceEntry, ReferenceEntry.id == ReferenceCitationLink.reference_id)
            .join(LibraryItem, LibraryItem.id == ReferenceEntry.item_id)
            .where(ReferenceCitationLink.arxiv_id.in_(ids), ReferenceCitationLink.match_status.in_(EXACT),
                   not_revoked_reference_clause()))


def _key_share(db: Session, item_ids, *, nowait: bool = False, skip: bool = False) -> set:
    if not item_ids:
        return set()
    return set(db.scalars(select(LibraryItem.id).where(LibraryItem.id.in_(sorted(item_ids)))
                          .order_by(LibraryItem.id)  # read+key_share renders FOR KEY SHARE
                          .with_for_update(read=True, key_share=True, nowait=nowait, skip_locked=skip)))


def reconcile_arxiv_ids(db: Session, arxiv_ids: Iterable[str], now: datetime | None = None,
                        *, limit: int | None = None) -> int:
    """Re-target existing exact links after discovery rows or tombstones change.

    Discovery hook in the page transaction; never commits. Common items of the
    candidate links are locked (UUID order, waiting) BEFORE the exclusive
    citation lock; an item that only appears under it is tried ``NOWAIT`` and
    a busy one aborts the page with ``RetargetBusy`` (nothing partial commits,
    the page cursor is not advanced). Discovery supplies a hard page fan-out cap.
    """
    ids = set(arxiv_ids)
    if not ids:
        return 0
    now = now or datetime.now(UTC)
    def candidates():
        stmt = _exact_links(ids).order_by(ReferenceCitationLink.reference_id)
        rows = db.execute(stmt.limit(limit + 1) if limit is not None else stmt).all()
        if limit is not None and len(rows) > limit:
            raise RetargetLimit()
        return rows
    hint = {r.item_id for r in candidates() if r.visibility == "common"}
    _key_share(db, hint)
    _lock(db, exclusive=True)
    rows = candidates()
    late = {r.item_id for r in rows if r.visibility == "common"} - hint
    if late:
        try:
            _key_share(db, late, nowait=True)
        except DBAPIError as exc:
            if getattr(getattr(exc, "orig", None), "sqlstate", None) == "55P03":
                raise RetargetBusy() from None
            raise
        rows = candidates()
    targets = _targets(db, ids)
    link = ReferenceCitationLink
    changed = 0
    for row in rows:
        status, paper_id = targets[row.arxiv_id]
        if (row.match_status, row.discovery_paper_id) != (status, paper_id):
            changed += db.execute(update(link).where(
                link.reference_id == row.reference_id, link.arxiv_id == row.arxiv_id,
                link.match_status == row.match_status,
                link.discovery_paper_id.is_not_distinct_from(row.discovery_paper_id))
                .values(match_status=status, discovery_paper_id=paper_id, reconciled_at=now)
                .execution_options(synchronize_session=False)).rowcount
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


# Each privilege on its own: has_table_privilege('t','A,B') is true when ANY listed privilege is held.
# UPDATE on reference_entries/library_items is what FOR SHARE / FOR KEY SHARE require.
NEEDED = (('reference_citation_links', 'SELECT'), ('reference_citation_links', 'INSERT'),
          ('reference_citation_links', 'UPDATE'), ('reference_entries', 'SELECT'), ('reference_entries', 'UPDATE'),
          ('library_items', 'SELECT'), ('library_items', 'UPDATE'), ('deletion_tombstones', 'SELECT'),
          ('discovery_papers', 'SELECT'), ('discovery_deleted_records', 'SELECT'))
GRANTS = text('SELECT ' + ' AND '.join(f"has_table_privilege('{t}','{p}')" for t, p in NEEDED))
BUSY = 'citation_lock_busy'
LINK_FIELDS = ('matcher_version', 'raw_citation_sha256', 'match_status', 'match_method', 'id_form', 'arxiv_id',
               'cited_version', 'id_start_offset', 'id_end_offset', 'discovery_paper_id')


def entry(db: Session) -> None:
    """Actual runtime login, its citation grants, then the writer gate, in its own released transaction."""
    def check(_):
        runtime.require_runtime(db)
        try:
            granted = db.scalar(GRANTS)
        except Exception:
            raise runtime.Halt('worker_runtime_unavailable') from None
        if granted is not True:
            raise runtime.Halt('worker_runtime_required')
        try:
            auth_safety.require_writer(db)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
    runtime.short(db, check)


def _now() -> datetime:
    now = utcnow()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise runtime.Halt('citation_clock_invalid')
    return now.astimezone(UTC)


@dataclass(frozen=True)
class Batch:
    """Facts of one batch transaction; counted only after a known commit and a clean release."""
    last: uuid.UUID | None
    selected: int
    references: int = 0
    written: int = 0
    skipped: tuple = ()


def _binding(db: Session, ids) -> dict:
    """Scalar state under the journal lock: link, current reference digest/item, item still open."""
    link, ref = ReferenceCitationLink, ReferenceEntry
    rows = db.execute(select(link.reference_id, *(getattr(link, k) for k in LINK_FIELDS),
                             sql_citation_sha256(ref.raw_citation).label('current'), ref.item_id,
                             closed_item_clause(ref.item_id).label('closed'))
                      .join(ref, ref.id == link.reference_id).where(link.reference_id.in_(ids))).all()
    return {r.reference_id: r for r in rows}


def _batch(engine: Engine, policy: CitationPolicy, after: uuid.UUID | None, facts: dict) -> Batch:
    db = Session(engine, expire_on_commit=False)
    entry(db)  # login, grants and gate before ANY private read of this batch

    def write(outcome):
        facts['outcome'] = outcome
        db.execute(text(f'SET LOCAL lock_timeout = {int(get_settings().recovery.gate_lock_timeout_ms)}'))
        ref, link = ReferenceEntry, ReferenceCitationLink
        # 1. Hint only: pending references after the pass keyset, open items, ID order.
        stmt = (select(ref.id, ref.item_id, LibraryItem.visibility)
                .join(LibraryItem, LibraryItem.id == ref.item_id)
                .outerjoin(link, link.reference_id == ref.id)
                .where(pending_references_clause(), not_revoked_reference_clause()))
        if after is not None:
            stmt = stmt.where(ref.id > after)
        hints = db.execute(stmt.order_by(ref.id).limit(policy.reconcile_batch_size)).all()
        if not hints:
            return Batch(after, 0)
        skipped: dict[str, int] = {}

        def skip(code):
            skipped[code] = skipped.get(code, 0) + 1
        # 2. Common items first (UUID order, never waiting): serializes with revoke/cleanup/extraction.
        common = {h.item_id for h in hints if h.visibility == 'common'}
        locked = _key_share(db, common, skip=True)
        closed = set(db.scalars(select(LibraryItem.id).where(
            LibraryItem.id.in_(sorted(locked)), closed_item_clause(LibraryItem.id)))) if locked else set()
        allowed = {h.id: h.item_id for h in hints
                   if h.visibility == 'private' or (h.item_id in locked and h.item_id not in closed)}
        for h in hints:
            if h.id not in allowed:
                skip('skipped:busy' if h.item_id not in locked else 'skipped:revoked')
        # 3. Reference rows (ID order, never waiting), reloaded under lock with their exact item.
        rows = db.execute(select(ref.id, ref.item_id, ref.version_id, ref.source_file_id, ref.raw_citation)
                          .where(ref.id.in_(sorted(allowed))).order_by(ref.id)
                          .with_for_update(read=True, skip_locked=True)).all() if allowed else []
        rows = [r for r in rows if allowed.get(r.id) == r.item_id]
        missing = set(allowed) - {r.id for r in rows}
        present = set(db.scalars(select(ref.id).where(ref.id.in_(sorted(missing))))) if missing else set()
        for rid in missing:
            skip('skipped:busy' if rid in present else 'skipped:removed')
        if not rows:
            return Batch(hints[-1].id, len(hints), skipped=tuple(sorted(skipped.items())))
        # 4. Citation lock, then discovery state and the link writes. A lock timeout here
        #    (discovery re-target, concurrent insert of the same reference) is busy, not a fault.
        try:
            _lock(db, exclusive=False)
            at = _now()
            expected, written = _upsert(db, rows, at)
            db.flush()
            db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        except DBAPIError as exc:
            if getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '55P03':
                raise runtime.Halt(BUSY) from None
            raise
        # 5. Journal exclusive (writer), then the exact scalar binding of what this batch decided.
        try:
            auth_safety.require_final(db, writer=True)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
        state = _binding(db, sorted(expected))
        for row in rows:
            current, values = state.get(row.id), expected[row.id]
            if (current is None or current.item_id != row.item_id or current.closed
                    or current.current != values['raw_citation_sha256']
                    or tuple(getattr(current, k) for k in LINK_FIELDS) != tuple(values[k] for k in LINK_FIELDS)):
                raise runtime.Halt('citation_binding_changed')
        if _now() < at:  # read after the LAST SQL predicate and the journal wait
            raise runtime.Halt('citation_clock_invalid')
        runtime.commit(db, outcome)
        return Batch(hints[-1].id, len(hints), len(rows), len(written), tuple(sorted(skipped.items())))

    return runtime.short(db, write)


def reconcile_pending(engine: Engine, policy: CitationPolicy) -> dict[str, int]:
    """One bounded backfill pass through the runtime (CLI, Celery and direct callers).

    Every batch: entry (actual runtime login, grants, writer gate) in its own
    transaction, then one short batch transaction ending in the journal
    exclusive lock, exact rebinding, a fresh clock and a known commit. Counts:
    ``batches``/``references``/``links_written`` only from known commits with a
    clean release (``links_written`` = rows inserted or changed; an identical
    row is not counted); ``skipped:busy|revoked|removed`` are left for a later
    pass (the in-memory keyset moves past them); ``bound:batches`` when
    ``reconcile_max_batches`` ran out on full batches; ``halted:<code>`` stops
    the pass without inference or retry, and ``committed_unreleased`` marks a
    known commit followed by a release fault (its rows stay; not counted above).
    ``busy:citation_lock`` stops the pass on a lock timeout after the citation
    lock (rolled back, nothing of that batch counted); it is not a fault.
    """
    if not policy.enabled:
        return {'skipped:disabled': 1}  # no query at all
    counts: dict[str, int] = {}

    def add(key, n=1):
        if n:
            counts[key] = counts.get(key, 0) + n
    after = None
    try:
        for _ in range(policy.reconcile_max_batches):
            facts: dict = {}
            try:
                batch = _batch(engine, policy, after, facts)
            except runtime.Halt:
                outcome = facts.get('outcome')
                if outcome is not None and outcome.committed:
                    add('committed_unreleased')
                raise
            if batch.selected == 0:
                break
            if batch.references:
                add('batches')
                add('references', batch.references)
                add('links_written', batch.written)
            for code, n in batch.skipped:
                add(code, n)
            after = batch.last
            if batch.selected < policy.reconcile_batch_size:
                break
        else:
            add('bound:batches')
    except runtime.Halt as halt:
        add('busy:citation_lock' if halt.code == BUSY else 'halted:' + halt.code)
    return counts
