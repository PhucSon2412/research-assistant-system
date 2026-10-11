"""Bounded OAI-PMH harvesting. PostgreSQL owns every page checkpoint.

Only descriptive metadata is written. This module deliberately does not import
LibraryItem, SourceFile, indexer, search, RAG, or a provider client. Each page
re-targets existing exact citation links (M5b-1) in the same transaction.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from sqlalchemy import select, text, or_, cast, Integer, func
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import auth_safety
from app.ingest import runtime
from app.citations import reconcile_arxiv_ids, RetargetBusy, RetargetLimit, NEEDED as CITATION_GRANTS
from app.config import DiscoveryPolicy, get_settings
from app.models.discovery import (DiscoveryDeletedRecord, DiscoveryPaper, DiscoveryPaperLicense,
                                  DiscoveryPaperTopic, DiscoveryRequestClock, DiscoverySource)

OAI_URL = "https://oaipmh.arxiv.org/oai"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
RAW = "{http://arxiv.org/OAI/arXivRaw/}"
ID_RE = re.compile(r"(?:[a-z-]+(?:\.[A-Z][A-Z0-9-]*)?/\d{7}|\d{4}\.\d{4,5})")
VERSION_RE = re.compile(r"v[1-9][0-9]*")


class DiscoveryError(Exception):
    def __init__(self, code: str, retry_after: float = 0):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class ExpiredToken(DiscoveryError):
    pass


@dataclass(frozen=True)
class Paper:
    remote_id: str
    title: str
    authors: str | None
    abstract: str | None
    categories: str | None
    license_url: str | None
    latest_version: str | None
    published: datetime | None
    updated: datetime | None
    datestamp: date


@dataclass(frozen=True)
class Page:
    papers: list[Paper]
    deleted: list[tuple[str, date]]
    token: str | None


def _value(parent, tag: str) -> str | None:
    node = parent.find(tag)
    return node.text.strip() if node is not None and node.text and node.text.strip() else None


def _version_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = parsedate_to_datetime(value)
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _license_url(value: str | None) -> str | None:
    """Keep an absolute HTTP(S) metadata URL, without inferring any rights."""
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port  # reject malformed port syntax
    except ValueError:
        return None
    return value


def _identity(header) -> tuple[str, date]:
    identifier = _value(header, OAI + "identifier")
    datestamp = _value(header, OAI + "datestamp")
    if not identifier or not identifier.startswith("oai:arXiv.org:") or not datestamp:
        raise DiscoveryError("invalid_record_identity")
    remote_id = identifier.removeprefix("oai:arXiv.org:")
    if not ID_RE.fullmatch(remote_id) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", datestamp):
        raise DiscoveryError("invalid_record_identity")
    try:
        return remote_id, date.fromisoformat(datestamp)
    except ValueError as exc:
        raise DiscoveryError("invalid_datestamp") from exc


def parse_page(data: bytes, max_bytes: int, max_records: int = 5000) -> Page:
    if len(data) > max_bytes:
        raise DiscoveryError("response_too_large")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DiscoveryError("invalid_xml") from exc
    except (ValueError, DefusedXmlException) as exc:
        raise DiscoveryError("unsafe_xml") from exc
    if root.tag != OAI + "OAI-PMH":
        raise DiscoveryError("invalid_oai_root")
    errors = root.findall(OAI + "error")
    if errors:
        codes = {e.get("code") for e in errors}
        if codes == {"badResumptionToken"}:
            raise ExpiredToken("badResumptionToken")
        if codes == {"noRecordsMatch"}:
            return Page([], [], None)
        allowed = {'badArgument', 'badVerb', 'cannotDisseminateFormat', 'noSetHierarchy'}
        code = next(iter(codes)) if len(codes) == 1 else None
        raise DiscoveryError("oai_" + code if code in allowed else "oai_error")
    listing = root.find(OAI + "ListRecords")
    if listing is None:
        raise DiscoveryError("missing_list_records")
    papers: list[Paper] = []
    deleted: list[tuple[str, date]] = []
    records = listing.findall(OAI + "record")
    if len(records) > max_records:
        raise DiscoveryError("record_limit_exceeded")
    for record in records:
        header = record.find(OAI + "header")
        if header is None:
            raise DiscoveryError("missing_header")
        remote_id, stamp = _identity(header)
        if header.get("status") == "deleted":
            deleted.append((remote_id, stamp))
            continue
        if header.get("status") is not None:
            raise DiscoveryError("invalid_record_status")
        raw = record.find(OAI + "metadata/" + RAW + "arXivRaw")
        metadata_id = _value(raw, RAW + "id") if raw is not None else None
        title = _value(raw, RAW + "title") if raw is not None else None
        if metadata_id != remote_id or not title:
            raise DiscoveryError("invalid_record_identity")
        versions = raw.findall(RAW + "version")
        valid_versions = [(int(v.get("version")[1:]), v) for v in versions if v.get("version") and VERSION_RE.fullmatch(v.get("version"))]
        valid_versions.sort(key=lambda pair: pair[0])
        first_date = _version_date(_value(valid_versions[0][1], RAW + "date")) if valid_versions else None
        last_date = _version_date(_value(valid_versions[-1][1], RAW + "date")) if valid_versions else None
        latest = "v" + str(valid_versions[-1][0]) if valid_versions else None
        # arXivRaw's single license is metadata of its current/latest version.
        # Keep it as provenance; rights review of a real file is separate.
        license_url = _license_url(_value(raw, RAW + "license"))
        papers.append(Paper(remote_id, title[:1000], _value(raw, RAW + "authors"),
                            _value(raw, RAW + "abstract"), _value(raw, RAW + "categories"),
                            license_url, latest, first_date, last_date, stamp))
    token = _value(listing, OAI + "resumptionToken")
    if token and len(token) > 4096:
        raise DiscoveryError("token_too_large")
    return Page(papers, deleted, token)


def retry_after(value: str | None, policy: DiscoveryPolicy) -> float:
    if not value:
        return 0
    try:
        seconds = int(value) if value.strip().isdigit() else (
            parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        return max(0, min(seconds, policy.retry_after_max_seconds))
    except (TypeError, ValueError, OverflowError):
        return 0


async def _fetch(params: dict[str, str], policy: DiscoveryPolicy) -> bytes:
    # A fixed HTTPS endpoint, redirects disabled, streamed byte cap, and timeout
    # prevent source-controlled URLs or response bodies from reaching internal hosts.
    try:
        async with httpx.AsyncClient(timeout=policy.timeout_seconds, follow_redirects=False,
                          headers={"User-Agent": policy.user_agent, "Accept": "application/xml", "Accept-Encoding": "identity"}) as client:
            async with client.stream("GET", OAI_URL, params=params) as response:
                if response.status_code != 200:
                    code = f"http_{response.status_code}" if response.status_code in {406, 429, 503} else "http_error"
                    delay = retry_after(response.headers.get('Retry-After'), policy) if response.status_code in {429, 503} else 0
                    raise DiscoveryError(code, delay)
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise DiscoveryError('unsupported_encoding')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > policy.max_response_bytes:
                        raise DiscoveryError("response_too_large")
                return bytes(data)
    except httpx.TimeoutException as exc:
        raise DiscoveryError("http_timeout") from exc
    except httpx.HTTPError as exc:
        raise DiscoveryError("transport_error") from exc


def fetch_page(params: dict[str, str], policy: DiscoveryPolicy) -> bytes:
    async def bounded():
        try:
            return await asyncio.wait_for(_fetch(params, policy), policy.http_deadline_seconds)
        except TimeoutError:
            raise DiscoveryError('http_deadline') from None
    # Cancellation settles the async stream/client before returning; no detached thread.
    return asyncio.run(bounded())


def _write_page(db: Session, topic: str, page: Page, now: datetime, *, retarget_limit: int | None = None) -> None:
    retarget_limit = retarget_limit if retarget_limit is not None else get_settings().discovery.retarget_limit
    for remote_id, stamp in page.deleted:
        db.execute(insert(DiscoveryDeletedRecord).values(source="arxiv", remote_id=remote_id,
                   oai_datestamp=stamp).on_conflict_do_update(
                       index_elements=["source", "remote_id"],
                       set_={"oai_datestamp": stamp},
                       where=DiscoveryDeletedRecord.oai_datestamp <= stamp))
    for paper in page.papers:
        tombstone = db.get(DiscoveryDeletedRecord, ("arxiv", paper.remote_id))
        if tombstone is not None and tombstone.oai_datestamp >= paper.datestamp:
            continue
        values = dict(source="arxiv", remote_id=paper.remote_id,
                      original_url=f"https://arxiv.org/abs/{paper.remote_id}", title=paper.title,
                      authors=paper.authors, abstract=paper.abstract, categories=paper.categories,
                      license_url=paper.license_url, latest_version=paper.latest_version,
                      source_published_at=paper.published, source_updated_at=paper.updated,
                      oai_datestamp=paper.datestamp, first_discovered_at=now, last_seen_at=now,
                      content_status="metadata_only")
        stmt = insert(DiscoveryPaper).values(**values)
        version_ok = (DiscoveryPaper.latest_version.is_(None) if paper.latest_version is None else or_(
            DiscoveryPaper.latest_version.is_(None),
            cast(func.substr(DiscoveryPaper.latest_version, 2), Integer) <= int(paper.latest_version[1:])))
        paper_id = db.scalar(stmt.on_conflict_do_update(
            constraint="uq_discovery_papers_source_remote_id",
            set_={k: v for k, v in values.items() if k not in ("source", "remote_id", "first_discovered_at", "content_status")},
            where=(DiscoveryPaper.oai_datestamp <= paper.datestamp) & version_ok).returning(DiscoveryPaper.id))
        if paper_id is None:
            paper_id = db.scalar(select(DiscoveryPaper.id).where(DiscoveryPaper.source == "arxiv", DiscoveryPaper.remote_id == paper.remote_id))
        if paper.license_url and paper.latest_version:
            db.execute(insert(DiscoveryPaperLicense).values(
                paper_id=paper_id, version_label=paper.latest_version,
                license_url=paper.license_url, oai_datestamp=paper.datestamp,
                first_observed_at=now, last_observed_at=now).on_conflict_do_update(
                    index_elements=["paper_id", "version_label"],
                    set_={"license_url": paper.license_url, "oai_datestamp": paper.datestamp,
                          "last_observed_at": now},
                    where=DiscoveryPaperLicense.oai_datestamp <= paper.datestamp))
        db.execute(insert(DiscoveryPaperTopic).values(paper_id=paper_id, topic=topic)
                   .on_conflict_do_nothing(index_elements=["paper_id", "topic"]))
    reconcile_arxiv_ids(db, [p.remote_id for p in page.papers] + [r for r, _ in page.deleted], now,
                        limit=retarget_limit)


NEEDED = tuple(dict.fromkeys(CITATION_GRANTS + tuple(
    (t, p) for t in ('discovery_sources', 'discovery_papers', 'discovery_paper_topics',
                    'discovery_paper_licenses', 'discovery_deleted_records')
    for p in ('SELECT', 'INSERT', 'UPDATE')) + (
        ('discovery_request_clock', 'SELECT'), ('discovery_request_clock', 'UPDATE'))))
GRANTS = text('SELECT ' + ' AND '.join(f"has_table_privilege('{t}','{p}')" for t, p in NEEDED))


@dataclass(frozen=True)
class Cursor:
    topic: str
    start: date
    until: date | None
    token: str | None

    def params(self):
        if self.token:
            return {'verb': 'ListRecords', 'resumptionToken': self.token}
        archive, category = self.topic.split('.', 1)
        return {'verb': 'ListRecords', 'metadataPrefix': 'arXivRaw',
                'set': f'{archive}:{archive}:{category}',
                'from': self.start.isoformat(), 'until': self.until.isoformat()}


def _cursor(source):
    return Cursor(source.topic, source.window_from, source.window_until, source.resumption_token)


def _now(db):
    value = db.scalar(text('SELECT clock_timestamp()'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise runtime.Halt('discovery_clock_invalid')
    return value


def _short(db, operation, facts):
    """Preserve outcome even when rollback/close fails. Never infer a commit by rereading."""
    outcome = runtime.Outcome()
    facts['outcome'] = outcome
    try:
        result = operation(outcome)
    except BaseException as exc:
        try:
            runtime.release(db)
        except runtime.Halt:
            facts['release_failed'] = True
            if not outcome.commit_started:
                raise runtime.Halt('discovery_release_unavailable') from None
        if outcome.commit_started:
            raise runtime.Halt('discovery_commit_uncertain') from None
        if isinstance(exc, (runtime.Halt, DiscoveryError, RetargetBusy, RetargetLimit)):
            raise
        if isinstance(exc, DBAPIError) and getattr(exc.orig, 'sqlstate', None) == '55P03':
            raise runtime.Halt('discovery_lock_busy') from None
        raise runtime.Halt('discovery_checkpoint_unavailable') from None
    try:
        runtime.release(db)
    except runtime.Halt:
        facts['release_failed'] = True
        raise runtime.Halt('discovery_release_unavailable') from None
    return result


def _checkpoint(engine, policy, nonce, operation, facts, *, expected: Cursor | None = None, claim=False, topic=None):
    db = Session(engine, expire_on_commit=False)

    def entry(_):
        runtime.require_runtime(db)
        if db.scalar(GRANTS) is not True:
            raise runtime.Halt('discovery_runtime_required')
        try:
            auth_safety.require_writer(db)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
    _short(db, entry, facts)  # no locks carried from gate integrity into writer

    def write(outcome):
        db.execute(text(f'SET LOCAL lock_timeout = {int(get_settings().recovery.gate_lock_timeout_ms)}'))
        db.execute(text(f'SET LOCAL statement_timeout = {int(policy.lease_seconds * 1000)}'))
        clock = db.scalar(select(DiscoveryRequestClock).where(DiscoveryRequestClock.id == 1).with_for_update())
        at = _now(db)
        if clock is None:
            raise runtime.Halt('missing_request_clock')
        previous_until = clock.lease_until
        if claim:
            if clock.lease_token is not None and previous_until > at:
                raise runtime.Halt('discovery_lease_busy')
        elif clock.lease_token != nonce or previous_until is None or previous_until <= at:
            raise runtime.Halt('discovery_lease_lost')
        source = None
        if expected is not None or topic is not None:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.topic == (expected.topic if expected else topic)).with_for_update())
            if source is None or (expected is not None and _cursor(source) != expected):
                raise runtime.Halt('discovery_cursor_changed')
        clock.lease_token = nonce
        clock.lease_until = at + timedelta(seconds=policy.lease_seconds)
        result = operation(db, clock, source, at)
        db.flush()
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        final_cursor = _cursor(source) if source is not None else None
        final_lease = (clock.lease_token, clock.lease_until, clock.next_request_at)
        try:
            auth_safety.require_final(db, writer=True)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
        bound = db.execute(select(DiscoveryRequestClock.lease_token, DiscoveryRequestClock.lease_until,
                                  DiscoveryRequestClock.next_request_at).where(DiscoveryRequestClock.id == 1)).one()
        if tuple(bound) != final_lease:
            raise runtime.Halt('discovery_lease_lost')
        if final_cursor is not None:
            row = db.execute(select(DiscoverySource.topic, DiscoverySource.window_from,
                                    DiscoverySource.window_until, DiscoverySource.resumption_token)
                             .where(DiscoverySource.topic == final_cursor.topic)).one()
            if Cursor(*row) != final_cursor:
                raise runtime.Halt('discovery_cursor_changed')
        fresh = _now(db)  # after last binding SQL and journal wait
        if fresh < at or (not claim and fresh >= previous_until) or fresh >= at + timedelta(seconds=policy.lease_seconds):
            raise runtime.Halt('discovery_lease_lost')
        runtime.commit(db, outcome)
        return result
    return _short(db, write, facts)


def _claim(engine, policy, nonce, facts):
    def claim(db, clock, source, at):
        for topic in policy.topics:
            db.execute(insert(DiscoverySource).values(topic=topic,
                       window_from=at.date() - timedelta(days=policy.initial_lookback_days))
                       .on_conflict_do_nothing(index_elements=['topic']))
    return _checkpoint(engine, policy, nonce, claim, facts, claim=True)


def _prepare(engine, policy, nonce, topic, force, facts):
    def prepare(db, clock, source, at):
        # force only skips successful poll scheduling, never an error backoff.
        if source.next_attempt_at and source.next_attempt_at > at and (not force or source.state == 'error'):
            return None
        if source.window_until is None:
            source.window_until = at.date()
        source.state = 'running'
        source.last_attempt_at = at
        return _cursor(source)
    return _checkpoint(engine, policy, nonce, prepare, facts, topic=topic)


def _reserve_request(engine, policy, nonce, cursor, facts):
    def reserve(db, clock, source, at):
        if clock.next_request_at and clock.next_request_at > at:
            return (clock.next_request_at - at).total_seconds()
        # Conservative crash window. On settled HTTP/page/error, shorten to end + interval.
        clock.next_request_at = clock.lease_until + timedelta(seconds=policy.min_request_interval_seconds)
        return 0.0
    return _checkpoint(engine, policy, nonce, reserve, facts, expected=cursor)


def _page_checkpoint(engine, policy, nonce, cursor, page, facts):
    def write(db, clock, source, at):
        _write_page(db, cursor.topic, page, at, retarget_limit=policy.retarget_limit)
        source.pages_seen += 1
        source.records_seen += len(page.papers) + len(page.deleted)
        source.resumption_token = page.token
        source.state = 'idle'
        source.consecutive_errors = 0
        source.last_error = None
        clock.next_request_at = _now(db) + timedelta(seconds=policy.min_request_interval_seconds)
        if page.token is None:
            source.window_from = cursor.until  # inclusive replay
            source.window_until = None
            source.last_success_at = at
            source.next_attempt_at = at + timedelta(seconds=policy.poll_seconds)
        else:
            source.next_attempt_at = None
        return _cursor(source)
    return _checkpoint(engine, policy, nonce, write, facts, expected=cursor)


ERROR_CODES = {'badResumptionToken', 'invalid_record_identity', 'invalid_datestamp', 'response_too_large',
               'invalid_xml', 'unsafe_xml', 'invalid_oai_root', 'oai_error', 'missing_list_records',
               'missing_header', 'invalid_record_status', 'token_too_large', 'record_limit_exceeded',
               'http_406', 'http_429', 'http_503', 'http_error', 'transport_error', 'http_timeout', 'http_deadline',
               'retarget_limit_exceeded', 'citation_retarget_busy', 'fetch_error', 'unsupported_encoding'}
ERROR_CODES |= {'oai_' + c for c in ('badArgument', 'badVerb', 'cannotDisseminateFormat', 'noSetHierarchy')}


def _error_checkpoint(engine, policy, nonce, cursor, error, facts):
    code = ('retarget_limit_exceeded' if isinstance(error, RetargetLimit) else
            'citation_retarget_busy' if isinstance(error, RetargetBusy) else error.code)
    if code not in ERROR_CODES:
        code = 'fetch_error'
    def write(db, clock, source, at):
        source.state = 'error'
        source.last_error = code
        source.consecutive_errors += 1
        delay = min(policy.retry_max_seconds, policy.retry_base_seconds * 2 ** min(source.consecutive_errors - 1, 12))
        retry_delay = min(error.retry_after, policy.retry_after_max_seconds) if isinstance(error, DiscoveryError) else 0
        delay = max(delay, retry_delay)
        source.next_attempt_at = at + timedelta(seconds=delay)
        if isinstance(error, ExpiredToken):
            source.resumption_token = None
        # Retry-After is source-wide, so a different topic cannot bypass it.
        clock.next_request_at = at + timedelta(seconds=max(policy.min_request_interval_seconds, retry_delay))
    _checkpoint(engine, policy, nonce, write, facts, expected=cursor)
    return code


def _release_lease(engine, policy, nonce, facts):
    def release(db, clock, source, at):
        clock.lease_token = clock.lease_until = None
    _checkpoint(engine, policy, nonce, release, facts)


def run_sync(engine: Engine, policy: DiscoveryPolicy, *, fetcher=None,
             now: datetime | None = None, force: bool = False) -> dict:
    """One finite runtime pass. ``now`` retained for caller compatibility, never trusted.

    HTTP/parse/sleep own no DB connection. Global nonce lease and exact input
    cursor bind every checkpoint. Counts include only known commits + clean
    Session release. A halted pass leaves its lease to expire; no speculative
    cleanup or error write after commit/release uncertainty.
    """
    counts = {'busy': False, 'pages': 0, 'records': 0, 'requests': 0}
    if not policy.enabled:
        return {**counts, 'skipped:disabled': 1}
    fetcher = fetcher or fetch_page
    nonce, started = uuid.uuid4(), time.monotonic()
    facts = {}
    try:
        _claim(engine, policy, nonce, facts)
        for topic in policy.topics:
            if time.monotonic() - started >= policy.max_pass_seconds:
                counts['bound:time'] = 1
                _release_lease(engine, policy, nonce, facts)
                return counts
            cursor = _prepare(engine, policy, nonce, topic, force, facts)
            if cursor is None:
                continue
            for _ in range(policy.max_pages_per_topic):
                while True:
                    if time.monotonic() - started >= policy.max_pass_seconds:
                        counts['bound:time'] = 1
                        _release_lease(engine, policy, nonce, facts)
                        return counts
                    admission = time.monotonic()
                    wait = _reserve_request(engine, policy, nonce, cursor, facts)
                    if not wait:
                        if time.monotonic() - admission >= policy.lease_seconds - policy.http_deadline_seconds - policy.lease_margin_seconds:
                            raise runtime.Halt('discovery_admission_expired')
                        break
                    # Wake, recheck gate/binding and reserve with a fresh PG clock.
                    time.sleep(min(wait, policy.lease_seconds / 3, max(.001, policy.max_pass_seconds - (time.monotonic() - started))))
                try:
                    phase = time.monotonic()
                    counts['requests'] += 1  # attempted calls, distinct from accepted records
                    page = parse_page(fetcher(cursor.params(), policy), policy.max_response_bytes,
                                      policy.max_records_per_page)
                    if time.monotonic() - phase >= policy.http_deadline_seconds:
                        raise DiscoveryError('http_deadline')
                    cursor = _page_checkpoint(engine, policy, nonce, cursor, page, facts)
                except (DiscoveryError, RetargetBusy, RetargetLimit) as error:
                    # These errors are emitted only before commit and after clean Session release.
                    code = _error_checkpoint(engine, policy, nonce, cursor, error, facts)
                    counts['error:' + code] = counts.get('error:' + code, 0) + 1
                    break
                except runtime.Halt:
                    raise
                except Exception:
                    code = _error_checkpoint(engine, policy, nonce, cursor, DiscoveryError('fetch_error'), facts)
                    counts['error:' + code] = counts.get('error:' + code, 0) + 1
                    break
                counts['pages'] += 1
                counts['records'] += len(page.papers) + len(page.deleted)
                if page.token is None:
                    break
            else:
                counts['bound:pages'] = counts.get('bound:pages', 0) + 1
        _release_lease(engine, policy, nonce, facts)
    except runtime.Halt as halt:
        if halt.code in {'discovery_lease_busy', 'discovery_lock_busy'}:
            counts['busy'] = True
            counts['busy:' + halt.code] = 1
        else:
            counts['halted:' + halt.code] = 1
        outcome = facts.get('outcome')
        if outcome is not None and outcome.committed:
            counts['committed_unreleased'] = 1
    return counts
