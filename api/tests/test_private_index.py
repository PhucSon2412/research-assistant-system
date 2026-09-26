from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.config import get_settings
from app.ingest.embedding import EmbeddingError, EmbeddingResult
from app.ingest.worker import process_job
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import full_pdf, image_only_pdf, partial_pdf, upload_pdf
from tests.test_uploads import upload_api


def settings_with_fake_key():
    settings = get_settings().model_copy(deep=True)
    settings.embedding.api_key = SecretStr("synthetic-test-key")
    return settings


def fake_provider(monkeypatch, dimensions=3, action=None):
    calls = []

    def fake(value, settings):
        assert isinstance(value, str) and value.strip()
        assert not value.startswith("%PDF-")
        assert "References" not in value and "[1]" not in value
        calls.append(value)
        if action:
            action(len(calls))
        return EmbeddingResult([0.1] * dimensions, settings.embedding.model_id, len(value))

    monkeypatch.setattr("app.ingest.indexer.embed", fake)
    return calls


def test_markdown_indexes_once_and_isolated_by_owner(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    data = b"# Synthetic title\n\n## Abstract\nSynthetic summary.\n\n## Methods\nSynthetic finding.\n\n## References\n[1] Bibliography only.\n"
    first, second = upload(a, data), upload(b, data)
    for ids in (first, second):
        assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda ids: process_job(uuid.UUID(ids["job_id"]), settings, storage), (first, second)))
    assert states == ["ready", "ready"]
    assert len(calls) >= 4
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 2
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='completed'") == len(calls)
    assert api.scalar("SELECT count(*) FROM index_entries") >= len(calls)
    assert process_job(uuid.UUID(first["job_id"]), settings, storage) == "not_claimed"
    assert api.scalar("SELECT count(*) FROM index_entries WHERE text LIKE '%Bibliography%'") == 0
    assert b.get(f"/library/items/{first['item_id']}").status_code == 404
    assert a.get(f"/library/items/{second['item_id']}").status_code == 404
    assert a.get(f"/library/items/{first['item_id']}").json()["item"]["content_status"] == "indexed"
    assert api.scalar("SELECT count(*) FROM index_entries WHERE keywords @@ plainto_tsquery('simple', 'finding')") > 0
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE vector_dims(vector)=3") == len(calls)


@pytest.mark.parametrize("source, expected", [(full_pdf, "ready"), (partial_pdf, "ready"), (image_only_pdf, "needs_review")])
def test_pdf_coverage_index_gate(upload_api, monkeypatch, source, expected):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    ids = upload_pdf(browser, source())
    extraction = process_job(uuid.UUID(ids["job_id"]), storage=storage)
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    state = process_job(uuid.UUID(ids["job_id"]), settings, storage) if extraction == "awaiting_index" else extraction
    assert state == expected
    if expected == "ready":
        assert calls
        assert api.scalar("SELECT count(*) FROM index_generations WHERE item_id=:i AND state='active'", i=uuid.UUID(ids["item_id"])) == 1
    else:
        assert not calls
        assert api.scalar("SELECT count(*) FROM index_entries") == 0


def test_stale_layout_and_revocation_do_not_call_provider(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload_pdf(api.user_browser("a@example.org"), full_pdf())
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE chunks SET pipeline_version='pypdf-layout-v1' WHERE source_file_id=:f"), {"f": uuid.UUID(ids["file_id"])})
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "index_body_missing"
    assert not calls
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0


def test_429_retry_keeps_completed_chunks_and_no_partial_publish(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\n" + b"Synthetic text. " * 200)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = []

    def provider(value, settings):
        calls.append(value)
        if len(calls) == 2:
            raise EmbeddingError("provider_rate_limited", definitely_rejected=True)
        return EmbeddingResult([0.1, 0.2, 0.3], settings.embedding.model_id, 10)

    monkeypatch.setattr("app.ingest.indexer.embed", provider)
    settings = settings_with_fake_key()
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "provider_rate_limited"
    assert api.scalar("SELECT count(*) FROM index_entries") == 1
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0
    assert api.scalar("SELECT content_status FROM library_items WHERE id=:i", i=uuid.UUID(ids["item_id"])) == "metadata_only"
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET next_attempt_at=NULL WHERE id=:j"), {"j": uuid.UUID(ids["job_id"])})
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    assert calls.count(calls[0]) == 1
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 1


def test_cancel_during_provider_never_publishes(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic text for cancellation.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    def cancel(_value, settings):
        with api.engine.begin() as conn:
            conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": uuid.UUID(ids["item_id"])})
            conn.execute(text("UPDATE ingestion_jobs SET cancel_requested=true, cancel_requested_at=now() WHERE id=:j"), {"j": uuid.UUID(ids["job_id"])})
        return EmbeddingResult([0.1, 0.2], settings.embedding.model_id, 4)
    monkeypatch.setattr("app.ingest.indexer.embed", cancel)
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "cancelled"
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0
    assert api.scalar("SELECT content_status FROM library_items WHERE id=:i", i=uuid.UUID(ids["item_id"])) == "metadata_only"


def test_wrong_dimensions_and_unknown_provider_outcome_are_not_retried(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\n" + b"Synthetic repeatable body. " * 150)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    counter = []
    def wrong_dimension(value, settings):
        counter.append(value)
        return EmbeddingResult([0.1] * (2 if len(counter) == 1 else 3), settings.embedding.model_id, 4)
    monkeypatch.setattr("app.ingest.indexer.embed", wrong_dimension)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "embedding_dimensions_mismatch"
    assert api.scalar("SELECT count(*) FROM index_entries") == 1
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='uncertain'") == 1
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "not_claimed"
    assert len(counter) == 2


def test_budget_blocks_before_provider_and_keeps_metadata_only(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic budget body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    settings.budget.embedding_daily_usd = 0
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "embedding_budget_exhausted"
    assert not calls
    assert api.scalar("SELECT count(*) FROM embedding_calls") == 0
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0


def test_second_worker_and_crash_reserved_call_do_not_duplicate_request(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic concurrent body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    calls = []
    def while_in_provider(value, settings):
        calls.append(value)
        assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "not_claimed"
        raise EmbeddingError("provider_outcome_uncertain")
    monkeypatch.setattr("app.ingest.indexer.embed", while_in_provider)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "provider_outcome_uncertain"
    assert len(calls) == 1
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='uncertain'") == 1
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0


def test_missing_acknowledgement_and_pre_call_revoke(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic consent body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE upload_intents SET used_at=NULL WHERE item_id=:i"), {"i": uuid.UUID(ids["item_id"])})
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "upload_acknowledgement_missing"
    assert not calls
    assert api.scalar("SELECT count(*) FROM embedding_calls") == 0


def test_rights_epoch_changed_before_provider_blocks_payload(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic revoked body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                     {"i": uuid.UUID(ids["item_id"])})
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "cancelled"
    assert not calls
    assert api.scalar("SELECT count(*) FROM embedding_calls") == 0


def test_common_without_file_rights_is_not_indexed(upload_api, monkeypatch):
    api, storage = upload_api
    with api.engine.begin() as conn:
        item = conn.scalar(text("INSERT INTO library_items(visibility, title) VALUES('common','Synthetic common') RETURNING id"))
        version = conn.scalar(text("INSERT INTO paper_versions(item_id,coverage) VALUES(:i,'complete') RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,idempotency_key,state,rights_epoch_at_start) VALUES('discovery','common',:i,:v,'common-test','awaiting_index',0) RETURNING id"), {"i": item, "v": version})
        conn.execute(text("INSERT INTO source_files(item_id,version_id,job_id,storage_key,verified_mime,byte_size,checksum_sha256) VALUES(:i,:v,:j,'unused','text/markdown',1,:h)"), {"i": item, "v": version, "j": job, "h": "0" * 64})
    calls = fake_provider(monkeypatch)
    assert process_job(job, settings_with_fake_key(), storage) == "rights_gate_missing"
    assert not calls
    assert api.scalar("SELECT count(*) FROM index_generations") == 0


def test_worker_death_after_request_reservation_does_not_reissue(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic crash body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = []
    def die(value, settings):
        calls.append(value)
        raise SystemExit("synthetic worker crash")
    monkeypatch.setattr("app.ingest.indexer.embed", die)
    settings = settings_with_fake_key()
    with pytest.raises(SystemExit):
        process_job(uuid.UUID(ids["job_id"]), settings, storage)
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='reserved'") == 1
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET lease_until=now()-interval '1 second' WHERE id=:j"),
                     {"j": uuid.UUID(ids["job_id"])})
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "embedding_outcome_uncertain"
    assert len(calls) == 1
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='uncertain'") == 1
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0


def test_concurrent_jobs_share_daily_budget_reservation(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    first = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic A body.\n")
    second = upload(api.user_browser("b@example.org"), b"## Methods\nSynthetic B body.\n")
    for ids in (first, second):
        assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    settings.budget.embedding_daily_usd = 0.000004
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda ids: process_job(uuid.UUID(ids["job_id"]), settings, storage), (first, second)))
    assert sorted(states) == ["embedding_budget_exhausted", "ready"]
    assert len(calls) == 1
    assert api.scalar("SELECT committed_usd+reserved_usd FROM embedding_budget_days") <= 0.000004
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 1


def test_provider_concurrency_limit_is_durable_across_jobs(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    first = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic first body.\n")
    second = upload(api.user_browser("b@example.org"), b"## Methods\nSynthetic second body.\n")
    for ids in (first, second):
        assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 1
    seen = []
    def provider(value, selected):
        seen.append(value)
        if len(seen) == 1:
            assert process_job(uuid.UUID(second["job_id"]), settings, storage) == "embedding_concurrency_limit"
        return EmbeddingResult([0.1, 0.2], selected.embedding.model_id, 3)
    monkeypatch.setattr("app.ingest.indexer.embed", provider)
    assert process_job(uuid.UUID(first["job_id"]), settings, storage) == "ready"
    assert len(seen) == 1
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET next_attempt_at=NULL WHERE id=:j"), {"j": uuid.UUID(second["job_id"])})
    assert process_job(uuid.UUID(second["job_id"]), settings, storage) == "ready"
    assert len(seen) == 2
