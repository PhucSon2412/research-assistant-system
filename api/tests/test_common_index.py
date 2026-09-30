"""M6b-2b2b common ingest/index of a published reviewed copy (PostgreSQL + MinIO).

A (Markdown and PDF) is indexed privately with the fake embedding provider of
``test_private_index``; the Admin approves the same bytes into common copies,
publishes them and starts indexing through ``/admin/common-copies/{id}/index``;
the real worker (``process_job``) runs the shared pipeline on the common file.
Nothing calls OpenRouter: ``settings_with_fake_key`` is an in-memory copy with a
synthetic key, and ``embed`` is replaced. Counts and timings here describe these
fixtures only, not a service level.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import publication
from app.ingest import indexer, worker
from app.ingest.embedding import EmbeddingError
from app.ingest.worker import due_job_ids, process_job
from tests.test_common_cleanup import DERIVED, derived_counts, derived_left, indexed, ledger, rows_of, wait_for_lock_wait
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import full_pdf, pdf_bytes, upload_pdf
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_publication import MISSING, publish, revoke, revoke_in_own_session
from tests.test_review_approval import approve
from tests.test_review_requests import submit, write
from tests.test_uploads import upload_api  # noqa: F401  (fixture)

MD_BODY = (b"# Common Index Fixture\n\n## Abstract\nA summary of the indexable method.\n\n## Methods\n"
           b"Indexable result is nine units.\n\n## References\n[1] A. Alpha. Linked Study. 2026.\n")
PDF_BODY = full_pdf()
INDEXABLE = {"audience": "instance_users", "original_download": True, "fulltext_search": True,
             "external_processing": True, "conditions": None}
KEY = re.compile(r"^promotion:[0-9a-f-]{36}:\d+$")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def copy_of(w, ids: dict, body: bytes, *, scope=INDEXABLE, published: bool = True) -> str:
    rid = submit(w.a, ids).json()["request"]["id"]
    response = approve(w.admin, w.api, rid, file_checksum_sha256=sha(body), scope=scope)
    assert response.status_code == 200, response.text
    item = response.json()["request"]["pending_publication_id"]
    if published:
        assert publish(w.admin, item, sha(body)).status_code == 200
    return item


def start(browser, item: str, **body):
    return write(browser, "POST", f"/admin/common-copies/{item}/index",
                 {"confirm_external_processing": True} | body)


def started(w, item: str) -> uuid.UUID:
    response = start(w.admin, item)
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["job"]["id"])


def jobs_of(w, item: str) -> list[tuple]:
    with w.api.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text("""SELECT id, state, idempotency_key, source_label, warnings,
            last_error_code, source_file_id, rights_epoch_at_start FROM ingestion_jobs WHERE item_id=:i
            ORDER BY created_at"""), {"i": uuid.UUID(item)}).all()]


def common_file(w, item: str) -> uuid.UUID:
    return w.api.scalar("SELECT id FROM source_files WHERE item_id=:i", i=uuid.UUID(item))


def item_row(w, item: str):
    with w.api.engine.connect() as conn:
        return conn.execute(text("""SELECT content_status, active_index_generation_id, active_version_id,
            publication_state FROM library_items WHERE id=:i"""), {"i": uuid.UUID(item)}).one()


def eligible_chunks(w, item: str) -> int:
    return w.api.scalar("""SELECT count(*) FROM chunks c JOIN sections s ON s.id=c.section_id
        WHERE c.item_id=:i AND s.kind IN ('body','abstract')""", i=uuid.UUID(item))


def distinct_texts(w, item: str) -> int:
    """Provider calls a full index needs: one per distinct eligible chunk text (hash) of the file."""
    return w.api.scalar("""SELECT count(DISTINCT c.chunk_hash) FROM chunks c JOIN sections s ON s.id=c.section_id
        WHERE c.item_id=:i AND s.kind IN ('body','abstract')""", i=uuid.UUID(item))


def budgets(w) -> dict:
    with w.api.engine.connect() as conn:
        return {"users": sorted(tuple(r) for r in conn.execute(text(
                    "SELECT day, user_id, committed_usd, reserved_usd FROM embedding_user_budget_days")).all()),
                "app": sorted(tuple(r) for r in conn.execute(text(
                    "SELECT day, committed_usd, reserved_usd FROM embedding_budget_days")).all())}


def replace_object(w, item: str, original: bytes) -> bytes:
    """Same size, different bytes: only the SHA-256 tells them apart."""
    altered = original[:-2] + bytes([original[-2] ^ 1]) + original[-1:]
    key = w.api.scalar("SELECT storage_key FROM source_files WHERE item_id=:i", i=uuid.UUID(item))
    w.storage.put(key, io.BytesIO(altered), "application/octet-stream")
    return altered


def restore_object(w, item: str, original: bytes) -> None:
    key = w.api.scalar("SELECT storage_key FROM source_files WHERE item_id=:i", i=uuid.UUID(item))
    w.storage.put(key, io.BytesIO(original), "application/octet-stream")


def assert_not_served(w, item: str) -> None:
    """Indexed is not opened: no chunk, search or RAG route serves the common copy to anyone."""
    file_id = str(common_file(w, item))
    for who, browser in (("a", w.a), ("b", w.b), ("admin", w.admin)):
        detail = browser.get(f"/library/items/{item}").json()["item"]
        assert detail["fulltext_search_available"] is False and detail["jobs"] == [], who
        for path in ("chunks", "references", "coverage", "metadata-candidates"):
            assert browser.get(f"/library/items/{item}/{path}").json() == MISSING, (who, path)
        for query in ("Indexable result nine units", "measured observation sufficient text"):
            found = write(browser, "POST", "/search", {"query": query, "mode": "keyword"})
            assert found.status_code == 200 and item not in found.text and file_id not in found.text, (who, query)
        assert write(browser, "POST", "/search", {"query": "Indexable", "mode": "keyword",
                                                  "item_id": item}).json() == MISSING, who
        assert write(browser, "POST", "/rag/ask", {"item_id": item, "question": "Indexable result?",
                                                   "mode": "keyword"}).json() == MISSING, who


@pytest.fixture
def w(upload_api, monkeypatch):
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    calls = fake_provider(monkeypatch)
    settings = settings_with_fake_key()
    private = {"md": indexed(storage, a, MD_BODY, settings), "pdf": indexed(storage, a, PDF_BODY, settings, pdf=True)}
    calls.clear()
    return SimpleNamespace(api=api, storage=storage, a=a, b=b, admin=admin, private=private, calls=calls,
                           settings=settings, monkeypatch=monkeypatch)


@pytest.mark.parametrize("kind", ["md", "pdf"])
def test_published_copy_indexes_through_the_shared_pipeline_and_stays_closed(w, kind):
    body = MD_BODY if kind == "md" else PDF_BODY
    private_before = rows_of(w.api, w.private[kind]["item_id"])
    budgets_before = budgets(w)
    copy = copy_of(w, w.private[kind], body)
    assert item_row(w, copy).content_status == "metadata_only"

    # Repeated and concurrent starts leave exactly one job; its key and label carry IDs and codes only.
    first = start(w.admin, copy).json()
    assert (first["created"], first["job"]["state"]) == (True, "received")
    again = start(w.admin, copy).json()
    assert (again["created"], again["job"]["id"]) == (False, first["job"]["id"])
    admins = [w.api.admin_browser() for _ in range(4)]
    barrier = Barrier(len(admins))

    def race(browser):
        barrier.wait()
        return start(browser, copy)

    with ThreadPoolExecutor(len(admins)) as pool:
        results = list(pool.map(race, admins))
    assert {r.status_code for r in results} == {200} and {r.json()["job"]["id"] for r in results} == {first["job"]["id"]}
    job = uuid.UUID(first["job"]["id"])
    [(jid, state, key, label, warnings, error, file_id, epoch)] = jobs_of(w, copy)
    assert (jid, state, label, error, file_id, epoch) == (job, "received", "admin_index", None, common_file(w, copy), 0)
    assert KEY.match(key) and str(common_file(w, copy)) in key
    # The common file keeps job_id NULL; the job names the file, and beat finds it.
    assert w.api.scalar("SELECT job_id FROM source_files WHERE item_id=:i", i=uuid.UUID(copy)) is None
    with Session(w.api.engine) as db:
        assert job in due_job_ids(db, w.settings)

    # Without a verified price (the default) the worker extracts, then waits: no provider call, still metadata_only.
    assert process_job(job, storage=w.storage) == "awaiting_index"
    assert w.calls == [] and item_row(w, copy).content_status == "metadata_only"
    counts = derived_counts(w.api, copy)
    assert counts["chunks"] > 0 and counts["sections"] > 0 and counts["reference_entries"] > 0
    assert (counts["pdf_pages"] > 0) is (kind == "pdf")
    detail = w.admin.get(f"/admin/common-copies/{copy}").json()["item"]["index"]
    assert (detail["eligibility"], detail["provider_ready"], detail["job"]["state"], detail["search_open"]) == (
        "indexable", False, "awaiting_index", False)

    # With a (fake) verified provider it finishes: active generation, indexed, job ready.
    assert process_job(job, w.settings, w.storage) == "ready"
    row = item_row(w, copy)
    assert row.content_status == "indexed" and row.active_index_generation_id is not None
    generation = w.api.scalar("SELECT index_version FROM index_generations WHERE id=:g", g=row.active_index_generation_id)
    assert generation == "common-keyword-vector-v1"
    # Every distinct eligible chunk text of the common file was embedded once, in its own call (identical
    # texts of one file share a call, as for private files); the references never.
    assert len(w.calls) == distinct_texts(w, copy) > 0
    assert not any("[1]" in c or "References" in c for c in w.calls)
    with w.api.engine.connect() as conn:
        calls = conn.execute(text("""SELECT owner_user_id, status, vector IS NOT NULL FROM embedding_calls
            WHERE source_file_id=:f"""), {"f": common_file(w, copy)}).all()
        private_chunk_ids = set(conn.scalars(text("SELECT id FROM chunks WHERE item_id=:i"),
                                             {"i": uuid.UUID(w.private[kind]["item_id"])}).all())
        common_chunk_ids = set(conn.scalars(text("SELECT id FROM chunks WHERE item_id=:i"),
                                            {"i": uuid.UUID(copy)}).all())
        entries = conn.scalars(text("""SELECT e.chunk_id FROM index_entries e WHERE e.generation_id=:g"""),
                               {"g": row.active_index_generation_id}).all()
    assert len(calls) == len(w.calls) and {tuple(c) for c in calls} == {(None, "completed", True)}
    # Same bytes and checksum as A's private copy, but separate rows: nothing copied or reused across items.
    assert private_chunk_ids and common_chunk_ids and not (private_chunk_ids & common_chunk_ids)
    assert set(entries) <= common_chunk_ids
    assert rows_of(w.api, w.private[kind]["item_id"]) == private_before
    # Charged to the application and embedding buckets only: no User's (or the Admin's) quota moved.
    after = budgets(w)
    assert after["users"] == budgets_before["users"]
    assert sum(r[1] for r in after["app"]) > sum(r[1] for r in budgets_before["app"])
    [(_, state, key, label, warnings, error, _, _)] = jobs_of(w, copy)
    assert (state, error) == ("ready", None)
    title = w.api.scalar("SELECT title FROM library_items WHERE id=:i", i=uuid.UUID(copy))
    for field in (key, label, str(warnings), str(error)):
        assert title not in field and sha(body) not in field and "paper" not in field
    audit = w.api.scalar("SELECT details FROM audit_events WHERE action='common_copy.index.start' AND outcome='success'")
    assert set(audit) == {"job_id", "source_file_id"}

    # Indexed is not opened: every normal route still refuses the copy's text.
    assert_not_served(w, copy)
    status = w.admin.get(f"/admin/common-copies/{copy}").json()["item"]
    assert status["content_status"] == "indexed" and status["fulltext_available"] is False
    assert "chưa mở tìm kiếm/hỏi đáp" in status["index"]["note"]
    assert start(w.admin, copy).json() == {"error": "already_indexed"}
    listed = w.b.get("/library/items?scope=common").json()["items"]
    assert [(i["content_status"], i["fulltext_search_available"]) for i in listed if i["id"] == copy] == [
        ("indexed", False)]


@pytest.mark.parametrize("scope, code", [
    ({"fulltext_search": False, "external_processing": False}, "fulltext_not_permitted"),
    ({"external_processing": False}, "external_processing_not_permitted"),
    ({"conditions": "Chỉ trích dẫn ngắn"}, "blocked_by_conditions"),
])
def test_rights_that_do_not_permit_indexing_are_refused_before_any_job(w, scope, code):
    copy = copy_of(w, w.private["md"], MD_BODY, scope=INDEXABLE | scope)
    response = start(w.admin, copy)
    assert (response.status_code, response.json()) == (409, {"error": code})
    assert jobs_of(w, copy) == [] and derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0) and w.calls == []
    assert w.admin.get(f"/admin/common-copies/{copy}").json()["item"]["index"]["eligibility"] == code
    assert w.api.scalar("""SELECT details->>'reason_code' FROM audit_events
        WHERE action='common_copy.index.start' AND outcome='denied'""") == code.removeprefix("common_")
    # A job written around the API (SQL) is refused by the worker too: no derived row, no provider call.
    with w.api.engine.begin() as conn:
        job = conn.scalar(text("""INSERT INTO ingestion_jobs(origin, scope_visibility, item_id, version_id,
            source_file_id, idempotency_key, rights_epoch_at_start)
            SELECT 'promotion', 'common', item_id, version_id, source_file_id, 'sql-bypass', 0
            FROM access_rights WHERE item_id=:i RETURNING id"""), {"i": uuid.UUID(copy)})
    assert process_job(job, w.settings, w.storage) == f"common_{code}"
    assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0) and w.calls == []
    assert item_row(w, copy).content_status == "metadata_only"


def test_download_permission_is_not_needed_and_other_states_are_refused(w):
    # permits_original_download=false does not block indexing; the two index permissions decide.
    no_download = copy_of(w, w.private["md"], MD_BODY, scope=INDEXABLE | {"original_download": False})
    job = started(w, no_download)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    assert process_job(job, w.settings, w.storage) == "ready"
    assert item_row(w, no_download).content_status == "indexed"
    assert w.a.get(f"/files/{common_file(w, no_download)}/original").json() == MISSING

    pdf_pending = copy_of(w, w.private["pdf"], PDF_BODY, published=False)
    assert start(w.admin, pdf_pending).json() == {"error": "common_copy_not_published"}
    assert revoke(w.admin, pdf_pending).status_code == 200
    assert start(w.admin, pdf_pending).json() == {"error": "common_copy_revoked"}
    legacy = str(w.api.add_item(owner=None, title="Legacy common"))
    for item in (legacy, w.private["md"]["item_id"], str(uuid.uuid4()), "not-a-uuid"):
        assert start(w.admin, item).json() == MISSING, item
    # Epoch moved after the rights were recorded (not a revoke): rights are not current.
    other = upload(w.a, b"## Methods\nAnother indexable body text.\n")
    epoch_copy = copy_of(w, other, b"## Methods\nAnother indexable body text.\n")
    with w.api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch = rights_epoch + 1 WHERE id=:i"),
                     {"i": uuid.UUID(epoch_copy)})
    assert start(w.admin, epoch_copy).json() == {"error": "rights_not_current"}
    # Session, role, CSRF, Origin and confirmation.
    assert start(w.a, epoch_copy).status_code == 403
    assert write(w.admin, "POST", f"/admin/common-copies/{epoch_copy}/index",
                 {"confirm_external_processing": True}, csrf=False).status_code == 403
    assert write(w.admin, "POST", f"/admin/common-copies/{epoch_copy}/index",
                 {"confirm_external_processing": True}, origin=None).status_code == 403
    assert write(w.admin, "POST", f"/admin/common-copies/{epoch_copy}/index", {}).status_code == 422
    assert start(w.admin, epoch_copy, extra="x").status_code == 422
    assert all(jobs_of(w, c) == [] for c in (pdf_pending, legacy, epoch_copy))
    assert len(w.calls) == distinct_texts(w, no_download)  # only the permitted copy reached the provider


def test_pdf_without_enough_body_is_not_indexed_and_not_retried(w):
    thin = pdf_bytes([["Cover", "Abstract", "Summary"], ["References", "[1] Source."]])
    ids = upload_pdf(w.a, thin)
    copy = copy_of(w, ids, thin)
    job = started(w, copy)
    assert process_job(job, w.settings, w.storage) == "needs_review"
    assert w.calls == [] and item_row(w, copy).content_status == "metadata_only"
    assert w.api.scalar("SELECT count(*) FROM index_generations WHERE item_id=:i", i=uuid.UUID(copy)) == 0
    assert start(w.admin, copy).json() == {"error": "extraction_rejected"}
    assert len(jobs_of(w, copy)) == 1


def test_rate_limit_retry_and_a_new_job_reuse_completed_embeddings_and_extraction(w):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    assert eligible_chunks(w, copy) >= 2

    def limited(n):
        if n == 2:
            raise EmbeddingError("provider_rate_limited", definitely_rejected=True)

    fake_provider(w.monkeypatch, action=limited)
    assert process_job(job, w.settings, w.storage) == "provider_rate_limited"
    assert start(w.admin, copy).json()["created"] is False  # the unfinished job is the retry
    with w.api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET next_attempt_at=NULL WHERE id=:j"), {"j": job})
    calls = fake_provider(w.monkeypatch)
    assert process_job(job, w.settings, w.storage) == "ready"
    # Only the chunk that was never embedded is sent again; one call row per chunk, no duplicate.
    assert len(calls) == eligible_chunks(w, copy) - 1
    assert w.api.scalar("SELECT count(*) FROM embedding_calls WHERE source_file_id=:f", f=common_file(w, copy)) == \
        eligible_chunks(w, copy)

    # A job that ends terminal after one chunk, then a new job: extraction rows and the completed
    # embedding are reused, nothing is inserted twice.
    other_body = b"## Abstract\nSecond fixture abstract.\n\n## Methods\nSecond fixture method body.\n"
    other = copy_of(w, upload(w.a, other_body), other_body)
    first = started(w, other)
    assert process_job(first, storage=w.storage) == "awaiting_index"
    real, seen = indexer._verify_object, []

    def fails_third(source, storage, settings):
        seen.append(1)
        if len(seen) == 3:  # the check before the second chunk's call
            raise worker.StepError("checksum_mismatch")
        return real(source, storage, settings)

    w.monkeypatch.setattr(indexer, "_verify_object", fails_third)
    calls = fake_provider(w.monkeypatch)
    assert process_job(first, w.settings, w.storage) == "checksum_mismatch"
    assert len(calls) == 1
    w.monkeypatch.setattr(indexer, "_verify_object", real)
    rows = {t: rows_of(w.api, other)[t] for t in ("sections", "chunks", "metadata_candidates", "reference_entries")}
    second = started(w, other)
    assert second != first and [KEY.match(j[2]) is not None for j in jobs_of(w, other)] == [True, True]
    assert jobs_of(w, other)[1][2].endswith(":2")
    calls = fake_provider(w.monkeypatch)
    assert process_job(second, storage=w.storage) == "awaiting_index"
    assert process_job(second, w.settings, w.storage) == "ready"
    assert len(calls) == 1  # the first chunk's completed call was reused
    assert {t: rows_of(w.api, other)[t] for t in rows} == rows
    assert w.api.scalar("SELECT count(*) FROM embedding_calls WHERE source_file_id=:f", f=common_file(w, other)) == 2


@pytest.mark.parametrize("when", ["before_extraction", "during_call"])
def test_same_size_replacement_of_the_common_object_is_caught(w, when):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    if when == "before_extraction":
        replace_object(w, copy, MD_BODY)
        assert process_job(job, w.settings, w.storage) == "checksum_mismatch"
        assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0) and w.calls == []
    else:
        assert process_job(job, storage=w.storage) == "awaiting_index"
        fake_provider(w.monkeypatch, action=lambda n: replace_object(w, copy, MD_BODY))
        assert process_job(job, w.settings, w.storage) == "checksum_mismatch"
        assert w.api.scalar("""SELECT count(*) FROM index_entries e JOIN index_generations g ON g.id=e.generation_id
            WHERE g.item_id=:i""", i=uuid.UUID(copy)) == 0
        # The response may have been billed: the ceiling is kept, never sent again automatically.
        assert w.api.scalar("SELECT status FROM embedding_calls WHERE source_file_id=:f",
                            f=common_file(w, copy)) == "uncertain"
    assert item_row(w, copy).content_status == "metadata_only"
    assert item_row(w, copy).active_index_generation_id is None


def test_revoke_before_any_provider_call_cancels_without_sending(w):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    assert revoke(w.admin, copy).status_code == 200
    assert process_job(job, w.settings, w.storage) == "cancelled"
    assert w.calls == [] and jobs_of(w, copy)[0][1] == "cancelled"
    assert item_row(w, copy).active_index_generation_id is None


def test_revoke_during_the_provider_call_stores_and_publishes_nothing(w):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    calls = fake_provider(w.monkeypatch, action=lambda n: revoke_in_own_session(w.api, copy))
    assert process_job(job, w.settings, w.storage) == "cancelled"
    assert len(calls) == 1  # sent before t_rev; it cannot be recalled from the provider
    with w.api.engine.connect() as conn:
        call = conn.execute(text("""SELECT status, cost_status, vector IS NULL FROM embedding_calls
            WHERE source_file_id=:f"""), {"f": common_file(w, copy)}).one()
    assert tuple(call) == ("uncertain", "unknown_reserved_ceiling", True)
    assert item_row(w, copy).content_status == "metadata_only" and item_row(w, copy).active_index_generation_id is None
    assert w.api.scalar("SELECT count(*) FROM index_generations WHERE item_id=:i AND state='active'",
                        i=uuid.UUID(copy)) == 0


def _revoke_in_thread(w, copy, outcome):
    def run():
        try:
            revoke_in_own_session(w.api, copy)
            outcome["revoked"] = True
        except Exception as exc:  # pragma: no cover - reported by the assertion
            outcome["revoke_error"] = repr(exc)
    thread = Thread(target=run)
    thread.start()
    return thread


def test_revoke_queued_just_before_publish_wins_and_nothing_is_published(w):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    total, real, outcome, threads = eligible_chunks(w, copy), indexer._insert_entry, {}, []

    def last_entry(db, generation, chunk, call):
        real(db, generation, chunk, call)
        if db.execute(text("SELECT count(*) FROM index_entries WHERE generation_id=:g"),
                      {"g": generation.id}).scalar() + 1 == total and not threads:
            # The item is locked by this transaction: the revoke queues for it, ahead of the
            # worker's next (publishing) transaction.
            threads.append(_revoke_in_thread(w, copy, outcome))
            wait_for_lock_wait(w.api)

    w.monkeypatch.setattr(indexer, "_insert_entry", last_entry)
    assert process_job(job, w.settings, w.storage) == "cancelled"
    threads[0].join(10)
    assert outcome == {"revoked": True}
    row = item_row(w, copy)
    assert (row.publication_state, row.content_status, row.active_index_generation_id) == ("revoked", "metadata_only",
                                                                                          None)
    assert w.api.scalar("SELECT count(*) FROM index_generations WHERE item_id=:i AND state='active'",
                        i=uuid.UUID(copy)) == 0
    assert publication.cleanup_revoked(w.api.engine, w.storage, w.api.clock.now, 50) == {"deleted": 1}
    assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0)


def test_publish_holding_the_lock_wins_then_revoke_leaves_the_index_at_t_rev(w):
    copy = copy_of(w, w.private["pdf"], PDF_BODY)
    job = started(w, copy)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    real, outcome, threads = indexer._publish, {}, []

    def publish_waited(db, job_row, item, generation, chunks, settings):
        threads.append(_revoke_in_thread(w, copy, outcome))
        wait_for_lock_wait(w.api)  # the revoke waits for this publishing transaction
        real(db, job_row, item, generation, chunks, settings)

    w.monkeypatch.setattr(indexer, "_publish", publish_waited)
    assert process_job(job, w.settings, w.storage) == "ready"
    threads[0].join(10)
    assert outcome == {"revoked": True}
    # Indexed for the instant between the two commits (and served by no route even then); from t_rev
    # the copy is revoked and names no active generation.
    row = item_row(w, copy)
    assert (row.publication_state, row.content_status, row.active_index_generation_id) == ("revoked", "metadata_only",
                                                                                          None)
    assert publication.cleanup_revoked(w.api.engine, w.storage, w.api.clock.now, 50) == {"deleted": 1}
    assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0)


def test_worker_holding_item_and_job_first_makes_revoke_wait_then_ends_cancelled(w):
    """Order 1: the worker's step holds item + job; the revoke waits, then flags the job."""
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    holding, release, outcome = Event(), Event(), {}
    real = worker.reconcile_references

    def held(db, **kwargs):  # inside the extraction transaction: item then job locked
        real(db, **kwargs)
        holding.set()
        assert release.wait(10)

    w.monkeypatch.setattr(worker, "reconcile_references", held)
    result = {}
    runner = Thread(target=lambda: result.setdefault("worker", process_job(job, storage=w.storage)))
    runner.start()
    assert holding.wait(10)
    revoker = _revoke_in_thread(w, copy, outcome)
    wait_for_lock_wait(w.api)
    assert item_row(w, copy).publication_state == "listed"
    release.set()
    runner.join(20)
    revoker.join(20)
    assert outcome == {"revoked": True} and result == {"worker": "cancelled"}
    assert jobs_of(w, copy)[0][1] == "cancelled"
    assert derived_counts(w.api, copy)["sections"] > 0  # committed before t_rev
    assert publication.cleanup_revoked(w.api.engine, w.storage, w.api.clock.now, 50) == {"deleted": 1}
    assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0)


def test_revoke_holding_the_item_first_makes_the_worker_wait_without_deadlock(w):
    """Order 2: the revoke holds the item (not yet the job); the worker locks item before job, so it waits.

    With the old order (job, then item) the worker held the job while the revoke went on to flag it:
    a lock cycle PostgreSQL breaks by aborting one side.
    """
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    holding, release, outcome, result = Event(), Event(), {}, {}
    real = publication._revoke_rows

    def held(db, *args, **kwargs):  # item locked FOR UPDATE and updated; jobs not touched yet
        real(db, *args, **kwargs)
        holding.set()
        assert release.wait(10)

    w.monkeypatch.setattr(publication, "_revoke_rows", held)
    revoker = _revoke_in_thread(w, copy, outcome)
    assert holding.wait(10)
    runner = Thread(target=lambda: result.setdefault("worker", process_job(job, storage=w.storage)))
    runner.start()
    wait_for_lock_wait(w.api)  # the worker waits for the item, holding nothing the revoke needs
    release.set()
    revoker.join(20)
    runner.join(20)
    assert outcome == {"revoked": True} and result == {"worker": "cancelled"}
    assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0) and w.calls == []


def test_cleanup_removes_rows_of_the_real_common_pipeline_and_nothing_private(w):
    copies = {kind: copy_of(w, w.private[kind], MD_BODY if kind == "md" else PDF_BODY) for kind in ("md", "pdf")}
    for copy in copies.values():
        job = started(w, copy)
        assert process_job(job, storage=w.storage) == "awaiting_index"
        assert process_job(job, w.settings, w.storage) == "ready"
    private = {kind: rows_of(w.api, w.private[kind]["item_id"]) for kind in copies}
    ledgers = {kind: ledger(w.api, copy) for kind, copy in copies.items()}
    for kind, copy in copies.items():
        assert all(n > 0 for t, n in derived_counts(w.api, copy).items()
                   if t not in ("pdf_pages", "reference_citation_links", "metadata_candidates")), kind
        assert revoke(w.admin, copy).status_code == 200
        assert_not_served_after_revoke(w, copy)
    assert publication.cleanup_revoked(w.api.engine, w.storage, w.api.clock.now, 50) == {"deleted": 2}
    for kind, copy in copies.items():
        assert derived_counts(w.api, copy) == dict.fromkeys(DERIVED, 0) and derived_left(w.api, copy) == []
        assert ledger(w.api, copy) == ledgers[kind] and all(row[-1] is None for row in ledgers[kind])
        assert w.api.scalar("""SELECT bool_and(vector IS NULL AND chunk_hash IS NULL) FROM embedding_calls
            WHERE source_file_id=:f""", f=common_file(w, copy))
        # The job survives cleanup with IDs and codes only.
        [(_, state, key, label, *_rest)] = jobs_of(w, copy)
        assert state == "ready" and KEY.match(key) and label == "admin_index"
    for kind in copies:
        assert rows_of(w.api, w.private[kind]["item_id"]) == private[kind], kind
    hits = write(w.a, "POST", "/search", {"query": "Indexable result nine units", "mode": "keyword"}).json()["hits"]
    assert {h["item_id"] for h in hits} == {w.private["md"]["item_id"]}


def assert_not_served_after_revoke(w, copy):
    for browser in (w.a, w.b, w.admin):
        assert browser.get(f"/library/items/{copy}").json() == MISSING
        assert write(browser, "POST", "/search", {"query": "Indexable", "mode": "keyword",
                                                  "item_id": copy}).json() == MISSING


def test_common_calls_are_charged_to_the_application_only(w):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    assert process_job(job, storage=w.storage) == "awaiting_index"
    # A User's quota of ~0 would stop that User's private indexing; the common copy is not charged to anyone.
    tiny_user = w.settings.model_copy(deep=True)
    tiny_user.budget.user_embedding_daily_usd = 0.00000001
    other = upload(w.b, b"## Methods\nB private text under a tiny quota.\n")
    assert process_job(uuid.UUID(other["job_id"]), storage=w.storage) == "awaiting_index"
    assert process_job(uuid.UUID(other["job_id"]), tiny_user, w.storage) == "embedding_user_quota_exhausted"
    assert process_job(job, tiny_user, w.storage) == "ready"
    # The application's embedding bucket does bind it, before any call.
    tiny_app = w.settings.model_copy(deep=True)
    tiny_app.budget.embedding_daily_usd = 0.00000001
    body = b"## Methods\nThird fixture body under a spent bucket.\n"
    third = copy_of(w, upload(w.a, body), body)
    job3 = started(w, third)
    assert process_job(job3, storage=w.storage) == "awaiting_index"
    calls = fake_provider(w.monkeypatch)
    assert process_job(job3, tiny_app, w.storage) == "embedding_budget_exhausted"
    assert calls == [] and jobs_of(w, third)[0][1] == "retryable_failed"
    # The DB keeps the owner equal to the item's scope.
    file_id, private_file = common_file(w, copy), uuid.UUID(w.private["md"]["file_id"])
    b_id = w.api.scalar("SELECT id FROM users WHERE email='b@example.org'")
    insert = """INSERT INTO embedding_calls(source_file_id, owner_user_id, model_id, provider, pipeline_version,
        chunk_hash, status, reserved_usd, cost_status, reserved_day) VALUES (:f, :o, 'm', 'p', 'x', repeat('d', 64),
        'reserved', 0, 'unknown', CURRENT_DATE)"""
    for f, owner, message in ((file_id, b_id, "charged to the application"),
                              (private_file, b_id, "charged to the item owner"),
                              (private_file, None, "charged to the item owner")):
        with pytest.raises(IntegrityError, match=message), w.api.engine.begin() as conn:
            conn.execute(text(insert), {"f": f, "o": owner})
    with pytest.raises(IntegrityError, match="keeps its budget owner"), w.api.engine.begin() as conn:
        conn.execute(text("UPDATE embedding_calls SET owner_user_id=:o WHERE source_file_id=:f"),
                     {"o": b_id, "f": file_id})
    assert Decimal(0) <= w.api.scalar("SELECT sum(committed_usd) FROM embedding_budget_days")


def test_schema_pins_promotion_jobs_and_gates_the_indexed_state(w):
    copy = copy_of(w, w.private["md"], MD_BODY)
    job = started(w, copy)
    item = uuid.UUID(copy)
    p = {"i": item, "f": common_file(w, copy), "pf": uuid.UUID(w.private["md"]["file_id"]), "j": job}

    def fails(message, *statements):
        with pytest.raises(IntegrityError, match=message), w.api.engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement), p)

    fails("promotion_pins_file", """INSERT INTO ingestion_jobs(origin, scope_visibility, item_id, idempotency_key,
        rights_epoch_at_start) VALUES ('promotion', 'common', :i, 'x', 0)""")
    fails("reads the reviewed file", """INSERT INTO ingestion_jobs(origin, scope_visibility, item_id, version_id,
        source_file_id, idempotency_key, rights_epoch_at_start) SELECT 'promotion', 'common', :i, version_id,
        id, 'x', 0 FROM source_files WHERE id=:pf""")  # a file of another item: FK or trigger refuses
    fails("records the rights epoch", """INSERT INTO ingestion_jobs(origin, scope_visibility, item_id, version_id,
        source_file_id, idempotency_key) SELECT 'promotion', 'common', item_id, version_id, source_file_id, 'x'
        FROM access_rights WHERE item_id=:i""")
    fails("uq_ingestion_jobs_live_promotion_file", """INSERT INTO ingestion_jobs(origin, scope_visibility, item_id,
        version_id, source_file_id, idempotency_key, rights_epoch_at_start) SELECT 'promotion', 'common', item_id,
        version_id, source_file_id, 'second', 0 FROM access_rights WHERE item_id=:i""")
    for column, value in (("rights_epoch_at_start", "5"), ("idempotency_key", "'changed'"),
                          ("source_file_id", "NULL")):
        fails("keeps its pinned", f"UPDATE ingestion_jobs SET {column}={value} WHERE id=:j")

    assert process_job(job, storage=w.storage) == "awaiting_index"
    assert process_job(job, w.settings, w.storage) == "ready"
    generation = item_row(w, copy).active_index_generation_id
    p["g"] = generation
    # Leaving the index open by a revoke that does not clear it is refused at commit.
    fails(r"indexed only under .*\(not_listed\)",
          "UPDATE library_items SET publication_state='revoked', rights_epoch=rights_epoch+1 WHERE id=:i",
          """UPDATE access_rights SET revoked_at=now(), revocation_reason_code='other', rights_epoch_at_revoke=1,
             revoked_by_user_id=(SELECT id FROM users WHERE role='admin') WHERE item_id=:i""",
          "UPDATE common_publications SET revoked_at=now() WHERE item_id=:i",
          """INSERT INTO deletion_tombstones(target_kind, item_id, reason_code, prior_publication_state, rights_epoch,
             recorded_at, recorded_by_user_id, purge_due_at) SELECT 'common_copy', :i, 'other', 'listed', 1, now(),
             id, now() + interval '1 day' FROM users WHERE role='admin'""")
    fails(r"\(generation_mismatch\)", "UPDATE index_generations SET state='superseded' WHERE id=:g",
          "UPDATE library_items SET content_status='indexed' WHERE id=:i")  # touches the gate columns
    fails(r"\(generation_mismatch\)", "UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i")
    fails(r"\(indexed_without_generation\)", "UPDATE library_items SET active_index_generation_id=NULL WHERE id=:i")
    # A listed copy whose rights do not permit indexing cannot be made indexed by SQL either.
    body = b"## Methods\nConditioned copy body.\n"
    conditioned = copy_of(w, upload(w.a, body), body, scope=INDEXABLE | {"conditions": "Chỉ trích dẫn ngắn"})
    p["c"] = uuid.UUID(conditioned)
    fails(r"\(indexed_without_generation\)|\(generation_mismatch\)",
          "UPDATE library_items SET content_status='indexed', active_version_id=(SELECT id FROM paper_versions"
          " WHERE item_id=:c) WHERE id=:c")
    pending_body = b"## Methods\nPending copy body.\n"
    pending = copy_of(w, upload(w.a, pending_body), pending_body, published=False)
    p["pending"] = uuid.UUID(pending)
    fails("indexed only while listed", "UPDATE library_items SET content_status='indexed', active_version_id="
          "(SELECT id FROM paper_versions WHERE item_id=:pending) WHERE id=:pending")
    assert item_row(w, copy).content_status == "indexed"
