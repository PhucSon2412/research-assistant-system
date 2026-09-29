from __future__ import annotations

import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest.embedding import EmbeddingError, EmbeddingResult
from app.ingest.worker import due_job_ids, process_job
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import full_pdf, image_only_pdf, partial_pdf, upload_pdf
from tests.test_uploads import upload_api


def settings_with_fake_key():
    settings = get_settings().model_copy(deep=True)
    settings.embedding.api_key = SecretStr("synthetic-test-key")
    settings.embedding.pricing_verified = True
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


def test_document_embedding_audits_response_identity(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    monkeypatch.setattr("app.ingest.indexer.embed", lambda value, selected:
        EmbeddingResult([.1, .2, .3], selected.embedding.model_id, 3,
                        "text-embedding-3-small", "Azure"))
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    with api.engine.connect() as conn:
        row = conn.execute(text("SELECT model_id,provider,response_model_id,response_provider"
                                " FROM embedding_calls")).one()
    assert row == (settings.embedding.model_id, "openrouter", "text-embedding-3-small", "Azure")


def test_document_identity_rejection_keeps_reservation_and_audit(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic body.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    def reject(value, selected):
        raise EmbeddingError("provider_response_invalid", response_model_id="other",
                             response_provider="Azure")
    monkeypatch.setattr("app.ingest.indexer.embed", reject)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "provider_response_invalid"
    with api.engine.connect() as conn:
        row = conn.execute(text("SELECT status,cost_status,reserved_usd,cost_usd,"
                                " response_model_id,response_provider FROM embedding_calls")).one()
    assert row.status == "uncertain" and row.cost_status == "unknown_reserved_ceiling"
    assert row.reserved_usd > 0 and row.cost_usd is None
    assert (row.response_model_id, row.response_provider) == ("other", "Azure")


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


def test_markdown_references_and_notes_never_becomes_evidence(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"),
        b"## Methods\nSynthetic body finding.\n## References and Notes\n[1] Bibliography secret.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    assert api.scalar("SELECT count(*) FROM sections WHERE kind='body' AND text LIKE '%Bibliography secret%'") == 0
    calls = fake_provider(monkeypatch)
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "ready"
    assert calls and all("Bibliography" not in value and "[1]" not in value for value in calls)
    assert api.scalar("SELECT count(*) FROM index_entries WHERE text LIKE '%Bibliography secret%'") == 0
    assert api.scalar("SELECT count(*) FROM reference_entries WHERE raw_citation LIKE '%Bibliography secret%'") == 1


def test_uncertain_markdown_bibliography_requires_review(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"),
        b"## Methods\nSynthetic body.\n## References to Earlier Work\n[1] Suspect.\n")
    calls = fake_provider(monkeypatch)
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "needs_review"
    assert not calls
    assert api.scalar("SELECT count(*) FROM chunks WHERE source_file_id=:f", f=uuid.UUID(ids["file_id"])) == 0
    assert api.scalar("SELECT count(*) FROM index_entries") == 0


def test_same_size_pdf_replacement_is_rejected_before_embedding(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload_pdf(api.user_browser("a@example.org"), full_pdf())
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids["file_id"]))
    original = full_pdf()
    replacement = original[:-1] + bytes([original[-1] ^ 1])
    assert len(replacement) == len(original)
    storage.put(key, io.BytesIO(replacement), "application/pdf")
    calls = fake_provider(monkeypatch)
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "checksum_mismatch"
    assert not calls
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0
    assert api.scalar("SELECT content_status FROM library_items WHERE id=:i", i=uuid.UUID(ids["item_id"])) == "metadata_only"


def test_pdf_replacement_during_provider_cannot_store_or_publish_result(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    original = full_pdf()
    ids = upload_pdf(api.user_browser("a@example.org"), original)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids["file_id"]))
    def replace(value, selected):
        storage.put(key, io.BytesIO(original[:-1] + bytes([original[-1] ^ 1])), "application/pdf")
        return EmbeddingResult([0.1, 0.2], selected.embedding.model_id, 5)
    monkeypatch.setattr("app.ingest.indexer.embed", replace)
    assert process_job(uuid.UUID(ids["job_id"]), settings_with_fake_key(), storage) == "checksum_mismatch"
    assert api.scalar("SELECT count(*) FROM index_entries") == 0
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 0


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


def test_unverified_price_blocks_private_provider_payload(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic price gate.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    settings.embedding.pricing_verified = False
    with get_sessionmaker()() as db:
        assert uuid.UUID(ids["job_id"]) not in due_job_ids(db, settings)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "not_claimed"
    assert not calls
    assert api.scalar("SELECT count(*) FROM embedding_calls") == 0


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
        item = conn.scalar(text("INSERT INTO library_items(visibility, publication_state, title) VALUES('common','listed','Synthetic common') RETURNING id"))
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
        conn.execute(text("UPDATE embedding_calls SET lease_until=now()-interval '1 second'"
                          " WHERE status='reserved'"))
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
    settings.embedding.max_request_bytes = 64
    settings.budget.embedding_daily_usd = 0.0000064
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda ids: process_job(uuid.UUID(ids["job_id"]), settings, storage), (first, second)))
    assert sorted(states) == ["embedding_budget_exhausted", "ready"]
    assert len(calls) == 1
    assert api.scalar("SELECT committed_usd+reserved_usd FROM embedding_budget_days") <= 0.0000064
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 1


def test_concurrent_users_trigger_app_bucket_warning_without_user_warning(upload_api, monkeypatch):
    api, storage = upload_api
    ids = []
    for name in ("a", "b", "c"):
        email = f"{name}@example.org"
        api.create_user(email)
        job = upload(api.user_browser(email), f"## Methods\nSynthetic {name} finding.\n".encode())
        assert process_job(uuid.UUID(job["job_id"]), storage=storage) == "awaiting_index"
        ids.append(job)
    settings = settings_with_fake_key()
    settings.embedding.max_request_bytes = 64
    settings.embedding.max_concurrent_requests = 3
    settings.budget.embedding_daily_usd = 0.0000128  # two full reservations
    settings.budget.embedding_warning_fraction = 0.75
    settings.budget.user_embedding_daily_usd = 0.0000128
    settings.budget.user_embedding_warning_fraction = 0.9
    admitted = Barrier(2)
    calls = []

    def provider(value, selected):
        calls.append(value)
        admitted.wait(timeout=10)  # both reservations overlap before settlement
        return EmbeddingResult([0.1, 0.2], selected.embedding.model_id, None)

    monkeypatch.setattr("app.ingest.indexer.embed", provider)
    with ThreadPoolExecutor(max_workers=3) as pool:
        states = list(pool.map(lambda job: process_job(uuid.UUID(job["job_id"]), settings, storage), ids))
    assert sorted(states) == ["embedding_budget_exhausted", "ready", "ready"]
    assert len(calls) == 2
    assert api.scalar("SELECT committed_usd+reserved_usd FROM embedding_budget_days") == Decimal("0.0000128")
    assert api.scalar("SELECT count(*) FROM ingestion_jobs WHERE warnings ? 'embedding_bucket_near_limit'") == 1
    assert api.scalar("SELECT count(*) FROM ingestion_jobs WHERE warnings ? 'embedding_user_quota_near_limit'") == 0
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 2


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


def test_dead_document_reservation_reaped_and_late_response_reconciles_once(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    first = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic first body.\n")
    assert process_job(uuid.UUID(first["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_concurrent_requests = 1
    entered, release = Event(), Event()
    def provider(value, selected):
        entered.set()
        assert release.wait(10)
        return EmbeddingResult([0.1, 0.2], selected.embedding.model_id, 3)
    monkeypatch.setattr("app.ingest.indexer.embed", provider)
    from app.authz import Principal
    from app.search import reserve
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(process_job, uuid.UUID(first["job_id"]), settings, storage)
        assert entered.wait(10)
        with api.engine.begin() as conn:
            conn.execute(text("UPDATE embedding_calls SET lease_until=now()-interval '1 second'"
                              " WHERE status='reserved'"))
        user = api.create_user("c@example.org")
        principal = Principal(user, "user", uuid.uuid4(), "c@example.org", None)
        with Session(api.engine) as db:
            new_id = reserve(db, principal, settings, "embed_query", Decimal("0.00000010"))
        assert new_id is not None
        assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='uncertain' AND cost_status='lease_expired_reserved_ceiling'") == 1
        before = api.scalar("SELECT reserved_usd FROM embedding_budget_days")
        assert before > 0
        release.set()
        assert running.result() == "ready"
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status='completed'") == 1
    assert api.scalar("SELECT reserved_usd FROM embedding_budget_days") == 0
    committed = api.scalar("SELECT committed_usd FROM embedding_budget_days")
    from app.ingest.indexer import _provider_failure
    from app.ingest.embedding import EmbeddingError
    call_id = api.scalar("SELECT id FROM embedding_calls")
    with Session(api.engine) as db:
        _provider_failure(db, call_id, EmbeddingError("late_duplicate"))
    assert api.scalar("SELECT committed_usd FROM embedding_budget_days") == committed


def test_same_user_quota_is_atomic_and_warns_near_limit(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    browser = api.user_browser("a@example.org")
    first = upload(browser, b"## Methods\nSynthetic first finding.\n")
    second = upload(browser, b"## Results\nSynthetic second finding.\n")
    for ids in (first, second):
        assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_request_bytes = 64
    settings.budget.user_embedding_daily_usd = 0.0000064  # one full reservation
    calls = []
    def provider(value, selected):
        calls.append(value)
        return EmbeddingResult([0.1, 0.2], selected.embedding.model_id, None)
    monkeypatch.setattr("app.ingest.indexer.embed", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda ids: process_job(uuid.UUID(ids["job_id"]), settings, storage), (first, second)))
    assert sorted(states) == ["embedding_user_quota_exhausted", "ready"]
    assert len(calls) == 1
    assert api.scalar("SELECT committed_usd+reserved_usd FROM embedding_user_budget_days") == Decimal("0.0000064")
    assert api.scalar("SELECT count(*) FROM ingestion_jobs WHERE warnings ? 'embedding_user_quota_near_limit'") == 1
    assert api.scalar("SELECT count(*) FROM index_generations WHERE state='active'") == 1


def test_user_reservation_released_on_429_then_reconciled_on_retry(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic retry finding.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_request_bytes = 64
    settings.budget.user_embedding_daily_usd = 0.0000064
    calls = []
    def provider(value, selected):
        calls.append(value)
        if len(calls) == 1:
            raise EmbeddingError("provider_rate_limited", definitely_rejected=True)
        return EmbeddingResult([0.1, 0.2], selected.embedding.model_id, 5)
    monkeypatch.setattr("app.ingest.indexer.embed", provider)
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "provider_rate_limited"
    assert api.scalar("SELECT committed_usd+reserved_usd FROM embedding_user_budget_days") == 0
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET next_attempt_at=NULL WHERE id=:j"), {"j": uuid.UUID(ids["job_id"])})
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    assert len(calls) == 2
    assert api.scalar("SELECT committed_usd FROM embedding_user_budget_days") == Decimal("0.0000005")
    assert api.scalar("SELECT reserved_usd FROM embedding_user_budget_days") == 0
    assert api.scalar("SELECT committed_usd FROM embedding_budget_days") == Decimal("0.0000005")
    assert api.scalar("SELECT cost_status FROM embedding_calls") == "usage_price_estimate"


def test_user_quota_reconciles_higher_observed_usage(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nSynthetic high usage.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    settings = settings_with_fake_key()
    settings.embedding.max_request_bytes = 128
    settings.budget.user_embedding_daily_usd = 0.0000128
    monkeypatch.setattr("app.ingest.indexer.embed", lambda value, selected:
        EmbeddingResult([0.1, 0.2], selected.embedding.model_id, 100))
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    assert api.scalar("SELECT committed_usd FROM embedding_user_budget_days") == Decimal("0.0000100")
    assert api.scalar("SELECT reserved_usd FROM embedding_user_budget_days") == 0
