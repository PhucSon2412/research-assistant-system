"""Bounded OAI-PMH harvesting. PostgreSQL owns every page checkpoint.

Only descriptive metadata is written. This module deliberately does not import
LibraryItem, SourceFile, indexer, search, RAG, or a provider client.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import DiscoveryPolicy
from app.models.discovery import DiscoveryPaper, DiscoveryPaperTopic, DiscoveryRequestClock, DiscoverySource

OAI_URL = "https://oaipmh.arxiv.org/oai"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
RAW = "{http://arxiv.org/OAI/arXivRaw/}"
ID_RE = re.compile(r"(?:[a-z-]+(?:\.[A-Z][A-Z0-9-]*)?/\d{7}|\d{4}\.\d{4,5})")
VERSION_RE = re.compile(r"v[1-9][0-9]*")
# Shared across task invocations; PostgreSQL session lock survives page commits.
SYNC_LOCK = 0x5A17A51


class DiscoveryError(Exception):
    pass


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


def parse_page(data: bytes, max_bytes: int) -> Page:
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
            return Page([], None)
        raise DiscoveryError("oai_" + str(next(iter(codes))))
    listing = root.find(OAI + "ListRecords")
    if listing is None:
        raise DiscoveryError("missing_list_records")
    papers: list[Paper] = []
    for record in listing.findall(OAI + "record"):
        header = record.find(OAI + "header")
        if header is None:
            raise DiscoveryError("missing_header")
        if header.get("status") == "deleted":
            continue
        identifier = _value(header, OAI + "identifier")
        datestamp = _value(header, OAI + "datestamp")
        raw = record.find(OAI + "metadata/" + RAW + "arXivRaw")
        remote_id = _value(raw, RAW + "id") if raw is not None else None
        title = _value(raw, RAW + "title") if raw is not None else None
        if not remote_id or not ID_RE.fullmatch(remote_id) or identifier != "oai:arXiv.org:" + remote_id or not title or not datestamp:
            raise DiscoveryError("invalid_record_identity")
        try:
            stamp = date.fromisoformat(datestamp[:10])
        except ValueError as exc:
            raise DiscoveryError("invalid_datestamp") from exc
        versions = raw.findall(RAW + "version")
        valid_versions = [(int(v.get("version")[1:]), v) for v in versions if v.get("version") and VERSION_RE.fullmatch(v.get("version"))]
        valid_versions.sort(key=lambda pair: pair[0])
        first_date = _version_date(_value(valid_versions[0][1], RAW + "date")) if valid_versions else None
        last_date = _version_date(_value(valid_versions[-1][1], RAW + "date")) if valid_versions else None
        latest = "v" + str(valid_versions[-1][0]) if valid_versions else None
        license_url = _value(raw, RAW + "license")
        # License is provenance only, never an automatic rights grant.
        if license_url and not license_url.startswith(("https://arxiv.org/", "http://arxiv.org/")):
            license_url = None
        papers.append(Paper(remote_id, title[:1000], _value(raw, RAW + "authors"),
                            _value(raw, RAW + "abstract"), _value(raw, RAW + "categories"),
                            license_url, latest, first_date, last_date, stamp))
    token = _value(listing, OAI + "resumptionToken")
    if token and len(token) > 4096:
        raise DiscoveryError("token_too_large")
    return Page(papers, token)


def fetch_page(params: dict[str, str], policy: DiscoveryPolicy) -> bytes:
    # A fixed HTTPS endpoint, redirects disabled, streamed byte cap, and timeout
    # prevent source-controlled URLs or response bodies from reaching internal hosts.
    try:
        with httpx.Client(timeout=policy.timeout_seconds, follow_redirects=False,
                          headers={"User-Agent": policy.user_agent, "Accept": "application/xml"}) as client:
            with client.stream("GET", OAI_URL, params=params) as response:
                if response.status_code != 200:
                    raise DiscoveryError(f"http_{response.status_code}")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > policy.max_response_bytes:
                        raise DiscoveryError("response_too_large")
                return bytes(data)
    except httpx.HTTPError as exc:
        raise DiscoveryError("transport_error") from exc


def _ensure_sources(db: Session, policy: DiscoveryPolicy, now: datetime) -> None:
    for topic in policy.topics:
        db.execute(insert(DiscoverySource).values(topic=topic, window_from=now.date() - timedelta(days=policy.initial_lookback_days))
                   .on_conflict_do_nothing(index_elements=["topic"]))
    db.commit()


def _reserve_request(db: Session, policy: DiscoveryPolicy) -> None:
    clock = db.scalar(select(DiscoveryRequestClock).where(DiscoveryRequestClock.id == 1).with_for_update())
    if clock is None:
        raise DiscoveryError("missing_request_clock")
    now = datetime.now(UTC)
    if clock.next_request_at and clock.next_request_at > now:
        time.sleep((clock.next_request_at - now).total_seconds())
    clock.next_request_at = datetime.now(UTC) + timedelta(seconds=policy.min_request_interval_seconds)
    db.commit()


def _write_page(db: Session, topic: str, page: Page, now: datetime) -> None:
    for paper in page.papers:
        values = dict(source="arxiv", remote_id=paper.remote_id,
                      original_url=f"https://arxiv.org/abs/{paper.remote_id}", title=paper.title,
                      authors=paper.authors, abstract=paper.abstract, categories=paper.categories,
                      license_url=paper.license_url, latest_version=paper.latest_version,
                      source_published_at=paper.published, source_updated_at=paper.updated,
                      oai_datestamp=paper.datestamp, first_discovered_at=now, last_seen_at=now,
                      content_status="metadata_only")
        stmt = insert(DiscoveryPaper).values(**values)
        paper_id = db.scalar(stmt.on_conflict_do_update(
            constraint="uq_discovery_papers_source_remote_id",
            set_={k: v for k, v in values.items() if k not in ("source", "remote_id", "first_discovered_at", "content_status")},
            where=DiscoveryPaper.oai_datestamp <= paper.datestamp).returning(DiscoveryPaper.id))
        if paper_id is None:
            paper_id = db.scalar(select(DiscoveryPaper.id).where(DiscoveryPaper.source == "arxiv", DiscoveryPaper.remote_id == paper.remote_id))
        db.execute(insert(DiscoveryPaperTopic).values(paper_id=paper_id, topic=topic)
                   .on_conflict_do_nothing(index_elements=["paper_id", "topic"]))


def run_sync(engine: Engine, policy: DiscoveryPolicy, *, fetcher: Callable[[dict[str, str], DiscoveryPolicy], bytes] = fetch_page,
             now: datetime | None = None, force: bool = False) -> dict[str, int | bool]:
    """One bounded pass. A DB advisory lock serializes workers across topics."""
    if not policy.enabled:
        return {"busy": False, "pages": 0, "records": 0}
    now = now or datetime.now(UTC)
    with engine.connect() as lock_conn:
        if not lock_conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": SYNC_LOCK}):
            return {"busy": True, "pages": 0, "records": 0}
        lock_conn.commit()
        pages = records = 0
        try:
            with Session(engine, expire_on_commit=False) as db:
                _ensure_sources(db, policy, now)
                for topic in policy.topics:
                    for _ in range(policy.max_pages_per_topic):
                        source = db.get(DiscoverySource, topic)
                        if not force and source.next_attempt_at and source.next_attempt_at > now:
                            break
                        if source.window_until is None:
                            source.window_until = now.date()
                        source.state = "running"
                        source.last_attempt_at = now
                        db.commit()
                        params = {"verb": "ListRecords", "resumptionToken": source.resumption_token} if source.resumption_token else {
                            "verb": "ListRecords", "metadataPrefix": "arXivRaw",
                            "set": ":".join((topic.split(".", 1)[0], topic.split(".", 1)[0], topic.split(".", 1)[1])),
                            "from": source.window_from.isoformat(), "until": source.window_until.isoformat()}
                        try:
                            _reserve_request(db, policy)
                            page = parse_page(fetcher(params, policy), policy.max_response_bytes)
                            source = db.get(DiscoverySource, topic)
                            _write_page(db, topic, page, now)
                            source.pages_seen += 1
                            source.records_seen += len(page.papers)
                            source.resumption_token = page.token
                            source.state = "idle"
                            source.consecutive_errors = 0
                            source.last_error = None
                            if page.token is None:
                                source.window_from = source.window_until  # inclusive overlap
                                source.window_until = None
                                source.last_success_at = now
                                source.next_attempt_at = now + timedelta(seconds=policy.poll_seconds)
                            else:
                                source.next_attempt_at = None
                            db.commit()  # paper, topics, token and counters are atomic
                            pages += 1
                            records += len(page.papers)
                            if page.token is None:
                                break
                        except ExpiredToken:
                            db.rollback()
                            source = db.get(DiscoverySource, topic)
                            source.resumption_token = None  # restart same inclusive window
                            source.state = "error"
                            source.last_error = "badResumptionToken"
                            source.next_attempt_at = now + timedelta(seconds=policy.retry_base_seconds)
                            db.commit()
                            break
                        except Exception as exc:
                            db.rollback()
                            source = db.get(DiscoverySource, topic)
                            source.state = "error"
                            source.consecutive_errors += 1
                            source.last_error = str(exc)[:200] if isinstance(exc, DiscoveryError) else type(exc).__name__
                            delay = min(policy.retry_max_seconds, policy.retry_base_seconds * 2 ** min(source.consecutive_errors - 1, 12))
                            source.next_attempt_at = now + timedelta(seconds=delay)
                            db.commit()
                            break
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SYNC_LOCK})
            lock_conn.commit()
    return {"busy": False, "pages": pages, "records": records}
