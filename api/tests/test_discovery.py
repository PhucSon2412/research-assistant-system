from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from pydantic import ValidationError
from sqlalchemy import text

from app.config import DiscoveryPolicy
from app.discovery import DiscoveryError, parse_page, run_sync
from tests.resource_fixtures import resource_api, resource_world, installation

PAGE_ONE = b'''<?xml version="1.0"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
 <ListRecords><record><header><identifier>oai:arXiv.org:2609.00001</identifier><datestamp>2026-09-28</datestamp><setSpec>cs:cs:AI</setSpec></header>
 <metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>2609.00001</id>
 <version version="v1"><date>Mon, 28 Sep 2026 10:00:00 GMT</date></version>
 <title>Discovery Alpha</title><authors>A. Author</authors><categories>cs.AI cs.LG</categories>
 <abstract>Never send this abstract to a provider.</abstract><license>http://arxiv.org/licenses/nonexclusive-distrib/1.0/</license>
 </arXivRaw></metadata></record><resumptionToken>page-two</resumptionToken></ListRecords></OAI-PMH>'''
PAGE_TWO = b'''<?xml version="1.0"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
 <ListRecords><record><header><identifier>oai:arXiv.org:2609.00002</identifier><datestamp>2026-09-28</datestamp></header>
 <metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>2609.00002</id><title>Discovery Beta</title>
 <authors>B. Author</authors><categories>cs.AI</categories></arXivRaw></metadata></record><resumptionToken />
 </ListRecords></OAI-PMH>'''
ERROR_TOKEN = b'''<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badResumptionToken">expired</error></OAI-PMH>'''
PAGE_DELETED = b'''<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>
 <record><header status="deleted"><identifier>oai:arXiv.org:2609.00001</identifier>
 <datestamp>2026-09-29</datestamp></header></record><resumptionToken /></ListRecords></OAI-PMH>'''
CC = b"https://creativecommons.org/licenses/by/4.0/"
ARXIV_LICENSE = b"http://arxiv.org/licenses/nonexclusive-distrib/1.0/"


def policy(**kwargs) -> DiscoveryPolicy:
    return DiscoveryPolicy(topics=["cs.AI", "cs.LG"], max_pages_per_topic=2, **kwargs)


def test_discovery_get_runtime_metadata_contract(resource_api,monkeypatch):
    """Read contract from checkpoint test; no search/RAG/writer HTTP bypass."""
    h=resource_api
    result=run_sync(h.world.runtime,policy(),fetcher=lambda params,_: (
        PAGE_TWO if params.get("resumptionToken") else PAGE_ONE),now=h.clock.now)
    assert result["pages"]==4 and h.scalar("SELECT count(*) FROM discovery_papers")==2
    h.create_user("a@example.org"); h.create_user("b@example.org")
    for browser in (h.user_browser("a@example.org"),h.user_browser("b@example.org"),h.admin_browser()):
        assert browser.get("/discovery/topics").json()=={"topics":["cs.AI","cs.LG"]}
        seen=[]
        for offset in range(3):
            r=browser.get(f"/discovery/papers?topic=cs.AI&limit=1&offset={offset}")
            assert r.status_code==200,r.text
            assert r.json()["total"]==2
            seen.extend(i["title"] for i in r.json()["items"])
            assert all(i["content_status"]=="metadata_only" for i in r.json()["items"])
        assert sorted(seen)==["Discovery Alpha","Discovery Beta"]
        assert browser.get("/discovery/papers?topic=unknown").json()["total"]==0
    assert h.user_browser("a@example.org").get("/discovery/sources").status_code==403
    status=h.admin_browser().get("/discovery/sources")
    assert status.status_code==200 and len(status.json()["sources"])==2
    assert all("resumption_token" not in row for row in status.json()["sources"])
    assert "page-two" not in status.text
    for table in ("library_items","source_files","chunks","index_entries"):
        assert h.scalar(f"SELECT count(*) FROM {table}")==0


def test_parser_safety_and_missing_fields():
    page = parse_page(PAGE_ONE, 100_000)
    assert page.token == "page-two"
    assert page.papers[0].latest_version == "v1"
    assert page.papers[0].license_url == ARXIV_LICENSE.decode()
    assert page.papers[0].published == datetime(2026, 9, 28, 10, tzinfo=UTC)
    assert parse_page(PAGE_TWO, 100_000).papers[0].published is None
    malformed_date = PAGE_ONE.replace(b"Mon, 28 Sep 2026 10:00:00 GMT", b"not-a-date")
    assert parse_page(malformed_date, 100_000).papers[0].published is None
    assert parse_page(PAGE_ONE.replace(ARXIV_LICENSE, CC), 100_000).papers[0].license_url == CC.decode()
    for invalid_license in (b"javascript:alert(1)", b"https://", b"https://user:pass@example.org/license",
                            b"https://bad host/license", b"ftp://creativecommons.org/by/4.0/"):
        assert parse_page(PAGE_ONE.replace(ARXIV_LICENSE, invalid_license), 100_000).papers[0].license_url is None
    assert parse_page(PAGE_ONE.replace(b"<license>" + ARXIV_LICENSE + b"</license>", b""),
                      100_000).papers[0].license_url is None
    for bad in (b'<!DOCTYPE a [<!ENTITY x "evil">]><a>&x;</a>',
                PAGE_ONE.replace(b"2609.00001", b"https://evil.example/"),
                PAGE_ONE.replace(b"<title>Discovery Alpha</title>", b"")):
        with pytest.raises(DiscoveryError):
            parse_page(bad, 100_000)
    with pytest.raises(DiscoveryError, match="response_too_large"):
        parse_page(PAGE_ONE, 10)


def test_discovery_limits_are_validated():
    for invalid in ({"topics": []}, {"topics": ["cs.AI", "cs.AI"]},
                    {"topics": ["https://evil.example"]}, {"min_request_interval_seconds": 2},
                    {"max_pages_per_topic": 0}, {"timeout_seconds": 0}):
        with pytest.raises(ValidationError):
            DiscoveryPolicy(**invalid)


def test_checkpoint_retry_overlap_and_api_acl(resource_api, monkeypatch):
    api = resource_api
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    calls = []
    fail_once = True

    def fetch(params, _policy):
        nonlocal fail_once
        calls.append(dict(params))
        if params.get("set") == "cs:cs:LG":
            return PAGE_ONE.replace(b"<resumptionToken>page-two</resumptionToken>", b"<resumptionToken />")
        if params.get("resumptionToken") == "page-two":
            if fail_once:
                fail_once = False
                raise DiscoveryError("http_503")
            return PAGE_TWO
        return PAGE_ONE

    first = run_sync(api.world.runtime, policy(), fetcher=fetch, now=now)
    assert first["pages"] == 2 and first["records"] == 2 and first["requests"] == 3
    assert api.scalar("SELECT count(*) FROM discovery_papers") == 1
    assert api.scalar("SELECT count(*) FROM discovery_paper_topics") == 2
    with api.engine.connect() as db:
        assert db.scalar(text("SELECT resumption_token FROM discovery_sources WHERE topic='cs.AI'")) == "page-two"
    retry_due(api.engine, errors_only=True)
    second = run_sync(api.world.runtime, policy(), fetcher=fetch, now=now + timedelta(minutes=2))
    assert second["pages"] == 1
    assert calls[-1] == {"verb": "ListRecords", "resumptionToken": "page-two"}
    assert api.scalar("SELECT count(*) FROM discovery_papers") == 2
    third = run_sync(api.world.runtime, policy(), fetcher=fetch, now=now + timedelta(days=1), force=True)
    assert third["pages"] >= 2
    assert api.scalar("SELECT count(*) FROM discovery_papers") == 2
    assert api.scalar("SELECT count(*) FROM library_items") == 0
    assert api.scalar("SELECT count(*) FROM source_files") == 0
    assert api.scalar("SELECT count(*) FROM chunks") == 0
    assert api.scalar("SELECT count(*) FROM index_entries") == 0

    api.bootstrap_admin()
    a_id = api.create_user("a@example.org")
    b_id = api.create_user("b@example.org")
    private_a = api.add_item(owner=a_id, title="private A")
    private_b = api.add_item(owner=b_id, title="private B")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    from app.config import get_settings
    from app.main import app
    settings = get_settings().model_copy(deep=True)
    settings.embedding.api_key = SecretStr("synthetic-test-key")
    settings.generation.pricing_verified = True
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("app.rag.generate", lambda *_: pytest.fail("metadata reached provider"))
    for browser in (a, b, admin):
        response = browser.get("/discovery/papers?topic=cs.AI&limit=1&offset=0")
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["content_status"] == "metadata_only"
        assert browser.get("/discovery/papers?topic=cs.LG").json()["total"] == 1
        assert browser.post("/search", {"query": "Never send", "mode": "keyword"}).json()["count"] == 0
        rag = browser.post("/rag/ask", {"scope": "all", "question": "Never send?", "mode": "keyword"})
        assert rag.status_code == 200
        assert rag.json()["status"] == "insufficient_evidence"
        assert "Never send this abstract" not in str(rag.json())
    assert a.get(f"/library/items/{private_b}").status_code == 404
    assert b.get(f"/library/items/{private_a}").status_code == 404
    assert admin.get(f"/library/items/{private_a}").status_code == 404
    assert a.get("/discovery/sources").status_code == 403
    assert admin.get("/discovery/sources").status_code == 200
    paper_id = admin.get("/discovery/papers").json()["items"][0]["id"]
    assert admin.get(f"/search/chunks/{paper_id}").status_code == 404


def test_expired_token_restarts_window_and_two_workers_do_not_duplicate(resource_world, monkeypatch):
    engine = resource_world.runtime
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    one = DiscoveryPolicy(topics=["cs.AI"], max_pages_per_topic=1)
    assert run_sync(engine, one, fetcher=lambda *_: PAGE_ONE, now=now)["pages"] == 1
    assert run_sync(engine, one, fetcher=lambda *_: ERROR_TOKEN, now=now + timedelta(minutes=1))["pages"] == 0
    params = []
    def replay(p, _):
        params.append(p)
        return PAGE_ONE
    retry_due(resource_world.engine)
    run_sync(engine, one, fetcher=replay, now=now + timedelta(minutes=3))
    assert "resumptionToken" not in params[0]
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM discovery_papers")) == 1

    entered, release = threading.Event(), threading.Event()
    def slow_fetch(*_):
        entered.set()
        assert release.wait(5)
        return PAGE_TWO
    result = []
    thread = threading.Thread(target=lambda: result.append(run_sync(engine, one, fetcher=slow_fetch, now=now + timedelta(days=1), force=True)))
    thread.start()
    assert entered.wait(5)
    assert run_sync(engine, one, fetcher=lambda *_: PAGE_TWO, now=now + timedelta(days=1), force=True)["busy"] is True
    release.set()
    thread.join(5)
    assert result[0]["pages"] == 1
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM discovery_papers")) == 2


def test_metadata_version_update_preserves_discovery_time(resource_world, monkeypatch):
    engine = resource_world.runtime
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    one = DiscoveryPolicy(topics=["cs.AI"], max_pages_per_topic=1)
    first_page = PAGE_ONE.replace(b"<resumptionToken>page-two</resumptionToken>", b"<resumptionToken />")
    first_page = first_page.replace(ARXIV_LICENSE, CC)
    run_sync(engine, one, fetcher=lambda *_: first_page, now=now)
    with engine.connect() as db:
        first_discovered = db.scalar(text("SELECT first_discovered_at FROM discovery_papers"))
    updated = first_page.replace(b"<datestamp>2026-09-28</datestamp>", b"<datestamp>2026-09-29</datestamp>")
    updated = updated.replace(b"<title>Discovery Alpha</title>", b"<version version=\"v2\"><date>Tue, 29 Sep 2026 10:00:00 GMT</date></version><title>Discovery Alpha revised</title>")
    updated = updated.replace(CC, ARXIV_LICENSE)
    run_sync(engine, one, fetcher=lambda *_: updated, now=now + timedelta(days=1), force=True)
    with engine.connect() as db:
        row = db.execute(text("SELECT title, latest_version, first_discovered_at, source_updated_at, content_status FROM discovery_papers")).one()
        assert row.title == "Discovery Alpha revised"
        assert row.latest_version == "v2"
        assert row.first_discovered_at == first_discovered
        assert row.source_updated_at == datetime(2026, 9, 29, 10, tzinfo=UTC)
        assert row.content_status == "metadata_only"
        assert db.scalar(text("SELECT count(*) FROM discovery_papers")) == 1
        assert db.execute(text("SELECT version_label,license_url FROM discovery_paper_licenses ORDER BY version_label")).all() == [
            ("v1", CC.decode()), ("v2", ARXIV_LICENSE.decode())]
        assert db.scalar(text("SELECT count(*) FROM source_files")) == 0
        assert db.scalar(text("SELECT count(*) FROM index_entries")) == 0


@pytest.mark.parametrize("topic", ["cs.CL", "cs.LG"])
def test_http_406_does_not_advance_until_success(resource_world, monkeypatch, topic):
    engine = resource_world.runtime
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    one = DiscoveryPolicy(topics=[topic], max_pages_per_topic=1)

    def rejected(*_):
        raise DiscoveryError("http_406")

    assert run_sync(engine, one, fetcher=rejected, now=now)["pages"] == 0
    with engine.connect() as db:
        row = db.execute(text("SELECT window_from,window_until,resumption_token,last_success_at,pages_seen,records_seen,state,last_error FROM discovery_sources WHERE topic=:t"), {"t": topic}).one()
        assert row.window_from == datetime.now(UTC).date() - timedelta(days=7)
        assert row.window_until == datetime.now(UTC).date()
        assert row.resumption_token is None and row.last_success_at is None
        assert row.pages_seen == 0 and row.records_seen == 0
        assert row.state == "error" and row.last_error == "http_406"
        assert db.scalar(text("SELECT count(*) FROM discovery_papers")) == 0

    retry_due(resource_world.engine)
    assert run_sync(engine, one, fetcher=lambda *_: PAGE_ONE, now=now + timedelta(minutes=2))["pages"] == 1
    with engine.connect() as db:
        row = db.execute(text("SELECT window_from,resumption_token,last_success_at FROM discovery_sources WHERE topic=:t"), {"t": topic}).one()
        assert row.window_from == datetime.now(UTC).date() - timedelta(days=7)
        assert row.resumption_token == "page-two" and row.last_success_at is None
    assert run_sync(engine, one, fetcher=rejected, now=now + timedelta(minutes=3))["pages"] == 0
    with engine.connect() as db:
        row = db.execute(text("SELECT resumption_token,last_success_at,pages_seen,state,last_error FROM discovery_sources WHERE topic=:t"), {"t": topic}).one()
        assert row.resumption_token == "page-two" and row.last_success_at is None
        assert row.pages_seen == 1 and row.state == "error" and row.last_error == "http_406"
    seen_params = []
    def successful_next_page(params, _):
        seen_params.append(params)
        return PAGE_TWO
    retry_due(resource_world.engine)
    assert run_sync(engine, one, fetcher=successful_next_page, now=now + timedelta(minutes=5))["pages"] == 1
    assert seen_params == [{"verb": "ListRecords", "resumptionToken": "page-two"}]
    with engine.connect() as db:
        row = db.execute(text("SELECT window_from,window_until,resumption_token,last_success_at,state FROM discovery_sources WHERE topic=:t"), {"t": topic}).one()
        assert row.window_from == datetime.now(UTC).date() and row.window_until is None
        assert row.resumption_token is None and row.last_success_at is not None and row.state == "idle"


def test_persistent_oai_deleted_record_is_hidden_and_checkpointed(resource_api, monkeypatch):
    api = resource_api
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    one = DiscoveryPolicy(topics=["cs.AI"], max_pages_per_topic=1)
    assert run_sync(api.world.runtime, one, fetcher=lambda *_: PAGE_ONE, now=now)["records"] == 1
    api.bootstrap_admin()
    admin = api.admin_browser()
    assert admin.get("/discovery/papers").json()["total"] == 1
    assert parse_page(PAGE_DELETED, 100_000).deleted == [("2609.00001", now.date())]
    retry_due(api.engine)
    assert run_sync(api.world.runtime, one, fetcher=lambda *_: PAGE_DELETED, now=now + timedelta(minutes=2))["records"] == 1
    assert admin.get("/discovery/papers").json()["total"] == 0
    with api.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM discovery_deleted_records")) == 1
        assert db.scalar(text("SELECT last_success_at FROM discovery_sources WHERE topic='cs.AI'")) is not None
        assert db.scalar(text("SELECT count(*) FROM source_files")) == 0


def retry_due(engine, errors_only=False):
    """Labelled owner setup advances only retry/poll scheduling, never the runtime clock."""
    with engine.begin() as db:
        db.execute(text("UPDATE discovery_sources SET next_attempt_at=NULL" + (" WHERE state='error'" if errors_only else "")))
