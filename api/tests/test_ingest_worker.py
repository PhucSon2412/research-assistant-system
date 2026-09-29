from __future__ import annotations

import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.worker import _begin, claim_job, due_job_ids, process_job, utcnow
from app.models import IngestionJob, SourceFile
from tests.helpers import WEB_ORIGIN
from tests.test_uploads import intent, send, upload_api


def upload(browser, original: bytes) -> dict:
    created = intent(browser, original)
    assert created.status_code == 201, created.text
    result = send(browser, created.json()["intent_token"], original)
    assert result.status_code == 201, result.text
    return result.json()


def test_private_markdown_pipeline_locators_metadata_and_acl(upload_api):
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    original = b"# Extracted title\n\n## Abstract\nA summary.\n\n## Methods\nA result.\n\n## References\n[1] A citation.\n"
    ids = upload(a, original)
    assert a.patch(f"/library/items/{ids['item_id']}", json={"title": "User title"},
        headers={"origin": WEB_ORIGIN}).status_code == 200
    job_id = uuid.UUID(ids["job_id"])
    with Session(api.engine) as db:
        assert job_id in due_job_ids(db, get_settings())
    assert process_job(job_id, storage=storage) == "awaiting_index"
    assert process_job(job_id, storage=storage) == "not_claimed"
    detail = a.get(f"/library/items/{ids['item_id']}").json()["item"]
    assert detail["title"] == "User title"
    assert detail["content_status"] == "metadata_only"
    assert detail["versions"][0]["coverage"] == "complete"
    assert detail["jobs"][0]["state"] == "awaiting_index"
    assert detail["jobs"][0]["attempt_count"] == 1
    assert detail["jobs"][0]["queue_wait_ms"] is not None
    assert detail["jobs"][0]["extraction_finished_at"] is not None
    assert detail["jobs"][0]["extraction_processing_ms"] is not None
    assert detail["jobs"][0]["finished_at"] is None
    assert detail["jobs"][0]["processing_ms"] is None
    candidates = a.get(f"/library/items/{ids['item_id']}/metadata-candidates").json()["candidates"]
    assert candidates[0]["value"] == "Extracted title"
    chunks = a.get(f"/library/items/{ids['item_id']}/chunks").json()["chunks"]
    references = a.get(f"/library/items/{ids['item_id']}/references").json()["references"]
    assert len(references) == 1
    assert references[0]["raw_citation"] == original.decode()[references[0]["start_offset"]:references[0]["end_offset"]]
    assert "## References" not in references[0]["raw_citation"]
    assert len(chunks) >= 4
    decoded = original.decode()
    for chunk in chunks:
        assert decoded[chunk["start_offset"]:chunk["end_offset"]] == chunk["text"]
        assert chunk["source_file_id"] == ids["file_id"]
    assert api.scalar("SELECT count(*) FROM chunks") == len(chunks)
    assert api.scalar("SELECT count(*) FROM ingest_step_attempts WHERE outcome='success'") == 3
    for other in (b, admin):
        for path in ("", "/chunks", "/metadata-candidates"):
            assert other.get(f"/library/items/{ids['item_id']}{path}").status_code == 404


def test_duplicate_is_scoped_to_owner(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    original = b"# Same paper\n\nSome text.\n"
    first, second, third = upload(a, original), upload(a, original), upload(b, original)
    assert process_job(uuid.UUID(first["job_id"]), storage=storage) == "awaiting_index"
    assert process_job(uuid.UUID(second["job_id"]), storage=storage) == "duplicate"
    assert process_job(uuid.UUID(third["job_id"]), storage=storage) == "awaiting_index"
    duplicate = a.get(f"/library/items/{second['item_id']}").json()["item"]
    assert duplicate["jobs"][0]["warnings"] == ["duplicate_in_private_library"]
    assert duplicate["jobs"][0]["finished_at"] is not None
    assert duplicate["jobs"][0]["extraction_finished_at"] is None
    assert not any(key.startswith("duplicate_of") for key in duplicate)
    assert duplicate["id"] != first["item_id"]
    assert duplicate["versions"][0]["id"] != first["version_id"]
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(second["item_id"])) == 0
    assert api.scalar("SELECT count(*) FROM metadata_candidates WHERE item_id=:i", i=uuid.UUID(second["item_id"])) == 0
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(third["item_id"])) > 0
    assert b.get(f"/library/items/{first['item_id']}").status_code == 404
    assert a.get(f"/library/items/{third['item_id']}").status_code == 404
    assert api.scalar("SELECT count(*) FROM source_files") == 3


def test_simultaneous_same_owner_validation_picks_one_canonical(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    original = b"# Race\n\nSame body.\n"
    first, second = upload(a, original), upload(a, original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda ids: process_job(uuid.UUID(ids["job_id"]), storage=storage),
                               (first, second)))
    assert sorted(states) == ["awaiting_index", "duplicate"]
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id IN (:a, :b)",
                      a=uuid.UUID(first["item_id"]), b=uuid.UUID(second["item_id"])) > 0
    assert api.scalar("SELECT count(DISTINCT item_id) FROM chunks") == 1


def test_common_full_text_has_no_rights_gate(upload_api):
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    original = b"# Private source\n\nBody.\n"
    private = upload(a, original)
    with Session(api.engine) as db:
        private_source = db.get(SourceFile, uuid.UUID(private["file_id"]))
        with api.engine.begin() as conn:
            item = conn.scalar(text("INSERT INTO library_items (visibility, publication_state, title) VALUES ('common', 'listed', 'Unreviewed') RETURNING id"))
            version = conn.scalar(text("INSERT INTO paper_versions (item_id) VALUES (:i) RETURNING id"), {"i": item})
            job = conn.scalar(text("INSERT INTO ingestion_jobs (origin, scope_visibility, item_id, version_id,"
                                   " idempotency_key, rights_epoch_at_start)"
                                   " VALUES ('discovery', 'common', :i, :v, :k, 0) RETURNING id"),
                              {"i": item, "v": version, "k": str(uuid.uuid4())})
            source = conn.scalar(text("INSERT INTO source_files (item_id, version_id, job_id, storage_key, verified_mime,"
                              " byte_size, checksum_sha256) VALUES (:i, :v, :j, :k, 'text/markdown', :n, :h) RETURNING id"),
                         {"i": item, "v": version, "j": job, "k": private_source.storage_key,
                          "n": len(original), "h": private_source.checksum_sha256})
            conn.execute(text("INSERT INTO metadata_candidates (item_id, version_id, source_file_id, field_name,"
                              " value, locator_kind, start_offset, end_offset, pipeline_version)"
                              " VALUES (:i, :v, :s, 'title', 'COMMON_CANDIDATE_SECRET', 'markdown', 0, 5, 'seed')"),
                         {"i": item, "v": version, "s": source})
            section = conn.scalar(text("INSERT INTO sections (item_id, version_id, source_file_id, kind, ordinal,"
                                       " text, locator_kind, start_offset, end_offset, start_line, end_line, pipeline_version)"
                                       " VALUES (:i, :v, :s, 'body', 0, 'COMMON_CHUNK_SECRET', 'markdown', 0, 19, 1, 1, 'seed') RETURNING id"),
                                  {"i": item, "v": version, "s": source})
            conn.execute(text("INSERT INTO chunks (section_id, item_id, version_id, source_file_id, ordinal, text,"
                              " locator_kind, start_offset, end_offset, start_line, end_line, chunk_hash, pipeline_version)"
                              " VALUES (:section, :i, :v, :s, 0, 'COMMON_CHUNK_SECRET', 'markdown', 0, 19, 1, 1, :h, 'seed')"),
                         {"section": section, "i": item, "v": version, "s": source, "h": "a" * 64})
            conn.execute(text("INSERT INTO reference_entries (item_id, version_id, source_file_id, ordinal,"
                              " raw_citation, fields, confidence, provenance, locator_kind, start_offset,"
                              " end_offset, pipeline_version) VALUES (:i, :v, :s, 0,"
                              " 'COMMON_REFERENCE_SECRET', '{}'::jsonb, 'raw_only', 'seed', 'markdown', 0, 23, 'seed')"),
                         {"i": item, "v": version, "s": source})
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=item) == 1
    assert api.scalar("SELECT count(*) FROM metadata_candidates WHERE item_id=:i", i=item) == 1
    for browser in (a, b, admin):
        detail = browser.get(f"/library/items/{item}")
        assert detail.status_code == 200
        assert detail.json()["item"]["files"] == []
        assert detail.json()["item"]["jobs"] == []
        assert browser.get(f"/files/{source}/original").status_code == 404
        for path in ("/chunks", "/metadata-candidates", "/coverage", "/references"):
            response = browser.get(f"/library/items/{item}{path}")
            assert response.status_code == 404
            assert "COMMON_" not in response.text
    with Session(api.engine) as db:
        assert job not in due_job_ids(db, get_settings())
    assert process_job(job, storage=storage) == "rights_gate_missing"
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=item) == 1


def test_two_claims_and_expired_worker_do_not_duplicate(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload(a, b"# Recovery\n\nBody text.\n")
    job_id = uuid.UUID(ids["job_id"])

    def claim():
        with Session(api.engine) as db:
            return claim_job(db, job_id, get_settings())

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: claim(), range(2)))
    assert sum(token is not None for token in tokens) == 1
    first_token = next(token for token in tokens if token is not None)
    with Session(api.engine) as db:
        source = _begin(db, job_id, first_token, "validate")
        # Simulate a worker dying before the step commit. The durable attempt
        # remains, and a later claim fences the stale token.
        job = db.get(IngestionJob, job_id)
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_job(job_id, storage=storage) == "awaiting_index"
    assert api.scalar("SELECT count(*) FROM chunks") > 0
    assert api.scalar("SELECT count(*) FROM ingest_step_attempts WHERE error_code='worker_lost'") == 1
    assert api.scalar("SELECT attempt_count FROM ingestion_jobs WHERE id=:i", i=job_id) == 2


def test_storage_retry_and_epoch_cancel(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    original = b"# Retry\n\nContent.\n"
    ids = upload(a, original)
    job_id = uuid.UUID(ids["job_id"])
    with Session(api.engine) as db:
        source = db.scalar(select(SourceFile).where(SourceFile.job_id == job_id))
        key = source.storage_key
    storage.delete(key)
    assert process_job(job_id, storage=storage) == "storage_unavailable"
    assert api.scalar("SELECT state FROM ingestion_jobs WHERE id=:i", i=job_id) == "retryable_failed"
    assert api.scalar("SELECT finished_at FROM ingestion_jobs WHERE id=:i", i=job_id) is None
    assert api.scalar("SELECT extraction_finished_at FROM ingestion_jobs WHERE id=:i", i=job_id) is None
    storage.put(key, io.BytesIO(original), "text/markdown")
    with Session(api.engine) as db:
        job = db.get(IngestionJob, job_id)
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_job(job_id, storage=storage) == "awaiting_index"
    assert api.scalar("SELECT finished_at FROM ingestion_jobs WHERE id=:i", i=job_id) is None
    assert api.scalar("SELECT extraction_finished_at FROM ingestion_jobs WHERE id=:i", i=job_id) is not None

    assert api.scalar("SELECT last_error_code FROM ingestion_jobs WHERE id=:i", i=job_id) is None

    other = upload(a, b"# Cancel\n\nOther content.\n")
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                     {"i": uuid.UUID(other["item_id"])})
    assert process_job(uuid.UUID(other["job_id"]), storage=storage) == "cancelled"
    assert api.scalar("SELECT finished_at FROM ingestion_jobs WHERE id=:i", i=uuid.UUID(other["job_id"])) is not None
    assert api.scalar("SELECT extraction_finished_at FROM ingestion_jobs WHERE id=:i",
                      i=uuid.UUID(other["job_id"])) is None
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(other["item_id"])) == 0


def test_epoch_change_during_extraction_prevents_result_commit(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload(a, b"# Withdrawn\n\nPaper body.\n")
    item_id = uuid.UUID(ids["item_id"])

    class RevokingStorage:
        def __init__(self):
            self.opens = 0

        def open(self, key):
            self.opens += 1
            if self.opens == 2:  # after validation, before extract result commit
                with api.engine.begin() as conn:
                    conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": item_id})
            return storage.open(key)

    assert process_job(uuid.UUID(ids["job_id"]), storage=RevokingStorage()) == "cancelled"
    assert api.scalar("SELECT count(*) FROM sections WHERE item_id=:i", i=item_id) == 0
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=item_id) == 0


def test_parser_failure_retries_with_bounded_policy(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload(a, b"# Parser fixture\n\nText.\n")
    job_id = uuid.UUID(ids["job_id"])

    def failed_parser(_original):
        raise RuntimeError("private text must not appear in error")

    from app.ingest import worker
    original_extract = worker.extract
    monkeypatch.setattr(worker, "extract", failed_parser)
    assert process_job(job_id, storage=storage) == "processing_unavailable"
    assert api.scalar("SELECT state FROM ingestion_jobs WHERE id=:i", i=job_id) == "retryable_failed"
    assert api.scalar("SELECT finished_at FROM ingestion_jobs WHERE id=:i", i=job_id) is None
    assert api.scalar("SELECT extraction_finished_at FROM ingestion_jobs WHERE id=:i", i=job_id) is None
    assert "private text" not in str(a.get(f"/library/items/{ids['item_id']}").json())
    monkeypatch.setattr(worker, "extract", original_extract)
    with Session(api.engine) as db:
        job = db.get(IngestionJob, job_id)
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_job(job_id, storage=storage) == "awaiting_index"


def test_bibliography_only_is_not_awaiting_index(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload(a, b"# Bibliography only\n\n## References\n[1] A cited work.\n")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "needs_review"
    detail = a.get(f"/library/items/{ids['item_id']}").json()["item"]
    assert detail["jobs"][0]["state"] == "needs_review"
    assert "bibliography_boundary_uncertain" in detail["jobs"][0]["warnings"]
    assert detail["jobs"][0]["finished_at"] is not None
    assert detail["jobs"][0]["extraction_finished_at"] is None
    assert detail["jobs"][0]["processing_ms"] is not None
    assert detail["versions"][0]["coverage"] == "none"
    assert detail["content_status"] == "metadata_only"
    assert a.get(f"/library/items/{ids['item_id']}/chunks").json()["chunks"] == []
    assert len(a.get(f"/library/items/{ids['item_id']}/references").json()["references"]) == 1
