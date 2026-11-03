"""M6b-2b2a cleanup of a revoked common copy's derived full-text rows (PostgreSQL + MinIO).

``clone_derived`` writes onto the common copy what the real private pipeline
produced for the same bytes (Markdown and PDF: sections, chunks, PDF pages,
references, citation links, metadata candidates, a job, an active generation,
index entries and embedding calls with vectors). Written before the common
pipeline existed, it now follows its contract (M6b-2b2b, 0025): the promotion
job pins the copy's file, the calls are charged to the application (no owner),
the rights permit indexing, and only a listed copy is made ``indexed`` (a
pending copy cannot be). ``test_common_index`` runs cleanup on rows from the
real common pipeline. Each copy's texts carry a distinct canary. Private
indexing uses the fake embedding provider of ``test_private_index``; nothing
calls OpenRouter.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from threading import Event, Thread

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import citations, publication
from app.ingest.worker import process_job
from tests.test_citations import harvest, oai_page
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import full_pdf, upload_pdf
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_publication import MISSING, content_routes, publish, revoke, revoke_in_own_session
from tests.test_review_approval import approve
from tests.test_review_requests import submit, write
from tests.original_fixtures import upload_api, base_upload_api  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401

ARXIV_ID = "2609.00011"
MD_BODY = (b"# Cleanup Fixture\n\n## Abstract\nA summary of the reviewable method.\n\n## Methods\n"
           b"Reviewable result is seven units.\n\n## References\n"
           b"[1] A. Alpha. Exact Linking Study. arXiv:2609.00011, 2026.\n[2] C. Gamma. Journal Paper. 2019.\n")
PDF_BODY = full_pdf()
CLOSED = "derived rows cannot be written for a revoked common copy"
ITEM_TABLES = ("sections", "chunks", "pdf_pages", "reference_entries", "metadata_candidates", "index_generations")
DERIVED = ITEM_TABLES + ("index_entries", "reference_citation_links")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def indexed(storage, browser, body: bytes, settings, *, pdf: bool = False) -> dict:
    ids = upload_pdf(browser, body) if pdf else upload(browser, body)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    return ids


# Rights that permit indexing (0025 gate); download stays off, so the original route stays closed as before.
INDEXABLE_SCOPE = {"audience": "instance_users", "original_download": False, "fulltext_search": True,
                   "external_processing": True, "conditions": None}


def common_copy(api, owner, admin, ids, body: bytes) -> str:
    rid = submit(owner, ids).json()["request"]["id"]
    response = approve(admin, api, rid, file_checksum_sha256=sha(body), scope=INDEXABLE_SCOPE)
    assert response.status_code == 200, response.text
    return response.json()["request"]["pending_publication_id"]


def clone_derived(api, src_item: str, copy: str, canary: str) -> None:
    """Write onto ``copy`` the private pipeline output of ``src_item`` (same bytes), texts + canary."""
    with api.setup_engine.begin() as conn:
        dst = uuid.UUID(copy)
        version, file_id = conn.execute(text("SELECT version_id, id FROM source_files WHERE item_id=:i"),
                                        {"i": dst}).one()
        p = {"src": uuid.UUID(src_item), "dst": dst, "dv": version, "df": file_id, "c": f" {canary}",
             "adm": conn.scalar(text("SELECT id FROM users WHERE role='admin'")),
             "key": f"m6b2b2a-{uuid.uuid4()}", "flight": sha(canary.encode())}
        p["job"] = conn.scalar(text("""INSERT INTO ingestion_jobs(origin, scope_visibility, item_id, version_id,
            source_file_id, idempotency_key, state, rights_epoch_at_start, attempt_count, lease_token, lease_until)
            VALUES ('promotion', 'common', :dst, :dv, :df, :key, 'indexing', 0, 1, gen_random_uuid(),
                    now() + interval '1 hour') RETURNING id"""), p)
        for statement in (
            "INSERT INTO ingest_step_attempts(job_id, step, attempt_no, outcome) VALUES (:job, 'index', 1, 'running')",
            "CREATE TEMP TABLE m(old uuid PRIMARY KEY, new uuid NOT NULL DEFAULT gen_random_uuid()) ON COMMIT DROP",
            """INSERT INTO m(old) SELECT id FROM sections WHERE item_id=:src UNION ALL
               SELECT id FROM chunks WHERE item_id=:src UNION ALL SELECT id FROM reference_entries WHERE item_id=:src
               UNION ALL SELECT id FROM index_generations WHERE item_id=:src AND state='active' UNION ALL
               SELECT id FROM embedding_calls WHERE source_file_id IN (SELECT id FROM source_files WHERE item_id=:src)""",
            """INSERT INTO sections(id, item_id, version_id, source_file_id, kind, heading, ordinal, text, start_offset,
               end_offset, start_line, end_line, pipeline_version, locator_kind, start_page, end_page, locator_detail)
               SELECT m.new, :dst, :dv, :df, kind, heading, ordinal, text || :c, start_offset, end_offset, start_line,
               end_line, pipeline_version, locator_kind, start_page, end_page, locator_detail
               FROM sections s JOIN m ON m.old = s.id""",
            """INSERT INTO chunks(id, item_id, version_id, source_file_id, section_id, ordinal, text, start_offset,
               end_offset, start_line, end_line, chunk_hash, pipeline_version, locator_kind, start_page, end_page,
               locator_detail)
               SELECT m.new, :dst, :dv, :df, ms.new, ordinal, c.text || :c, start_offset, end_offset, start_line,
               end_line, encode(sha256(convert_to(c.text || :c, 'UTF8')), 'hex'), pipeline_version, locator_kind,
               start_page, end_page, locator_detail FROM chunks c JOIN m ON m.old = c.id JOIN m ms ON ms.old = c.section_id""",
            """INSERT INTO pdf_pages(item_id, version_id, source_file_id, page_number, text, width, height, role,
               body_chars, body_page, qualifies, error_code, layout_mode, pipeline_version)
               SELECT :dst, :dv, :df, page_number, text || :c, width, height, role, body_chars, body_page, qualifies,
               error_code, layout_mode, pipeline_version FROM pdf_pages WHERE item_id=:src""",
            """INSERT INTO reference_entries(id, item_id, version_id, source_file_id, ordinal, raw_citation, fields,
               confidence, provenance, locator_kind, page_number, start_offset, end_offset, pipeline_version)
               SELECT m.new, :dst, :dv, :df, ordinal, raw_citation || :c, fields, confidence, provenance, locator_kind,
               page_number, start_offset, end_offset, pipeline_version FROM reference_entries r JOIN m ON m.old = r.id""",
            """INSERT INTO reference_citation_links(reference_id, matcher_version, raw_citation_sha256, match_status,
               match_method, id_form, arxiv_id, cited_version, id_start_offset, id_end_offset, discovery_paper_id)
               SELECT m.new, matcher_version, raw_citation_sha256, match_status, match_method, id_form, arxiv_id,
               cited_version, id_start_offset, id_end_offset, discovery_paper_id
               FROM reference_citation_links l JOIN m ON m.old = l.reference_id""",
            """INSERT INTO metadata_candidates(item_id, version_id, source_file_id, field_name, value, start_offset,
               end_offset, pipeline_version, locator_kind, start_page)
               SELECT :dst, :dv, :df, field_name, value || :c, start_offset, end_offset, pipeline_version,
               locator_kind, start_page FROM metadata_candidates WHERE item_id=:src""",
            """INSERT INTO index_generations(id, item_id, version_id, source_file_id, job_id, model_id, provider,
               dimensions, pipeline_version, index_version, expected_chunks, rights_epoch, state, activated_at)
               SELECT m.new, :dst, :dv, :df, :job, model_id, provider, dimensions, pipeline_version, index_version,
               expected_chunks, 0, 'active', activated_at FROM index_generations g JOIN m ON m.old = g.id""",
            """INSERT INTO embedding_calls(id, source_file_id, owner_user_id, model_id, provider, pipeline_version,
               chunk_hash, status, vector, dimensions, input_tokens, reserved_usd, cost_usd, cost_status,
               reserved_day, finished_at, response_model_id, response_provider)
               SELECT DISTINCT ON (mc.new) mc.new, :df, NULL, ec.model_id, ec.provider, ec.pipeline_version, nc.chunk_hash, ec.status,
               ('[' || array_to_string(array_fill(0.7::float8, ARRAY[ec.dimensions]), ',') || ']')::vector,
               ec.dimensions, ec.input_tokens, ec.reserved_usd, ec.cost_usd, ec.cost_status, ec.reserved_day,
               ec.finished_at, ec.response_model_id, ec.response_provider
               FROM embedding_calls ec JOIN m mc ON mc.old = ec.id JOIN index_entries e ON e.embedding_call_id = ec.id
               JOIN m mch ON mch.old = e.chunk_id JOIN chunks nc ON nc.id = mch.new""",
            # A request still in flight at cleanup: reserved, no vector yet.
            """INSERT INTO embedding_calls(source_file_id, owner_user_id, model_id, provider, pipeline_version,
               chunk_hash, status, reserved_usd, cost_status, reserved_day, lease_until)
               SELECT :df, NULL, model_id, provider, pipeline_version, :flight, 'reserved', 0.00001, 'unknown',
               CURRENT_DATE, now() + interval '1 hour' FROM index_generations WHERE item_id=:dst""",
            """INSERT INTO index_entries(generation_id, chunk_id, embedding_call_id, chunk_hash, text)
               SELECT mg.new, mch.new, mc.new, nc.chunk_hash, nc.text FROM index_entries e
               JOIN m mg ON mg.old = e.generation_id JOIN m mch ON mch.old = e.chunk_id
               JOIN chunks nc ON nc.id = mch.new JOIN m mc ON mc.old = e.embedding_call_id""",
            # Only a listed copy is indexed (0025 gate); a pending one keeps its rows without an active pointer.
            """UPDATE library_items SET active_index_generation_id =
               (SELECT id FROM index_generations WHERE item_id=:dst), content_status='indexed', active_version_id=:dv
               WHERE id=:dst AND publication_state='listed'"""):
            conn.execute(text(statement), p)


def rows_of(api, item_id: str) -> dict[str, list]:
    """Every row that belongs to the item: itself, version/file/job, derived rows and its embedding ledger."""
    i = uuid.UUID(item_id)
    queries = {t: f"SELECT * FROM {t} WHERE item_id=:i" for t in ITEM_TABLES} | {
        "index_entries": "SELECT e.* FROM index_entries e JOIN index_generations g ON g.id=e.generation_id"
                         " WHERE g.item_id=:i",
        "reference_citation_links": "SELECT l.* FROM reference_citation_links l JOIN reference_entries r"
                                    " ON r.id=l.reference_id WHERE r.item_id=:i",
        "embedding_calls": "SELECT c.* FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id"
                           " WHERE f.item_id=:i",
        "item": "SELECT * FROM library_items WHERE id=:i", "versions": "SELECT * FROM paper_versions WHERE item_id=:i",
        "files": "SELECT * FROM source_files WHERE item_id=:i", "jobs": "SELECT * FROM ingestion_jobs WHERE item_id=:i",
        "attempts": "SELECT a.* FROM ingest_step_attempts a JOIN ingestion_jobs j ON j.id=a.job_id WHERE j.item_id=:i"}
    with api.setup_engine.connect() as conn:
        return {name: sorted(tuple(map(str, r)) for r in conn.execute(text(sql), {"i": i}).all())
                for name, sql in queries.items()}


def derived_counts(api, item_id: str) -> dict[str, int]:
    rows = rows_of(api, item_id)
    return {t: len(rows[t]) for t in DERIVED}


def derived_left(api, item_id: str) -> list[str]:
    return api.scalar("SELECT ras_common_copy_derived_left(:i)", i=uuid.UUID(item_id))


def ledger(api, item_id: str) -> list[tuple]:
    """The cost columns of the copy's embedding calls, which cleanup keeps."""
    with api.setup_engine.connect() as conn:
        return sorted(tuple(r) for r in conn.execute(text("""SELECT c.id, c.model_id, c.provider, c.response_model_id,
            c.response_provider, c.pipeline_version, c.status, c.dimensions, c.input_tokens, c.reserved_usd,
            c.cost_usd, c.cost_status, c.reserved_day, c.owner_user_id FROM embedding_calls c
            JOIN source_files f ON f.id=c.source_file_id WHERE f.item_id=:i"""), {"i": uuid.UUID(item_id)}).all())


def file_of(api, item_id: str) -> str:
    return str(api.scalar("SELECT id FROM source_files WHERE item_id=:i", i=uuid.UUID(item_id)))


def paper(api) -> str:
    return str(api.scalar("SELECT id FROM discovery_papers WHERE remote_id=:r", r=ARXIV_ID))


def assert_blocked(api, browsers, copy: str, canary: str, *, listed: bool = False) -> None:
    """No normal route serves the copy's derived text, whatever rows still exist; nor, once revoked, the copy."""
    file_id, target = file_of(api, copy), paper(api)
    for who, browser in browsers.items():
        detail = browser.get(f"/library/items/{copy}")
        if listed:  # published metadata only: no chunk text, no canary
            assert detail.status_code == 200 and canary not in detail.text, who
        else:
            assert detail.json() == MISSING, who
        for path in content_routes(copy, file_id):
            assert browser.get(path).json() == MISSING, (who, path)
        for query in (canary, "Reviewable seven units", "measured observation"):
            found = write(browser, "POST", "/search", {"query": query, "mode": "keyword"})
            assert found.status_code == 200 and copy not in found.text and canary not in found.text, (who, query)
        assert write(browser, "POST", "/search", {"query": canary, "mode": "keyword",
                                                  "item_id": copy}).json() == MISSING, who
        assert write(browser, "POST", "/rag/ask", {"item_id": copy, "question": f"{canary} result?",
                                                   "mode": "keyword"}).json() == MISSING, who
        citing = browser.get(f"/discovery/papers/{target}/citing-items")
        assert citing.status_code == 200 and copy not in citing.text, who


@pytest.fixture
def world(upload_api, monkeypatch):
    """A (Markdown + PDF) and B (same Markdown bytes) indexed privately; three common copies with derived rows."""
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    harvest(api.engine, oai_page([(ARXIV_ID, "Exact Linking Study", 1, "2026-09-28")]))
    settings = settings_with_fake_key()
    fake_provider(monkeypatch)
    ids = {"a_md": indexed(storage, a, MD_BODY, settings), "a_pdf": indexed(storage, a, PDF_BODY, settings, pdf=True),
           "b_md": indexed(storage, b, MD_BODY, settings)}
    copies = {"md": common_copy(api, a, admin, ids["a_md"], MD_BODY),
              "pdf": common_copy(api, a, admin, ids["a_pdf"], PDF_BODY),
              # Another common copy of the same bytes (B's request), never revoked here.
              "other": common_copy(api, b, admin, ids["b_md"], MD_BODY)}
    for name in ("md", "pdf"):
        assert publish(admin, copies[name], sha(MD_BODY if name == "md" else PDF_BODY)).status_code == 200
    for name, source in (("md", "a_md"), ("pdf", "a_pdf"), ("other", "b_md")):
        clone_derived(api, ids[source]["item_id"], copies[name], f"{name.upper()}CANARY")
    return api, storage, {"a": a, "b": b, "admin": admin}, ids, copies


def test_cleanup_removes_every_derivative_of_the_revoked_copies_and_nothing_else(world):
    api, storage, browsers, ids, copies = world
    md, pdf, other = copies["md"], copies["pdf"], copies["other"]
    counts = {name: derived_counts(api, copies[name]) for name in ("md", "pdf")}
    # The fixture is representative: every derived table is populated, PDF pages for the PDF only,
    # a linked citation edge, and vectors of the copy's own (canary) chunk hashes.
    assert all(counts["md"][t] > 0 for t in DERIVED if t != "pdf_pages") and counts["md"]["pdf_pages"] == 0
    assert all(counts["pdf"][t] > 0 for t in DERIVED if t != "metadata_candidates"), counts["pdf"]
    assert api.scalar("""SELECT count(*) FROM reference_citation_links l JOIN reference_entries r ON r.id=l.reference_id
        WHERE r.item_id=:i AND l.match_status='linked'""", i=uuid.UUID(md)) == 1
    with api.setup_engine.connect() as conn:
        hashes = conn.scalars(text("SELECT chunk_hash FROM chunks WHERE item_id IN (:m, :p)"),
                              {"m": uuid.UUID(md), "p": uuid.UUID(pdf)}).all()
    assert hashes
    ledgers = {name: ledger(api, copies[name]) for name in ("md", "pdf")}
    kept = {key: rows_of(api, ids[key]["item_id"]) for key in ids} | {"other": rows_of(api, other)}
    box = write(browsers["b"], "POST", "/collections", {"name": "B"}).json()["collection"]
    assert write(browsers["b"], "POST", f"/collections/{box['id']}/items", {"item_id": md}).status_code == 201
    assert_blocked(api, browsers, md, "MDCANARY", listed=True)  # a listed copy's derived rows were never served
    with api.setup_engine.begin() as conn:  # stale links: the backfill would pick both copies' references
        conn.execute(text("UPDATE reference_citation_links SET matcher_version='old' WHERE reference_id IN"
                          " (SELECT id FROM reference_entries WHERE item_id IN (:m, :o))"),
                     {"m": uuid.UUID(md), "o": uuid.UUID(other)})

    for name in ("md", "pdf"):
        assert revoke(browsers["admin"], copies[name]).status_code == 200
        assert_blocked(api, browsers, copies[name], f"{name.upper()}CANARY")  # from t_rev, rows still present
    assert derived_counts(api, md) == counts["md"]
    # Link writers skip a revoked copy instead of failing on the fence (discovery re-target, backfill).
    with Session(api.setup_engine) as db:
        citations.reconcile_arxiv_ids(db, [ARXIV_ID])
        db.commit()
    # Through the runtime login (C2d-5f1); the owner engine is refused at entry.
    assert citations.reconcile_pending(api.setup_engine, settings_with_fake_key().citations) == {
        "halted:worker_runtime_required": 1}
    backfill = citations.reconcile_pending(api.engine, settings_with_fake_key().citations)
    assert backfill["references"] == counts["md"]["reference_entries"]  # the other copy's only (same bytes)
    stale = """SELECT count(*) FROM reference_citation_links l JOIN reference_entries r ON r.id=l.reference_id
        WHERE r.item_id=:i AND l.matcher_version='old'"""
    assert api.scalar(stale, i=uuid.UUID(md)) == counts["md"]["reference_citation_links"]
    assert api.scalar(stale, i=uuid.UUID(other)) == 0
    kept["other"] = rows_of(api, other)

    assert publication.operator_cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 2}
    for name in ("md", "pdf"):
        copy = copies[name]
        assert derived_counts(api, copy) == dict.fromkeys(DERIVED, 0), name
        assert derived_left(api, copy) == []
        with api.setup_engine.connect() as conn:
            item = conn.execute(text("SELECT active_index_generation_id, content_status FROM library_items"
                                     " WHERE id=:i"), {"i": uuid.UUID(copy)}).one()
            calls = conn.execute(text("""SELECT c.vector IS NULL, c.chunk_hash IS NULL, c.content_removed_at FROM
                embedding_calls c JOIN source_files f ON f.id=c.source_file_id WHERE f.item_id=:i"""),
                                 {"i": uuid.UUID(copy)}).all()
            job = conn.execute(text("SELECT state, finished_at, lease_token, cancel_requested FROM ingestion_jobs"
                                    " WHERE item_id=:i"), {"i": uuid.UUID(copy)}).one()
            attempt = conn.scalar(text("SELECT a.outcome FROM ingest_step_attempts a JOIN ingestion_jobs j"
                                       " ON j.id=a.job_id WHERE j.item_id=:i"), {"i": uuid.UUID(copy)})
        assert tuple(item) == (None, "metadata_only")
        assert calls and {tuple(c) for c in calls} == {(True, True, api.clock.now)}
        # The cost ledger survives: same calls, same model/provider/tokens/cost/status/day.
        assert ledger(api, copy) == ledgers[name]
        assert (job.state, job.finished_at, job.lease_token, job.cancel_requested, attempt) == (
            "cancelled", api.clock.now, None, True, "cancelled")
        details = api.scalar("SELECT details FROM audit_events WHERE action='common_copy.cleanup' AND target_id="
                             "(SELECT id::text FROM deletion_tombstones WHERE item_id=:i)", i=uuid.UUID(copy))
        assert details["derived_rows_removed"] == counts[name] and details["jobs_cancelled"] == 1
        assert details["embedding_calls_scrubbed"] == len(ledgers[name]) and details["object_outcome"] == "deleted"
        assert_blocked(api, browsers, copy, f"{name.upper()}CANARY")
    assert api.scalar("SELECT count(*) FROM collection_items WHERE item_id=:i", i=uuid.UUID(md)) == 0

    # Nothing of the revoked copies' text, chunk hashes or vectors is left anywhere in the database...
    dumped = api.dump_all_text()
    for leftover in ("MDCANARY", "PDFCANARY", *hashes):
        assert leftover not in dumped, leftover
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE vector::text LIKE '%0.7%'") == len(ledger(api, other)) - 1
    # ...while the private copies (same checksum for A's and B's Markdown) and the other copy are identical.
    for key in ids:
        assert rows_of(api, ids[key]["item_id"]) == kept[key], key
    assert rows_of(api, other) == kept["other"] and "OTHERCANARY" in dumped
    assert browsers["a"].get(f"/files/{ids['a_md']['file_id']}/original").content == MD_BODY
    hits = write(browsers["b"], "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()
    assert {h["item_id"] for h in hits["hits"]} == {ids["b_md"]["item_id"]}
    assert browsers["a"].get(f"/discovery/papers/{paper(api)}/citing-items").json()["total"] == 1
    audits = api.scalar("SELECT string_agg(details::text, ' ') FROM audit_events")
    for leftover in ("MDCANARY", "PDFCANARY", "Reviewable", "measured observation", *hashes):
        assert leftover not in audits, leftover

    # Rerunning changes nothing.
    before = {name: rows_of(api, copies[name]) for name in copies}
    assert publication.operator_cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {}
    assert {name: rows_of(api, copies[name]) for name in copies} == before

    # A scrubbed call still reserved keeps its slot and ceiling until its lease expires; the reaper then
    # marks it uncertain as usual (status/lease updates are not content). Private ingest still works.
    with api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE embedding_calls SET lease_until = now() - interval '1 second'"
                          " WHERE status='reserved'"))
    settings = settings_with_fake_key()
    fresh = indexed(storage, browsers["b"], b"## Methods\nA later private upload.\n", settings)
    assert derived_counts(api, fresh["item_id"])["chunks"] > 0
    for name in ("md", "pdf"):
        assert api.scalar("""SELECT count(*) FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id
            WHERE f.item_id=:i AND c.status='uncertain' AND c.cost_status='lease_expired_reserved_ceiling'
            AND c.vector IS NULL AND c.chunk_hash IS NULL""", i=uuid.UUID(copies[name])) == 1


def late_writes(api, copy: str, private: dict) -> list[tuple[str, str]]:
    """(statement, expected error) for derived writes aimed at a revoked copy."""
    c = "(SELECT id FROM source_files WHERE item_id=:i)"  # the copy's file
    f = "(SELECT version_id FROM source_files WHERE item_id=:i)"  # and its version
    return [
        (f"""INSERT INTO sections(item_id, version_id, source_file_id, kind, ordinal, text, locator_kind, start_offset,
            end_offset, start_line, end_line, pipeline_version) VALUES (:i, {f}, {c}, 'body', 99, 'late', 'markdown',
            0, 4, 1, 1, 'late')""", CLOSED),
        (f"""INSERT INTO chunks(item_id, version_id, source_file_id, section_id, ordinal, text, start_offset,
            end_offset, start_line, end_line, chunk_hash, pipeline_version, locator_kind)
            VALUES (:i, {f}, {c}, gen_random_uuid(), 0, 'late', 0, 4, 1, 1, repeat('a', 64), 'late', 'markdown')""",
         CLOSED),
        (f"""INSERT INTO pdf_pages(item_id, version_id, source_file_id, page_number, text, width, height, role,
            body_chars, body_page, qualifies, layout_mode, pipeline_version)
            VALUES (:i, {f}, {c}, 1, 'late', 1, 1, 'body', 4, true, false, 'plain', 'late')""", CLOSED),
        (f"""INSERT INTO reference_entries(item_id, version_id, source_file_id, ordinal, raw_citation, fields,
            confidence, provenance, locator_kind, start_offset, end_offset, pipeline_version)
            VALUES (:i, {f}, {c}, 0, 'late', '{{}}', 'low', 'late', 'markdown', 0, 4, 'late')""", CLOSED),
        (f"""INSERT INTO metadata_candidates(item_id, version_id, source_file_id, field_name, value, locator_kind,
            start_offset, end_offset, pipeline_version) VALUES (:i, {f}, {c}, 'title', 'late', 'markdown', 0, 4,
            'late')""", CLOSED),
        (f"""INSERT INTO index_generations(item_id, version_id, source_file_id, job_id, model_id, provider,
            pipeline_version, index_version, expected_chunks, rights_epoch)
            VALUES (:i, {f}, {c}, (SELECT id FROM ingestion_jobs WHERE item_id=:i), 'm', 'p', 'late', 'late', 1, 1)""",
         CLOSED),
        (f"""INSERT INTO embedding_calls(source_file_id, owner_user_id, model_id, provider, pipeline_version, chunk_hash,
            status, reserved_usd, cost_status, reserved_day) VALUES ({c}, NULL,
            'm', 'p', 'late', repeat('b', 64), 'reserved', 0, 'unknown', CURRENT_DATE)""", CLOSED),
        # An entry of A's private generation that names the copy's (scrubbed) embedding call.
        (f"""INSERT INTO index_entries(generation_id, chunk_id, embedding_call_id, chunk_hash, text)
            SELECT g.id, ch.id, (SELECT c.id FROM embedding_calls c WHERE c.source_file_id = {c} LIMIT 1),
            repeat('c', 64), 'late' FROM index_generations g JOIN chunks ch ON ch.item_id = g.item_id
            WHERE g.item_id = '{private["item_id"]}' LIMIT 1""", CLOSED),
        # A late provider response writing its vector (a worker past its checks).
        (f"UPDATE embedding_calls SET vector='[1,1,1]', dimensions=3 WHERE source_file_id = {c}", CLOSED),
        (f"UPDATE embedding_calls SET content_removed_at=NULL WHERE source_file_id = {c}", "stays removed"),
    ]


def test_late_writes_after_purge_are_refused_and_cost_stays_reconcilable(world):
    api, storage, browsers, ids, copies = world
    md = copies["md"]
    assert revoke(browsers["admin"], md).status_code == 200
    with api.setup_engine.connect() as conn:
        generation = conn.scalar(text("SELECT id FROM index_generations WHERE item_id=:i"), {"i": uuid.UUID(md)})
    # Between t_rev and cleanup the rows still exist, and nothing can be added to or re-published from them.
    for statement, message in (
            ("UPDATE reference_citation_links SET match_status='no_arxiv_id', match_method=NULL, id_form=NULL,"
             " arxiv_id=NULL, cited_version=NULL, id_start_offset=NULL, id_end_offset=NULL, discovery_paper_id=NULL"
             " WHERE reference_id IN (SELECT id FROM reference_entries WHERE item_id=:i)", CLOSED),
            ("UPDATE chunks SET text = text || ' again' WHERE item_id=:i", CLOSED),
            ("UPDATE index_entries SET text = text || ' again' WHERE generation_id=:g", CLOSED),
            ("UPDATE index_generations SET state='superseded' WHERE item_id=:i", CLOSED)):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            conn.execute(text(statement), {"i": uuid.UUID(md), "g": generation})
    # Revoke already left the index (M6b-2b2b): no active pointer from t_rev, and it cannot come back.
    assert api.scalar("SELECT active_index_generation_id IS NULL AND content_status='metadata_only'"
                      " FROM library_items WHERE id=:i", i=uuid.UUID(md))
    with pytest.raises(IntegrityError, match="never indexed again"), api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET active_index_generation_id=:g WHERE id=:i"),
                     {"i": uuid.UUID(md), "g": generation})
    # Purging with rows left is refused by the DB, whatever the application does (metadata removed first,
    # so the 0023 metadata guard is satisfied and the 0024 derived guard decides).
    with pytest.raises(IntegrityError, match="cleanup must remove derived chunks, embedding_content"), \
            Session(api.setup_engine) as db:
        publication._remove_metadata(db, uuid.UUID(md), api.clock.now)
        db.execute(text("""UPDATE deletion_tombstones SET purged_at=now(), object_outcome='no_object',
            scrubbed_fields='{}', retained_fields='{}' WHERE item_id=:i"""), {"i": uuid.UUID(md)})
    # Only a revoked copy's embedding content can be removed: never a private one.
    with pytest.raises(IntegrityError, match="only a revoked common copy"), api.setup_engine.begin() as conn:
        conn.execute(text("""UPDATE embedding_calls SET vector=NULL, chunk_hash=NULL, content_removed_at=now()
            WHERE source_file_id=:f"""), {"f": uuid.UUID(ids["a_md"]["file_id"])})

    assert publication.operator_cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
    for statement, message in late_writes(api, md, ids["a_md"]):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            conn.execute(text(statement), {"i": uuid.UUID(md)})
    assert derived_left(api, md) == []
    # Late cost reconciliation (status, tokens, cost) is still accepted on a scrubbed call.
    with api.setup_engine.begin() as conn:
        call = conn.scalar(text("""UPDATE embedding_calls SET status='completed', cost_usd=0.00002,
            cost_status='usage_price_estimate', input_tokens=7, lease_until=NULL
            WHERE status='reserved' AND source_file_id=(SELECT id FROM source_files WHERE item_id=:i)
            RETURNING id"""), {"i": uuid.UUID(md)})
    assert call is not None
    assert api.scalar("SELECT vector IS NULL AND chunk_hash IS NULL FROM embedding_calls WHERE id=:c", c=call)
    # Private items are unaffected by the fence.
    with api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE chunks SET text = text WHERE item_id=:i"), {"i": uuid.UUID(ids["a_md"]["item_id"])})


def wait_for_lock_wait(api, *, blocker_pid: int | None = None) -> None:
    deadline = time.monotonic() + 10
    # Runtime cannot inspect other users' pg_stat_activity fields. The owner
    # observes this labelled legacy adversary; no runtime grants are widened.
    while True:
        with api.setup_engine.connect() as conn:
            waiting = conn.scalar(text("""SELECT count(*) FROM pg_stat_activity a
                WHERE datname=current_database() AND wait_event_type='Lock'
                  AND (CAST(:blocker AS integer) IS NULL OR
                    (CAST(:blocker AS integer)=ANY(pg_blocking_pids(a.pid))
                     AND a.query LIKE '%library_items%'))"""), {"blocker": blocker_pid})
        if waiting:
            return
        assert time.monotonic() < deadline, "nothing waited for the item lock"
        time.sleep(0.02)


LATE_SECTION = """INSERT INTO sections(item_id, version_id, source_file_id, kind, ordinal, text, locator_kind,
    start_offset, end_offset, start_line, end_line, pipeline_version)
    SELECT item_id, version_id, id, 'body', 900, 'RACECANARY', 'markdown', 0, 10, 1, 1, 'race'
    FROM source_files WHERE item_id=:i"""


def test_writer_before_revoke_commits_first_and_cleanup_removes_its_rows(world, monkeypatch):
    api, storage, browsers, ids, copies = world
    md = copies["md"]
    before = derived_counts(api, md)
    conn = api.setup_engine.connect()
    tx = conn.begin()
    conn.execute(text(LATE_SECTION), {"i": uuid.UUID(md)})  # holds the item FOR KEY SHARE until commit
    outcome = {}

    def revoke_it():
        try:
            revoke_in_own_session(api, md)
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - reported below
            outcome["error"] = exc

    revoker = Thread(target=revoke_it)
    revoker.start()
    wait_for_lock_wait(api)  # the revoke waits for the writer, it cannot commit in between
    assert api.scalar("SELECT publication_state FROM library_items WHERE id=:i", i=uuid.UUID(md)) == "listed"
    tx.commit()
    conn.close()
    revoker.join(10)
    assert outcome == {"ok": True}
    assert derived_counts(api, md)["sections"] == before["sections"] + 1
    assert publication.operator_cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
    assert derived_counts(api, md) == dict.fromkeys(DERIVED, 0)
    assert "RACECANARY" not in api.dump_all_text()


def test_writer_waiting_on_a_revoke_is_refused_after_t_rev(world, monkeypatch):
    api, storage, browsers, ids, copies = world
    pdf = copies["pdf"]
    before = derived_counts(api, pdf)
    holding, release = Event(), Event()
    real = publication._cancel_jobs

    def held(db, item_id, now):  # inside the revoke transaction, item locked FOR UPDATE
        cancelled = real(db, item_id, now)
        holding.set()
        assert release.wait(10)
        return cancelled

    monkeypatch.setattr(publication, "_cancel_jobs", held)
    outcome: dict[str, object] = {}

    def revoke_it():
        revoke_in_own_session(api, pdf)
        outcome["revoked"] = True

    def write_it():
        try:
            with api.setup_engine.begin() as c:
                c.execute(text(LATE_SECTION), {"i": uuid.UUID(pdf)})
            outcome["written"] = True
        except IntegrityError as exc:
            outcome["refused"] = str(exc.orig)

    revoker = Thread(target=revoke_it)
    revoker.start()
    assert holding.wait(10)
    writer = Thread(target=write_it)
    writer.start()
    wait_for_lock_wait(api)  # the writer waits for the revoke's lock
    release.set()
    revoker.join(10)
    writer.join(10)
    assert outcome.get("revoked") is True and "written" not in outcome
    assert CLOSED in outcome["refused"]
    assert derived_counts(api, pdf) == before
    assert publication.operator_cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
    assert derived_counts(api, pdf) == dict.fromkeys(DERIVED, 0)


def test_failure_mid_cleanup_rolls_back_everything_and_is_retried(world, monkeypatch):
    api, storage, browsers, ids, copies = world
    md = copies["md"]
    assert revoke(browsers["admin"], md).status_code == 200
    counts, rows = derived_counts(api, md), rows_of(api, md)
    key = api.scalar("SELECT storage_key FROM source_files WHERE item_id=:i", i=uuid.UUID(md))
    now = api.clock.now

    def tomb():
        with api.setup_engine.connect() as conn:
            return conn.execute(text("SELECT purged_at, cleanup_attempts, last_cleanup_error FROM deletion_tombstones"
                                     " WHERE item_id=:i"), {"i": uuid.UUID(md)}).one()

    # 1. A DB error after the derived rows were deleted: everything rolls back.
    real_finish = publication._finish_jobs
    monkeypatch.setattr(publication, "_finish_jobs", lambda db, item_id, at: db.execute(text("SELECT 1/0")))
    assert publication.operator_cleanup_revoked(api.setup_engine, storage, now, 50) == {"error:cleanup_failed": 1}
    assert rows_of(api, md) == rows and tuple(tomb()) == (None, 1, "cleanup_failed")
    monkeypatch.setattr(publication, "_finish_jobs", real_finish)

    # 2. Storage down: nothing removed either.
    class Down:
        def size(self, key):
            raise ConnectionError("synthetic S3 outage")

    assert publication.operator_cleanup_revoked(api.setup_engine, Down(), now, 50) == {"error:storage_unavailable": 1}
    assert rows_of(api, md) == rows and tuple(tomb()) == (None, 2, "storage_unavailable")

    # 3. The object is deleted, then the transaction fails: rows come back, the object stays gone.
    real = publication._remove_metadata

    def fail_after_object(db, item_id, at):
        real(db, item_id, at)
        raise RuntimeError("synthetic failure after the object delete")

    monkeypatch.setattr(publication, "_remove_metadata", fail_after_object)
    assert publication.operator_cleanup_revoked(api.setup_engine, storage, now, 50) == {"error:cleanup_failed": 1}
    assert rows_of(api, md) == rows and tuple(tomb()) == (None, 3, "cleanup_failed")
    assert key not in {k for k, _ in storage.orphan_keys("")}
    detail = browsers["admin"].get(f"/admin/common-copies/{md}").json()["item"]["tombstone"]
    assert (detail["purged_at"], detail["last_cleanup_error"], detail["overdue"]) == (None, "cleanup_failed", False)
    assert_blocked(api, browsers, md, "MDCANARY")  # still blocked while cleanup keeps failing
    monkeypatch.setattr(publication, "_remove_metadata", real)

    # The retry finishes the job.
    assert publication.operator_cleanup_revoked(api.setup_engine, storage, now, 50) == {"already_absent": 1}
    assert derived_counts(api, md) == dict.fromkeys(DERIVED, 0)
    purged_at, attempts, error = tomb()
    assert (purged_at, attempts, error) == (now, 4, None)
    details = api.scalar("SELECT details FROM audit_events WHERE action='common_copy.cleanup'")
    assert details["derived_rows_removed"] == counts
