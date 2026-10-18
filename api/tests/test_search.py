"""M3b integration against PostgreSQL/pgvector and fake external providers."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
import httpx
from sqlalchemy.orm import Session

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text

from app.config import Settings, get_settings
from app.ingest.embedding import EmbeddingError, EmbeddingResult
from app.ingest.worker import process_job
from app.main import app
from app.search import RerankError, RerankResult, fuse, rerank_order, reserve, rerank, settle
from app.provider_capacity import reap_expired, reservation_heartbeat
from app.models import SearchUsage
from app.authz import Principal
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import partial_pdf, upload_pdf
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_uploads import upload_api
from tests.test_c2c1_bootstrap import installation  # noqa: F401


def configured(settings):
    app.dependency_overrides[get_settings] = lambda: settings


def indexed(api, storage, browser, body, settings):
    ids = upload(browser, body)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    return ids


def test_fusion_and_rerank_mapping():
    a, b, c = (uuid.uuid4() for _ in range(3))
    assert fuse([a, b], [b, c]) == [b, a, c]
    assert rerank_order([a, b], [{"index": 1, "relevance_score": .9},
                                 {"index": 0, "relevance_score": .2}]) == [b, a]
    with pytest.raises(ValueError):
        rerank_order([a, b], [{"index": 0, "relevance_score": .2}])
    with pytest.raises(ValueError):
        rerank_order([a], [{"index": 2, "relevance_score": .2}])


def test_search_acl_before_keyword_vector_topk_and_rerank_payload(upload_api, monkeypatch):
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    settings = settings_with_fake_key()
    settings.search.keyword_top_k = 1
    settings.search.vector_top_k = 1
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    first = indexed(api, storage, a, b"## Methods\nRareword A body.\n## References\n[1] Bibliography secret.\n", settings)
    second = indexed(api, storage, b, b"## Methods\nRareword B secret body.\n", settings)
    api.add_item(owner=None, title="common metadata_only")
    configured(settings)
    sent = []
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type: (
        sent.append(("embed", value, input_type)) or EmbeddingResult([.1, .1, .1], cfg.embedding.model_id, 2)))
    def rerank(query, docs, cfg):
        sent.append(("rerank", query, docs))
        return ([{"index": i, "relevance_score": 1.0 - i / 10} for i in range(len(docs))], 10, None)
    monkeypatch.setattr("app.search.rerank", rerank)
    response = a.post("/search", {"query": "Rareword", "mode": "hybrid", "rerank": True})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "hybrid" and data["rerank_status"] == "applied"
    assert data["count"] == 1
    assert data["hits"][0]["item_id"] == first["item_id"]
    assert data["hits"][0]["source_file_id"] == first["file_id"]
    chunk_id = data["hits"][0]["chunk_id"]
    opened = a.get(f"/search/chunks/{chunk_id}")
    assert opened.status_code == 200 and opened.json()["chunk_id"] == chunk_id
    assert b.get(f"/search/chunks/{chunk_id}").status_code == 404
    assert admin.get(f"/search/chunks/{chunk_id}").status_code == 404
    assert "B secret" not in str(data) and "Bibliography" not in str(data)
    assert sent[0] == ("embed", "Rareword", "search_query")
    assert all("B secret" not in d and "Bibliography" not in d for d in sent[1][2])
    assert b.post("/search", {"query": "Rareword", "mode": "keyword", "rerank": False}).json()["hits"][0]["item_id"] == second["item_id"]
    assert admin.post("/search", {"query": "Rareword", "mode": "keyword", "rerank": False}).json()["count"] == 0
    fake = str(uuid.uuid4())
    for target in (second["item_id"], fake):
        denied = a.post("/search", {"query": "Rareword", "mode": "keyword", "item_id": target})
        assert denied.status_code == 404 and denied.json() == {"error": "not_found"}


def test_search_stale_generation_epoch_and_model_dimension_gate(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    fake_provider(monkeypatch)
    ids = indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    first = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": False}).json()
    assert first["count"] == 1
    chunk_id = first["hits"][0]["chunk_id"]
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
    assert browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": False}).json()["count"] == 0
    assert browser.get(f"/search/chunks/{chunk_id}").status_code == 404
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch-1 WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
        conn.execute(text("UPDATE index_generations SET dimensions=4 WHERE item_id=:i"), {"i": uuid.UUID(ids["item_id"])})
    assert browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": False}).json()["count"] == 0
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE index_generations SET dimensions=3 WHERE item_id=:i"), {"i": uuid.UUID(ids["item_id"])})
        conn.execute(text("UPDATE index_generations SET state='superseded' WHERE item_id=:i"), {"i": uuid.UUID(ids["item_id"])})
    assert browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": False}).json()["count"] == 0
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE index_generations SET state='active' WHERE item_id=:i"), {"i": uuid.UUID(ids["item_id"])})
        conn.execute(text("UPDATE library_items SET content_status='metadata_only' WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
    assert browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": False}).json()["count"] == 0
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET content_status='indexed' WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
        conn.execute(text("UPDATE index_generations SET model_id='other/model' WHERE item_id=:i"), {"i": uuid.UUID(ids["item_id"])})
    assert browser.post("/search", {"query": "keyword", "mode": "hybrid", "rerank": False}).json()["error"] == "search_index_model_mismatch"
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE index_generations SET model_id=:m WHERE item_id=:i"),
                     {"i": uuid.UUID(ids["item_id"]), "m": settings.embedding.model_id})
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type: EmbeddingResult([.1, .1], cfg.embedding.model_id, 1))
    resp = browser.post("/search", {"query": "keyword", "mode": "hybrid", "rerank": False})
    assert resp.status_code == 503 and resp.json()["error"] == "search_embedding_dimensions_mismatch"
    assert api.scalar("SELECT count(*) FROM search_usage WHERE status='uncertain'") == 1


def test_partial_pdf_hit_keeps_coverage_and_page_locator(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    fake_provider(monkeypatch)
    ids = upload_pdf(browser, partial_pdf())
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    configured(settings)
    result = browser.post("/search", {"query": "observation", "mode": "keyword", "rerank": False}).json()
    assert result["count"] > 0
    assert all(hit["coverage"] == "partial" and hit["locator_kind"] == "pdf"
               and hit["start_page"] is not None for hit in result["hits"])


@pytest.mark.parametrize("failure,expected", [
    (RerankError("rerank_rate_limited", True), "degraded_rerank_rate_limited"),
    (RerankError("rerank_outcome_uncertain"), "degraded_rerank_outcome_uncertain"),
])
def test_rerank_degrades_with_accounting(upload_api, monkeypatch, failure, expected):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    def fail(*args):
        raise failure
    monkeypatch.setattr("app.search.rerank", fail)
    resp = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
    assert resp.status_code == 200 and resp.json()["rerank_status"] == expected
    status = "rejected" if failure.rejected else "uncertain"
    assert api.scalar("SELECT status FROM search_usage") == status


def test_scope_changes_before_rerank_and_response(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    ids = indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    payload = []
    def revoke_before_hydrate(db, principal, item_id, chunk_ids, **audience):
        if not payload:
            payload.append("first")
            with api.engine.begin() as conn:
                conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
        return original(db, principal, item_id, chunk_ids, **audience)
    from app import search as module
    original = module.hydrate
    monkeypatch.setattr(module, "hydrate", revoke_before_hydrate)
    monkeypatch.setattr(module, "rerank", lambda *args: payload.append("provider"))
    resp = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
    assert resp.status_code == 409 and resp.json() == {"error": "search_scope_changed"}
    assert "provider" not in payload


def test_revoke_during_rerank_aborts_response(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    ids = indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    def revoke(query, docs, cfg):
        with api.engine.begin() as conn:
            conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
        return ([{"index": i, "relevance_score": 1.0} for i in range(len(docs))], 2, None)
    monkeypatch.setattr("app.search.rerank", revoke)
    resp = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
    assert resp.status_code == 409 and resp.json()["error"] == "search_scope_changed"
    assert api.scalar("SELECT status FROM search_usage") == "completed"


def test_query_embedding_429_and_timeout_accounting(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    fake_provider(monkeypatch)
    indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    for error, status in ((EmbeddingError("provider_rate_limited", True), "rejected"),
                          (EmbeddingError("provider_outcome_uncertain"), "uncertain")):
        with api.engine.connect() as conn:
            before=set(conn.scalars(text('SELECT id FROM search_usage')))
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr("app.search.embed", fail)
        response = browser.post("/search", {"query": "keyword", "mode": "hybrid", "rerank": False})
        assert response.status_code == 503 and response.json()["error"] == error.code
        with api.engine.connect() as conn:
            added=[r for r in conn.execute(text('SELECT id,status FROM search_usage')) if r.id not in before]
        assert len(added)==1 and added[0].status==status


def test_rerank_transport_contract_and_bad_response(monkeypatch):
    settings = Settings(db=None, database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-test-key")
    captured = []
    original_client = httpx.Client
    def handler(request):
        payload = __import__("json").loads(request.content)
        captured.append(payload)
        assert payload == {"model": settings.rerank.model_id, "query": "synthetic query",
                           "documents": ["synthetic body"], "top_n": 1,
                           "provider": {"only": ["fireworks"], "allow_fallbacks": False, "data_collection": "deny",
                                        "zdr": True, "max_price": {"prompt": "0.200000", "request": "0.010000"}}}
        return httpx.Response(200, json={"model": "accounts/fireworks/models/qwen3-reranker-8b",
            "provider": "Fireworks",
            "results": [{"index": 0, "relevance_score": .8}], "usage": {"total_tokens": 5}})
    monkeypatch.setattr("app.search.httpx.Client", lambda timeout: original_client(
        transport=httpx.MockTransport(handler), timeout=timeout))
    response = rerank("synthetic query", ["synthetic body"], settings)
    results, tokens, units = response
    assert rerank_order([uuid.uuid4()], results)
    assert tokens == 5 and units is None and len(captured) == 1
    assert response.response_model_id == "accounts/fireworks/models/qwen3-reranker-8b"
    assert response.response_provider == "Fireworks"
    monkeypatch.setattr("app.search.httpx.Client", lambda timeout: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "model": "wrong", "provider": "Fireworks", "results": []})), timeout=timeout))
    with pytest.raises(RerankError) as error:
        rerank("synthetic query", ["synthetic body"], settings)
    assert error.value.code == "rerank_response_invalid"
    assert error.value.response_model_id == "wrong"
    assert error.value.response_provider == "Fireworks"


@pytest.mark.parametrize("response_model,response_provider", [
    ("accounts/fireworks/models/qwen3-reranker-8b", "Other"),
    ("accounts/fireworks/models/qwen3-reranker-8b", None),
    (None, "Fireworks"),
])
def test_rerank_transport_rejects_unknown_identity(monkeypatch, response_model, response_provider):
    settings = Settings(db=None, database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-test-key")
    payload = {"results": [{"index": 0, "relevance_score": .8}]}
    if response_model is not None:
        payload["model"] = response_model
    if response_provider is not None:
        payload["provider"] = response_provider
    original_client = httpx.Client
    monkeypatch.setattr("app.search.httpx.Client", lambda timeout: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), timeout=timeout))
    with pytest.raises(RerankError) as error:
        rerank("synthetic query", ["synthetic body"], settings)
    assert error.value.code == "rerank_response_invalid"
    assert error.value.response_model_id == response_model
    assert error.value.response_provider == response_provider


def test_rerank_transport_429_and_timeout(monkeypatch):
    settings = Settings(db=None, database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-test-key")
    original_client = httpx.Client
    monkeypatch.setattr("app.search.httpx.Client", lambda timeout: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(429)), timeout=timeout))
    with pytest.raises(RerankError) as error:
        rerank("synthetic query", ["synthetic body"], settings)
    assert error.value.code == "rerank_rate_limited" and error.value.rejected

    def timeout_handler(request):
        raise httpx.ReadTimeout("synthetic timeout")
    monkeypatch.setattr("app.search.httpx.Client", lambda timeout: original_client(
        transport=httpx.MockTransport(timeout_handler), timeout=timeout))
    with pytest.raises(RerankError) as error:
        rerank("synthetic query", ["synthetic body"], settings)
    assert error.value.code == "rerank_outcome_uncertain" and not error.value.rejected


def test_malformed_rerank_result_reconciles_reservation(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    monkeypatch.setattr("app.search.rerank", lambda query, docs, cfg: ([None], None, None))
    response = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
    assert response.status_code == 200, response.text
    assert response.json()["rerank_status"] == "degraded_rerank_response_invalid"
    ledger = response.json()["cost"]["rerank"]
    assert ledger["status"] == "unknown_reserved_ceiling"
    with api.engine.connect() as conn:
        row = conn.execute(text("SELECT status,cost_status,reserved_usd,cost_usd FROM search_usage WHERE task_type='rerank'")).one()
    assert row.status == "uncertain" and row.cost_status == "unknown_reserved_ceiling"
    assert row.reserved_usd > 0 and row.cost_usd is None


def test_search_audits_query_and_rerank_response_identity(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type:
        EmbeddingResult([.1, .1, .1], cfg.embedding.model_id, 2,
                        "text-embedding-3-small", "Azure"))
    monkeypatch.setattr("app.search.rerank", lambda query, docs, cfg:
        RerankResult([{"index": i, "relevance_score": 1.0} for i in range(len(docs))],
                     3, None, "accounts/fireworks/models/qwen3-reranker-8b", "Fireworks"))
    response = browser.post("/search", {"query": "keyword", "mode": "hybrid", "rerank": True})
    assert response.status_code == 200 and response.json()["rerank_status"] == "applied"
    with api.engine.connect() as conn:
        rows = conn.execute(text("SELECT task_type,model_id,provider,response_model_id,response_provider"
                                 " FROM search_usage ORDER BY task_type")).all()
    assert rows == [
        ("embed_query", settings.embedding.model_id, "openrouter", "text-embedding-3-small", "Azure"),
        ("rerank", settings.rerank.model_id, "openrouter",
         "accounts/fireworks/models/qwen3-reranker-8b", "Fireworks"),
    ]


def test_identity_rejection_keeps_reservation_and_audits_response(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type:
        (_ for _ in ()).throw(EmbeddingError("provider_response_invalid",
            response_model_id="other", response_provider="Azure")))
    failed = browser.post("/search", {"query": "keyword", "mode": "hybrid", "rerank": False})
    assert failed.status_code == 503 and failed.json()["error"] == "provider_response_invalid"
    monkeypatch.setattr("app.search.rerank", lambda query, docs, cfg:
        (_ for _ in ()).throw(RerankError("rerank_response_invalid",
            response_model_id="other", response_provider="Fireworks")))
    degraded = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
    assert degraded.status_code == 200
    assert degraded.json()["rerank_status"] == "degraded_rerank_response_invalid"
    with api.engine.connect() as conn:
        rows = conn.execute(text("SELECT task_type,status,cost_status,reserved_usd,cost_usd,"
                                 " response_model_id,response_provider FROM search_usage ORDER BY task_type")).all()
    assert len(rows) == 2
    assert all(row.status == "uncertain" and row.cost_status == "unknown_reserved_ceiling"
               and row.reserved_usd > 0 and row.cost_usd is None for row in rows)
    assert [(row.task_type, row.response_model_id, row.response_provider) for row in rows] == [
        ("embed_query", "other", "Azure"), ("rerank", "other", "Fireworks")]


def test_rerank_payload_is_opt_in(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    indexed(api, storage, browser, b"## Methods\nSynthetic keyword body.\n", settings)
    configured(settings)
    sent = []
    def provider(query, docs, cfg):
        sent.append((query, docs))
        return ([{"index": i, "relevance_score": 1.0} for i in range(len(docs))], 3, None)
    monkeypatch.setattr("app.search.rerank", provider)
    off = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": False})
    assert off.status_code == 200 and off.json()["rerank_status"] == "not_requested"
    assert sent == []
    on = browser.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
    assert on.status_code == 200 and on.json()["rerank_status"] == "applied"
    assert sent == [("keyword", ["## Methods\nSynthetic keyword body."])]


def test_concurrent_search_reservations_share_bucket(upload_api):
    api, _ = upload_api
    user = api.create_user("a@example.org")
    settings = settings_with_fake_key()
    settings.budget.user_rerank_daily_usd = 0.000001
    principal = Principal(user, "user", uuid.uuid4(), "a@example.org", None)
    def one():
        with Session(api.engine) as db:
            try:
                return reserve(db, principal, settings, "rerank", Decimal("0.00000060"))
            except Exception as exc:
                return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: one(), range(2)))
    assert sum(isinstance(value, uuid.UUID) for value in results) == 1
    assert sum(getattr(value, "code", None) == "search_user_rerank_budget_exhausted" for value in results) == 1
    assert api.scalar("SELECT count(*) FROM search_usage") == 1


def test_keyword_request_quota_and_concurrent_reservation(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    settings = settings_with_fake_key()
    settings.search.max_requests_per_user_day = 1
    configured(settings)
    assert browser.post("/search", {"query": "x", "mode": "keyword"}).status_code == 200
    assert browser.post("/search", {"query": "x", "mode": "keyword"}).status_code == 429
    assert api.scalar("SELECT request_count FROM search_request_days") == 1


def test_parallel_query_embedding_obeys_shared_concurrency(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 1
    fake_provider(monkeypatch)
    indexed(api, storage, a, b"## Methods\nSynthetic A keyword body.\n", settings)
    indexed(api, storage, b, b"## Methods\nSynthetic B keyword body.\n", settings)
    configured(settings)
    entered, release = Event(), Event()
    calls = []
    def slow_embed(query, selected, input_type):
        calls.append(query)
        entered.set()
        assert release.wait(10)
        return EmbeddingResult([.1, .2, .3], selected.embedding.model_id, 2)
    monkeypatch.setattr("app.search.embed", slow_embed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(a.post, "/search", {"query": "keyword", "mode": "hybrid", "rerank": False})
        assert entered.wait(10)
        second = b.post("/search", {"query": "keyword", "mode": "hybrid", "rerank": False})
        assert second.status_code == 429 and second.json()["error"] == "search_embedding_concurrency_limit"
        release.set()
        assert first.result().status_code == 200
    assert len(calls) == 1
    assert api.scalar("SELECT count(*) FROM search_usage WHERE task_type='embed_query' AND status='reserved'") == 0


def test_worker_index_and_query_embedding_share_concurrency(upload_api, monkeypatch):
    api, storage = upload_api
    monkeypatch.setattr("app.ingest.worker.utcnow", lambda: api.clock.now)
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 1
    fake_provider(monkeypatch)
    indexed(api, storage, a, b"## Methods\nSynthetic A keyword body.\n", settings)
    pending = upload(b, b"## Methods\nSynthetic B pending body.\n")
    assert process_job(uuid.UUID(pending["job_id"]), storage=storage) == "awaiting_index"
    configured(settings)
    entered, release = Event(), Event()
    def slow_embed(query, selected, input_type):
        entered.set()
        assert release.wait(10)
        return EmbeddingResult([.1, .2, .3], selected.embedding.model_id, 2)
    monkeypatch.setattr("app.search.embed", slow_embed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(a.post, "/search", {"query": "keyword", "mode": "hybrid", "rerank": False})
        assert entered.wait(10)
        assert process_job(uuid.UUID(pending["job_id"]), settings, storage) == "embedding_concurrency_limit"
        release.set()
        assert first.result().status_code == 200
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='reserved'") == 0


def test_parallel_rerank_obeys_concurrency(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    settings.rerank.max_concurrent_requests = 1
    fake_provider(monkeypatch)
    indexed(api, storage, a, b"## Methods\nSynthetic A keyword body.\n", settings)
    indexed(api, storage, b, b"## Methods\nSynthetic B keyword body.\n", settings)
    configured(settings)
    entered, release = Event(), Event()
    calls = []
    def slow_rerank(query, docs, selected):
        calls.append(docs)
        entered.set()
        assert release.wait(10)
        return ([{"index": i, "relevance_score": 1.0} for i in range(len(docs))], 3, None)
    monkeypatch.setattr("app.search.rerank", slow_rerank)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(a.post, "/search", {"query": "keyword", "mode": "keyword", "rerank": True})
        assert entered.wait(10)
        second = b.post("/search", {"query": "keyword", "mode": "keyword", "rerank": True})
        assert second.status_code == 200 and second.json()["rerank_status"] == "degraded_concurrency"
        release.set()
        assert first.result().json()["rerank_status"] == "applied"
    assert len(calls) == 1
    assert api.scalar("SELECT count(*) FROM search_usage WHERE task_type='rerank' AND status='reserved'") == 0


def test_concurrent_bucket_warnings_include_all_reservations(upload_api):
    api, _ = upload_api
    users = [api.create_user(f"{name}@example.org") for name in ("a", "b")]
    principals = [Principal(user, "user", uuid.uuid4(), f"{name}@example.org", None)
                  for user, name in zip(users, ("a", "b"))]
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 2
    settings.rerank.max_concurrent_requests = 2
    settings.budget.embedding_daily_usd = 0.00000120
    settings.budget.user_embedding_daily_usd = 0.00000120
    settings.budget.embedding_warning_fraction = 0.75
    settings.budget.rerank_daily_usd = 0.00000120
    settings.budget.user_rerank_daily_usd = 0.00000120
    settings.budget.rerank_warning_fraction = 0.75
    for task, expected in (("embed_query", "embedding_bucket_near_limit"),
                           ("rerank", "rerank_bucket_near_limit")):
        start = Barrier(2)
        def one(principal):
            with Session(api.engine) as db:
                start.wait(timeout=10)
                return reserve(db, principal, settings, task, Decimal("0.00000060"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(one, principals))
        assert len(set(ids)) == 2
        with api.engine.connect() as conn:
            warnings = conn.scalars(text("SELECT warnings FROM search_usage WHERE task_type=:t"), {"t": task}).all()
        assert sum(expected in row for row in warnings) == 1


def test_query_embedding_warning_counts_document_spend(upload_api):
    api, _ = upload_api
    user = api.create_user("a@example.org")
    principal = Principal(user, "user", uuid.uuid4(), "a@example.org", None)
    settings = settings_with_fake_key()
    settings.budget.embedding_daily_usd = 0.00000120
    settings.budget.user_embedding_daily_usd = 0.00000120
    settings.budget.embedding_warning_fraction = 0.75
    with api.engine.begin() as conn:
        conn.execute(text("INSERT INTO embedding_budget_days(day,committed_usd,reserved_usd) VALUES(CURRENT_DATE,0.00000060,0)"))
    with Session(api.engine) as db:
        usage_id = reserve(db, principal, settings, "embed_query", Decimal("0.00000060"))
    with api.engine.connect() as conn:
        warnings = conn.scalar(text("SELECT warnings FROM search_usage WHERE id=:i"), {"i": usage_id})
    assert warnings == ["embedding_bucket_near_limit"]


def test_document_index_warning_counts_query_reservation(upload_api, monkeypatch):
    api, storage = upload_api
    query_user = api.create_user("a@example.org")
    api.create_user("b@example.org")
    browser = api.user_browser("b@example.org")
    ids = upload(browser, b"## Methods\nSynthetic B body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 2
    settings.embedding.max_request_bytes = 64
    settings.budget.embedding_daily_usd = 0.00001300
    settings.budget.user_embedding_daily_usd = 0.00001300
    settings.budget.embedding_warning_fraction = 0.75
    principal = Principal(query_user, "user", uuid.uuid4(), "a@example.org", None)
    with Session(api.engine) as db:
        reserve(db, principal, settings, "embed_query", Decimal("0.00000640"))
    fake_provider(monkeypatch)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    with api.engine.connect() as conn:
        warnings = conn.scalar(text("SELECT warnings FROM ingestion_jobs WHERE id=:j"),
                               {"j": uuid.UUID(ids["job_id"])})
    assert "embedding_bucket_near_limit" in warnings


@pytest.mark.parametrize("task", ["embed_query", "rerank"])
def test_expired_search_lease_frees_slot_but_keeps_cost_and_late_settlement_once(upload_api, task):
    api, _ = upload_api
    users = [api.create_user(f"{name}@example.org") for name in ("a", "b", "c")]
    principals = [Principal(user, "user", uuid.uuid4(), f"{name}@example.org", None)
                  for user, name in zip(users, ("a", "b", "c"))]
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 1
    settings.rerank.max_concurrent_requests = 1
    amount = Decimal("0.00000060")
    with Session(api.engine) as db:
        stale = reserve(db, principals[0], settings, task, amount)
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE search_usage SET lease_until=now()-interval '1 second',"
                          " day=CURRENT_DATE-1 WHERE id=:i"), {"i": stale})

    start = Barrier(2)
    def compete(principal):
        with Session(api.engine) as db:
            start.wait(timeout=10)
            try:
                return reserve(db, principal, settings, task, amount)
            except Exception as exc:
                return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, principals[1:]))
    assert sum(isinstance(x, uuid.UUID) for x in outcomes) == 1
    assert sum(getattr(x, "code", None) == f"search_{'embedding' if task == 'embed_query' else 'rerank'}_concurrency_limit"
               for x in outcomes) == 1
    with api.engine.connect() as conn:
        row = conn.execute(text("SELECT status,cost_status,reserved_usd,cost_usd FROM search_usage WHERE id=:i"),
                           {"i": stale}).one()
    assert row == ("uncertain", "lease_expired_reserved_ceiling", amount, None)
    with Session(api.engine) as db:
        assert settle(db, stale, tokens=2, price=0.20) == "usage_price_estimate"
        assert settle(db, stale, tokens=9, price=0.20) == "usage_price_estimate"
    with api.engine.connect() as conn:
        row = conn.execute(text("SELECT status,cost_usd,reserved_usd FROM search_usage WHERE id=:i"),
                           {"i": stale}).one()
    assert row == ("completed", Decimal("0.00000040"), amount)


def test_live_search_reservation_renews_lease(upload_api):
    api, _ = upload_api
    user = api.create_user("a@example.org")
    principal = Principal(user, "user", uuid.uuid4(), "a@example.org", None)
    settings = settings_with_fake_key()
    with Session(api.engine) as db:
        usage_id = reserve(db, principal, settings, "embed_query", Decimal("0.00000010"))
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE search_usage SET lease_until=now()+interval '4 seconds' WHERE id=:i"),
                     {"i": usage_id})
    with reservation_heartbeat(api.engine, SearchUsage, usage_id, 3):
        from time import sleep
        sleep(5.2)
        reap_expired(api.engine)
        assert api.scalar("SELECT count(*) FROM search_usage WHERE status='reserved'") == 1


@pytest.mark.parametrize("task", ["embed_query", "rerank"])
def test_expired_lease_keeps_current_day_budget_and_user_quota(upload_api, task):
    api, _ = upload_api
    user = api.create_user("a@example.org")
    principal = Principal(user, "user", uuid.uuid4(), "a@example.org", None)
    settings = settings_with_fake_key()
    if task == "embed_query":
        settings.budget.embedding_daily_usd = 0.00000060
        settings.budget.user_embedding_daily_usd = 0.00000060
    else:
        settings.budget.rerank_daily_usd = 0.00000060
        settings.budget.user_rerank_daily_usd = 0.00000060
    with Session(api.engine) as db:
        stale = reserve(db, principal, settings, task, Decimal("0.00000060"))
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE search_usage SET lease_until=now()-interval '1 second' WHERE id=:i"),
                     {"i": stale})
    with Session(api.engine) as db:
        with pytest.raises(Exception) as exc:
            reserve(db, principal, settings, task, Decimal("0.00000010"))
    assert getattr(exc.value, "code", None) == ("search_embedding_budget_exhausted" if task == "embed_query"
                                                 else "search_rerank_budget_exhausted")
    assert api.scalar("SELECT count(*) FROM search_usage WHERE status='uncertain'") == 1


def test_query_user_warning_counts_document_and_concurrent_queries(upload_api, monkeypatch):
    api, storage = upload_api
    user = api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    ids = upload(browser, b"## Methods\nSynthetic body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    principal = Principal(user, "user", uuid.uuid4(), "a@example.org", None)
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 2
    settings.embedding.max_request_bytes = 64
    settings.embedding.max_usd_per_million_tokens = 0.01
    settings.budget.user_embedding_daily_usd = 0.00000120
    settings.budget.user_embedding_warning_fraction = 0.75
    monkeypatch.setattr("app.ingest.indexer.embed", lambda value, selected:
                        EmbeddingResult([.1, .2, .3], selected.embedding.model_id, 30))
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    assert api.scalar("SELECT committed_usd FROM embedding_user_budget_days") == Decimal("0.00000030")
    start = Barrier(2)
    def one():
        with Session(api.engine) as db:
            start.wait(timeout=10)
            return reserve(db, principal, settings, "embed_query", Decimal("0.00000030"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: one(), range(2)))
    with api.engine.connect() as conn:
        warnings = conn.scalars(text("SELECT warnings FROM search_usage WHERE id IN (:a,:b)"),
                                {"a": ids[0], "b": ids[1]}).all()
    assert sum("embedding_user_quota_near_limit" in row for row in warnings) == 1


def test_document_user_warning_counts_query_reservation(upload_api, monkeypatch):
    api, storage = upload_api
    user = api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    ids = upload(browser, b"## Methods\nSynthetic body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_request_bytes = 64
    settings.embedding.max_concurrent_requests = 2
    settings.budget.user_embedding_daily_usd = 0.00001300
    settings.budget.user_embedding_warning_fraction = 0.75
    principal = Principal(user, "user", uuid.uuid4(), "a@example.org", None)
    with Session(api.engine) as db:
        reserve(db, principal, settings, "embed_query", Decimal("0.00000640"))
    fake_provider(monkeypatch)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    with api.engine.connect() as conn:
        warnings = conn.scalar(text("SELECT warnings FROM ingestion_jobs WHERE id=:j"),
                               {"j": uuid.UUID(ids["job_id"])})
    assert "embedding_user_quota_near_limit" in warnings
