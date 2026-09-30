"""M6b-2b3a full-text search of indexed common copies under their AccessRights (PostgreSQL + MinIO).

A and B each index private Markdown (B with the *same bytes and checksum* as
one of A's files); the Admin approves A's files into common copies: one
published and indexed (``ok``), one still ``pending``, one published without
provider processing (so it stays ``metadata_only``), one indexed and then
revoked (its index rows still await cleanup). ``process_job`` runs the real
pipeline; embedding, rerank and generation are fakes. Nothing calls
OpenRouter and ``pricing_verified`` is only set on in-memory settings copies.
"""
from __future__ import annotations

import uuid
from threading import Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import search as search_module
from app.config import get_settings
from app.ingest.embedding import EmbeddingResult
from app.ingest.worker import process_job
from app.main import app
from tests.test_common_cleanup import indexed, wait_for_lock_wait
from tests.test_common_index import INDEXABLE, sha, start
from tests.test_ingest_worker import upload
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_publication import MISSING, publish, revoke, revoke_in_own_session
from tests.test_review_approval import approve
from tests.test_review_requests import submit
from tests.test_uploads import upload_api  # noqa: F401  (fixture)

MAIN = (b"# Shared Search Fixture\n\n## Abstract\nSharedcanary abstract of the method.\n\n## Methods\n"
        b"Sharedcanary result is nine units in the measured run.\n\n## References\n"
        b"[1] A. Alpha. Refcanary Linked Study. 2026.\n")
A_ONLY = b"## Methods\nSharedcanary result Asecretcanary private body of A.\n"
B_ONLY = b"## Methods\nSharedcanary result Bsecretcanary private body of B.\n"
PENDING = b"## Methods\nSharedcanary Pendingcanary body awaiting publication.\n"
META = b"## Methods\nSharedcanary Metacanary body without provider processing.\n"
# Matches every term the permitted copy matches, and one more: a stronger keyword hit.
REVOKED = b"## Methods\nSharedcanary result is nine units; Revokedcanary body revoked after indexing.\n"
DERIVED_PATHS = ("chunks", "references", "coverage", "metadata-candidates")


def search(browser, query="Sharedcanary", **body):
    return browser.post("/search", {"query": query, "mode": "keyword"} | body)


def hit_items(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {hit["item_id"] for hit in response.json()["hits"]}


def chunk_ids(api, item: str) -> list[str]:
    """Indexed body/abstract chunks of the item's active or last generation, straight from the DB."""
    with api.engine.connect() as conn:
        return [str(c) for c in conn.scalars(text("""SELECT DISTINCT e.chunk_id FROM index_entries e
            JOIN index_generations g ON g.id=e.generation_id WHERE g.item_id=:i ORDER BY 1"""),
            {"i": uuid.UUID(item)}).all()]


def copy_of(w, ids: dict, body: bytes, *, scope=INDEXABLE, published=True, index=False) -> str:
    rid = submit(w.a, ids).json()["request"]["id"]
    response = approve(w.admin, w.api, rid, file_checksum_sha256=sha(body), scope=scope)
    assert response.status_code == 200, response.text
    item = response.json()["request"]["pending_publication_id"]
    if published:
        assert publish(w.admin, item, sha(body)).status_code == 200
    if index:
        started = start(w.admin, item)
        assert started.status_code == 200, started.text
        job = uuid.UUID(started.json()["job"]["id"])
        assert process_job(job, storage=w.storage) == "awaiting_index"
        assert process_job(job, w.settings, w.storage) == "ready"
    return item


def extracted(w, browser, body: bytes) -> dict:
    """A private upload extracted but never indexed: it cannot appear in anyone's search."""
    ids = upload(browser, body)
    assert process_job(uuid.UUID(ids["job_id"]), storage=w.storage) == "awaiting_index"
    return ids


def no_outbound_http(monkeypatch) -> None:
    """Every provider here is a fake; a request that reaches httpx is a test bug, never a call to OpenRouter."""
    def refuse(*args, **kwargs):
        raise AssertionError("outbound HTTP attempted in a common-search test")
    # The network transport only: the ASGI TestClient uses its own transport.
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", refuse)


def build(upload_api, monkeypatch, *, ok_scope=INDEXABLE):
    no_outbound_http(monkeypatch)
    # Defaults; tests that inspect payloads replace them.
    fake_query_embedding(monkeypatch, [])
    fake_rerank(monkeypatch, [])
    monkeypatch.setattr("app.rag.generate", refuse_generation)
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    settings = settings_with_fake_key()
    settings.rerank.pricing_verified = True
    settings.generation.pricing_verified = True
    fake_provider(monkeypatch)
    w = SimpleNamespace(api=api, storage=storage, a=a, b=b, admin=admin, settings=settings)
    w.a_main = indexed(storage, a, MAIN, settings)
    w.a_secret = indexed(storage, a, A_ONLY, settings)
    w.b_same = indexed(storage, b, MAIN, settings)  # same bytes and checksum as A's file and the common copy
    w.b_secret = indexed(storage, b, B_ONLY, settings)
    w.ok = copy_of(w, w.a_main, MAIN, scope=ok_scope, index=True)
    w.pending = copy_of(w, extracted(w, a, PENDING), PENDING, published=False)
    w.meta = copy_of(w, extracted(w, a, META), META, scope=INDEXABLE | {"external_processing": False})
    assert start(admin, w.meta).json() == {"error": "external_processing_not_permitted"}
    w.revoked = copy_of(w, extracted(w, a, REVOKED), REVOKED, index=True)
    w.revoked_chunks = chunk_ids(api, w.revoked)
    assert w.revoked_chunks
    w.collection = a.post("/collections", {"name": "Mixed"}).json()["collection"]["id"]
    for item in (w.ok, w.meta, w.revoked, w.a_secret["item_id"]):
        assert a.post(f"/collections/{w.collection}/items", {"item_id": item}).status_code == 201
    assert revoke(admin, w.revoked).status_code == 200
    # Revoked, not cleaned up: its chunks, entries and vectors are still in the DB.
    assert api.scalar("""SELECT count(*) FROM index_entries e JOIN index_generations g ON g.id=e.generation_id
        WHERE g.item_id=:i""", i=uuid.UUID(w.revoked)) > 0
    w.ok_chunks = chunk_ids(api, w.ok)
    w.ok_file = str(api.scalar("SELECT id FROM source_files WHERE item_id=:i", i=uuid.UUID(w.ok)))
    w.allowed = {"a": {w.a_main["item_id"], w.a_secret["item_id"], w.ok},
                 "b": {w.b_same["item_id"], w.b_secret["item_id"], w.ok}, "admin": {w.ok}}
    w.browsers = {"a": a, "b": b, "admin": admin}
    app.dependency_overrides[get_settings] = lambda: settings
    return w


@pytest.fixture
def w(upload_api, monkeypatch):
    return build(upload_api, monkeypatch)


def fake_query_embedding(monkeypatch, sent: list):
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type: (
        sent.append(("embed", value)) or EmbeddingResult([0.1, 0.1, 0.1], cfg.embedding.model_id, 2)))


def refuse_generation(messages, settings):
    raise AssertionError("generation called")


def fake_rerank(monkeypatch, sent: list, action=None):
    def rerank(query, docs, cfg):
        sent.append(("rerank", query, list(docs)))
        if action:
            action()
        return ([{"index": i, "relevance_score": 1.0 - i / 100} for i in range(len(docs))], 10, None)
    monkeypatch.setattr("app.search.rerank", rerank)


def test_every_account_searches_the_permitted_common_copy_and_no_one_elses_private(w, monkeypatch):
    sent = []
    fake_query_embedding(monkeypatch, sent)
    fake_rerank(monkeypatch, sent)
    for who, browser in w.browsers.items():
        for body in ({}, {"mode": "hybrid"}, {"mode": "hybrid", "rerank": True}):
            response = search(browser, **body)
            data = response.json()
            # The audience is filtered before top-k: exactly the caller's own private and the permitted common copy.
            assert hit_items(response) == w.allowed[who], (who, body)
            assert data["count"] == len(data["hits"]) and data["candidate_count"] >= data["count"], (who, body)
            for forbidden in ("Bsecretcanary", "Asecretcanary", "Pendingcanary", "Metacanary", "Revokedcanary",
                              "Refcanary"):
                if not (who == "a" and forbidden == "Asecretcanary") and not (who == "b" and forbidden == "Bsecretcanary"):
                    assert forbidden not in response.text, (who, body, forbidden)
            common = [hit for hit in data["hits"] if hit["item_id"] == w.ok]
            assert common and {hit["visibility"] for hit in common} == {"common"}, who
            assert {hit["source_file_id"] for hit in common} == {w.ok_file}, who
            # Rights permit download here, so the hit carries the download route of this file only.
            assert {hit["original_url"] for hit in common} == {f"/files/{w.ok_file}/original"}, who
        # The rerank payload of this caller had only their audience's text; no reference entry.
        docs = [doc for entry in sent if entry[0] == "rerank" for doc in entry[2]]
        assert docs and not any("Refcanary" in d or "References" in d for d in docs), who
        other = {"a": "Bsecretcanary", "b": "Asecretcanary", "admin": "secretcanary"}[who]
        assert not any(other in d for d in docs), who
        sent.clear()
    # Keyword counts only the permitted copy for the Admin, although the revoked copy's rows also match.
    expected = w.api.scalar("""SELECT count(*) FROM chunks c JOIN index_entries e ON e.chunk_id=c.id
        WHERE c.item_id=:i AND c.text LIKE '%Sharedcanary%'""", i=uuid.UUID(w.ok))
    assert w.api.scalar("""SELECT count(*) FROM chunks WHERE item_id=:i AND text LIKE '%Sharedcanary%'""",
                        i=uuid.UUID(w.revoked)) > 0
    admin = search(w.admin).json()
    assert admin["candidate_count"] == admin["count"] == expected > 0
    # The chunk viewer opens each common hit for A, B and the Admin; private chunks only for their owner.
    for chunk in w.ok_chunks:
        for who, browser in w.browsers.items():
            opened = browser.get(f"/search/chunks/{chunk}")
            assert opened.status_code == 200, (who, opened.text)
            assert (opened.json()["item_id"], opened.json()["visibility"]) == (w.ok, "common")
    for owner, ids in (("a", w.a_main), ("b", w.b_same)):
        for chunk in chunk_ids(w.api, ids["item_id"]):
            for who, browser in w.browsers.items():
                expected = 200 if who == owner else 404
                assert browser.get(f"/search/chunks/{chunk}").status_code == expected, (owner, who)
    for chunk in w.revoked_chunks:
        for browser in w.browsers.values():
            assert browser.get(f"/search/chunks/{chunk}").json() == MISSING


def test_item_collection_and_derived_reads_serve_only_the_permitted_copy(w):
    for who, browser in w.browsers.items():
        assert hit_items(search(browser, item_id=w.ok)) == {w.ok}, who
        for item in (w.pending, w.meta, w.revoked, str(uuid.uuid4())):
            assert search(browser, item_id=item).json() == MISSING, (who, item)
        detail = browser.get(f"/library/items/{w.ok}").json()["item"]
        assert (detail["fulltext_search_available"], detail["rag_available"]) == (True, True), who
        assert [v["coverage"] for v in detail["versions"]] == ["complete"], who
        chunks = browser.get(f"/library/items/{w.ok}/chunks").json()["chunks"]
        assert chunks and {c["source_file_id"] for c in chunks} == {w.ok_file}, who
        refs = browser.get(f"/library/items/{w.ok}/references").json()
        assert [r["evidence_kind"] for r in refs["references"]] == ["bibliography_entry"], who
        assert refs["citation_relation_note"], who
        assert browser.get(f"/library/items/{w.ok}/coverage").status_code == 200, who
        assert browser.get(f"/library/items/{w.ok}/metadata-candidates").status_code == 200, who
        for item in (w.pending, w.meta, w.revoked):
            for path in DERIVED_PATHS:
                assert browser.get(f"/library/items/{item}/{path}").json() == MISSING, (who, item, path)
        if who != "a":
            for path in DERIVED_PATHS:
                assert browser.get(f"/library/items/{w.a_main['item_id']}/{path}").json() == MISSING, (who, path)
        listed = {i["id"]: i for i in browser.get("/library/items?scope=common&limit=100").json()["items"]}
        assert listed[w.ok]["fulltext_search_available"] is True, who
        assert listed[w.meta]["fulltext_search_available"] is False and w.pending not in listed, who
        assert w.revoked not in listed, who
    # A reference entry is labelled as a citation relation; it is never indexed as content.
    for browser in w.browsers.values():
        assert search(browser, "Refcanary Linked").json()["count"] == 0
    # Collection membership narrows the audience; it never admits a metadata_only or revoked copy.
    scoped = search(w.a, collection_id=w.collection)
    assert hit_items(scoped) == {w.ok, w.a_secret["item_id"]}
    assert scoped.json()["candidate_count"] == scoped.json()["count"]
    for browser in (w.b, w.admin):
        assert search(browser, collection_id=w.collection).json() == MISSING
    members = {i["id"]: i["fulltext_search_available"]
               for i in w.a.get(f"/collections/{w.collection}/items?limit=100").json()["items"]}
    assert members == {w.ok: True, w.meta: False, w.a_secret["item_id"]: True}  # the revoked copy is not listed
    # Admin status reports the same gate.
    index = w.admin.get(f"/admin/common-copies/{w.ok}").json()["item"]["index"]
    assert (index["search_open"], index["rag_open"]) == (True, True)
    for item in (w.meta, w.revoked):
        assert w.admin.get(f"/admin/common-copies/{item}").json()["item"]["index"]["search_open"] is False


def test_forbidden_copies_never_take_a_top_k_slot(w, monkeypatch):
    """With top-k 1, a revoked copy's stronger keyword and nearer vector rows must not win the only slot."""
    w.settings.search.keyword_top_k = 1
    w.settings.search.vector_top_k = 1
    with w.api.engine.begin() as conn:
        # Out-of-band: the revoked copy's rows (still present) match best on both lists.
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("""UPDATE embedding_calls SET vector='[1,0,0]' WHERE source_file_id IN
            (SELECT id FROM source_files WHERE item_id=:r)"""), {"r": uuid.UUID(w.revoked)})
        conn.execute(text("""UPDATE embedding_calls SET vector='[0,1,0]' WHERE source_file_id IN
            (SELECT id FROM source_files WHERE item_id=:o)"""), {"o": uuid.UUID(w.ok)})
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type: EmbeddingResult(
        [1.0, 0.0, 0.0], cfg.embedding.model_id, 2))
    for mode in ("keyword", "hybrid"):
        response = search(w.admin, "Sharedcanary result nine Revokedcanary", mode=mode)
        assert hit_items(response) == {w.ok}, mode
        assert "Revokedcanary" not in response.text


TAMPERS = {
    # Each one-row change is written out of band (triggers off) and undone; the serving gate must not trust
    # the index-time checks. Tokens: :i item, :g generation, :r rights, :j job.
    "generation_of_private_pipeline": ("UPDATE index_generations SET index_version='private-keyword-vector-v1' WHERE id=:g",
                                       "UPDATE index_generations SET index_version='common-keyword-vector-v1' WHERE id=:g"),
    "generation_not_active": ("UPDATE index_generations SET state='superseded' WHERE id=:g",
                              "UPDATE index_generations SET state='active' WHERE id=:g"),
    "item_epoch_ahead": ("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i",
                         "UPDATE library_items SET rights_epoch=rights_epoch-1 WHERE id=:i"),
    "job_epoch_differs": ("UPDATE ingestion_jobs SET rights_epoch_at_start=rights_epoch_at_start+7 WHERE id=:j",
                          "UPDATE ingestion_jobs SET rights_epoch_at_start=rights_epoch_at_start-7 WHERE id=:j"),
    "rights_revoked": ("""UPDATE access_rights SET revoked_at=now(), revoked_by_user_id=reviewer_user_id,
                          revocation_reason_code='rights_withdrawn', rights_epoch_at_revoke=rights_epoch_at_grant+1
                          WHERE id=:r""",
                       """UPDATE access_rights SET revoked_at=NULL, revoked_by_user_id=NULL, revocation_reason_code=NULL,
                          rights_epoch_at_revoke=NULL WHERE id=:r"""),
    "rights_conditions": ("UPDATE access_rights SET scope_conditions='Chỉ trích dẫn ngắn' WHERE id=:r",
                          "UPDATE access_rights SET scope_conditions=NULL WHERE id=:r"),
    "rights_no_fulltext": ("UPDATE access_rights SET permits_fulltext_search=false, permits_external_processing=false WHERE id=:r",
                           "UPDATE access_rights SET permits_fulltext_search=true, permits_external_processing=true WHERE id=:r"),
    "rights_other_checksum": ("UPDATE access_rights SET file_checksum_sha256=repeat('0', 64) WHERE id=:r",
                              "UPDATE access_rights SET file_checksum_sha256=(SELECT checksum_sha256 FROM source_files WHERE item_id=:i) WHERE id=:r"),
    "publication_revoked": ("UPDATE common_publications SET revoked_at=now() WHERE item_id=:i",
                            "UPDATE common_publications SET revoked_at=NULL WHERE item_id=:i"),
    "publication_size_differs": ("UPDATE common_publications SET verified_byte_size=verified_byte_size+1 WHERE item_id=:i",
                                 "UPDATE common_publications SET verified_byte_size=verified_byte_size-1 WHERE item_id=:i"),
    "item_pending": ("UPDATE library_items SET publication_state='pending' WHERE id=:i",
                     "UPDATE library_items SET publication_state='listed' WHERE id=:i"),
    "item_metadata_only": ("UPDATE library_items SET content_status='metadata_only' WHERE id=:i",
                           "UPDATE library_items SET content_status='indexed' WHERE id=:i"),
    "item_other_active_version": ("UPDATE library_items SET active_version_id=:other WHERE id=:i",
                                  "UPDATE library_items SET active_version_id=:v WHERE id=:i"),
    "tombstone": ("""INSERT INTO deletion_tombstones(target_kind, item_id, reason_code, prior_publication_state,
                     rights_epoch, recorded_at, recorded_by_user_id, purge_due_at)
                     SELECT 'common_copy', i.id, 'rights_withdrawn', 'listed', i.rights_epoch + 1, now(),
                     (SELECT id FROM users WHERE role='admin' LIMIT 1), now() + interval '1 day'
                     FROM library_items i WHERE i.id=:i""",
                  "DELETE FROM deletion_tombstones WHERE item_id=:i"),
}


def ok_ids(w) -> dict:
    with w.api.engine.connect() as conn:
        row = conn.execute(text("""SELECT i.id i, i.active_index_generation_id g, i.active_version_id v, r.id r,
            g.job_id j FROM library_items i JOIN access_rights r ON r.item_id=i.id
            JOIN index_generations g ON g.id=i.active_index_generation_id WHERE i.id=:i"""),
            {"i": uuid.UUID(w.ok)}).mappings().one()
    return dict(row)


def out_of_band(w, sql: str, params: dict) -> None:
    with w.api.engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text(sql), {k: v for k, v in params.items() if f":{k}" in sql})


def assert_closed(w, label: str) -> None:
    for who, browser in w.browsers.items():
        assert hit_items(search(browser)) == w.allowed[who] - {w.ok}, (label, who)
        assert w.ok not in search(browser, mode="hybrid").text, (label, who)
        assert search(browser, item_id=w.ok).json() == MISSING, (label, who)
        for chunk in w.ok_chunks:
            assert browser.get(f"/search/chunks/{chunk}").json() == MISSING, (label, who)
        for path in DERIVED_PATHS:
            assert browser.get(f"/library/items/{w.ok}/{path}").json() == MISSING, (label, who, path)
        detail = browser.get(f"/library/items/{w.ok}").json()
        # Metadata may stay visible (listed) or not (pending); full text is never announced.
        assert detail == MISSING or detail["item"]["fulltext_search_available"] is False, label
    assert w.ok not in hit_items(search(w.a, collection_id=w.collection)), label


def test_serving_gate_rechecks_rights_publication_epoch_and_generation(w, monkeypatch):
    fake_query_embedding(monkeypatch, [])
    params = ok_ids(w) | {"other": uuid.UUID(w.a_main["version_id"])}
    for label, (tamper, undo) in TAMPERS.items():
        out_of_band(w, tamper, params)
        assert_closed(w, label)
        out_of_band(w, undo, params)
        assert w.ok in hit_items(search(w.b)), label


def test_chunk_of_another_file_or_version_of_the_copy_stays_closed(w):
    """Derived rows are matched on their own version/file: only the rights' file of the active version opens."""
    with w.api.engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        version = conn.scalar(text("INSERT INTO paper_versions (item_id, version_label) VALUES (:i, 'v2') RETURNING id"),
                              {"i": uuid.UUID(w.ok)})
        other = conn.scalar(text("""INSERT INTO source_files (item_id, version_id, storage_key, original_filename,
            byte_size, checksum_sha256, verified_mime) SELECT item_id, :v, 'originals/other-' || id, 'other.md',
            byte_size, repeat('1', 64), verified_mime FROM source_files WHERE item_id=:i RETURNING id"""),
            {"i": uuid.UUID(w.ok), "v": version})
        conn.execute(text("""INSERT INTO sections (item_id, version_id, source_file_id, kind, ordinal, text,
            locator_kind, start_offset, end_offset, start_line, end_line, pipeline_version)
            VALUES (:i, :v, :f, 'body', 1, 'Othercanary', 'markdown', 0, 11, 1, 1, 'x')"""),
            {"i": uuid.UUID(w.ok), "v": version, "f": other})
        conn.execute(text("""INSERT INTO chunks (item_id, version_id, source_file_id, section_id, ordinal, text,
            start_offset, end_offset, start_line, end_line, chunk_hash, pipeline_version, locator_kind)
            SELECT :i, :v, :f, s.id, 1, 'Othercanary', 0, 11, 1, 1, repeat('2', 64), 'x', 'markdown'
            FROM sections s WHERE s.source_file_id=:f"""), {"i": uuid.UUID(w.ok), "v": version, "f": other})
    for who, browser in w.browsers.items():
        listed = browser.get(f"/library/items/{w.ok}/chunks?limit=100")
        assert listed.status_code == 200 and "Othercanary" not in listed.text, who
        assert {c["source_file_id"] for c in listed.json()["chunks"]} == {w.ok_file}, who
        assert search(browser, "Othercanary").json()["count"] == 0, who


def test_download_is_a_separate_gate_from_search(upload_api, monkeypatch):
    w = build(upload_api, monkeypatch, ok_scope=INDEXABLE | {"original_download": False})
    for who, browser in w.browsers.items():
        hits = [h for h in search(browser).json()["hits"] if h["item_id"] == w.ok]
        assert hits and {h["original_url"] for h in hits} == {None}, who
        assert browser.get(f"/search/chunks/{w.ok_chunks[0]}").json()["original_url"] is None, who
        assert browser.get(f"/files/{w.ok_file}/original").json() == MISSING, who
        assert browser.get(f"/library/items/{w.ok}").json()["item"]["files"] == [], who
    # A's own private hits still carry their download route.
    own = [h for h in search(w.a).json()["hits"] if h["item_id"] == w.a_main["item_id"]]
    assert {h["original_url"] for h in own} == {f"/files/{w.a_main['file_id']}/original"}


def test_rerank_gets_common_text_only_while_rights_permit_external_processing(w, monkeypatch):
    sent = []
    fake_rerank(monkeypatch, sent)
    params = ok_ids(w)
    # Full text without external processing (possible only out of band: indexing needs both): search still
    # serves the copy, but none of its chunks is sent to rerank.
    out_of_band(w, "UPDATE access_rights SET permits_external_processing=false WHERE id=:r", params)
    admin = search(w.admin, rerank=True).json()
    assert {h["item_id"] for h in admin["hits"]} == {w.ok}
    assert (admin["rerank_status"], admin["rerank_withheld"], sent) == ("degraded_not_permitted", admin["count"], [])
    a = search(w.a, rerank=True).json()
    common_hits = [h for h in a["hits"] if h["item_id"] == w.ok]
    private_hits = [h for h in a["hits"] if h["item_id"] != w.ok]
    assert common_hits and a["rerank_status"] == "applied" and a["rerank_withheld"] == len(common_hits)
    [(_, _, docs)] = sent
    # A's own chunks only (A's private file has the same text as the copy; the count tells them apart).
    assert len(docs) == len(private_hits) and any("Asecretcanary" in d for d in docs)
    # Reranked candidates come first, the withheld common hits keep their fused order after them.
    assert [h["item_id"] for h in a["hits"]][-len(common_hits):] == [w.ok] * len(common_hits)
    common_texts = common_chunk_texts(w)
    out_of_band(w, "UPDATE access_rights SET permits_external_processing=true WHERE id=:r", params)
    sent.clear()
    # Conditions close the copy entirely: no hit, nothing sent.
    out_of_band(w, "UPDATE access_rights SET scope_conditions='Chỉ trích dẫn ngắn' WHERE id=:r", params)
    closed = search(w.admin, rerank=True).json()
    assert (closed["count"], closed["candidate_count"], closed["rerank_status"], sent) == (0, 0, "not_requested", [])
    out_of_band(w, "UPDATE access_rights SET scope_conditions=NULL WHERE id=:r", params)
    permitted = search(w.admin, rerank=True).json()
    assert permitted["rerank_status"] == "applied" and permitted["rerank_withheld"] == 0
    [(_, _, docs)] = sent
    assert set(docs) <= common_texts


# ----------------------------------------------------------------------------- revoke timing


def assert_gone_for_new_requests(w) -> None:
    """From t_rev: no new hit, chunk or derived read of the copy, although its index rows are still there."""
    assert w.api.scalar("""SELECT count(*) FROM index_entries e JOIN index_generations g ON g.id=e.generation_id
        WHERE g.item_id=:i""", i=uuid.UUID(w.ok)) > 0
    for who, browser in w.browsers.items():
        assert w.ok not in hit_items(search(browser)), who
        assert w.ok not in hit_items(search(browser, mode="hybrid")), who
        assert search(browser, item_id=w.ok).json() == MISSING, who
        for chunk in w.ok_chunks:
            assert browser.get(f"/search/chunks/{chunk}").json() == MISSING, who
        for path in DERIVED_PATHS:
            assert browser.get(f"/library/items/{w.ok}/{path}").json() == MISSING, (who, path)
    assert w.ok not in hit_items(search(w.a, collection_id=w.collection))


def rerank_usage_statuses(w) -> list[str]:
    with w.api.engine.connect() as conn:
        return list(conn.scalars(text("SELECT status FROM search_usage WHERE task_type='rerank' ORDER BY created_at")))


def test_revoke_before_retrieval_blocks_new_requests_before_cleanup(w, monkeypatch):
    fake_query_embedding(monkeypatch, [])
    assert revoke(w.admin, w.ok).status_code == 200
    assert_gone_for_new_requests(w)


def test_revoke_before_rerank_sends_nothing(w, monkeypatch):
    sent = []
    fake_rerank(monkeypatch, sent)
    original = search_module.reserve

    def revoke_after_reservation(db, principal, cfg, task, amount):
        usage = original(db, principal, cfg, task, amount)
        if task == "rerank":
            revoke_in_own_session(w.api, w.ok)
        return usage
    monkeypatch.setattr("app.search.reserve", revoke_after_reservation)
    response = search(w.admin, rerank=True)
    assert (response.status_code, response.json()) == (409, {"error": "search_scope_changed"})
    assert sent == [] and rerank_usage_statuses(w) == ["rejected"]
    monkeypatch.setattr("app.search.reserve", original)
    assert_gone_for_new_requests(w)


def test_revoke_during_rerank_withholds_the_response(w, monkeypatch):
    sent = []
    fake_rerank(monkeypatch, sent, action=lambda: revoke_in_own_session(w.api, w.ok))
    response = search(w.b, rerank=True)
    assert (response.status_code, response.json()) == (409, {"error": "search_scope_changed"})
    # What reached the provider was checked before sending (before t_rev): B's audience only.
    [(_, _, docs)] = sent
    assert docs and not any("Asecretcanary" in d or "Revokedcanary" in d for d in docs)
    fake_rerank(monkeypatch, sent)
    assert_gone_for_new_requests(w)


def test_revoke_committed_before_the_response_lock_withholds_it(w, monkeypatch):
    original = search_module._lock_hits

    def revoke_then_lock(db, rows, collection_id):
        revoke_in_own_session(w.api, w.ok)
        return original(db, rows, collection_id)
    monkeypatch.setattr("app.search._lock_hits", revoke_then_lock)
    response = search(w.admin)
    assert (response.status_code, response.json()) == (409, {"error": "search_scope_changed"})
    monkeypatch.setattr("app.search._lock_hits", original)
    assert_gone_for_new_requests(w)


def test_revoke_waiting_on_the_response_lock_lets_that_response_finish(w, monkeypatch):
    original = search_module._lock_hits
    done = {}

    def lock_then_revoke(db, rows, collection_id):
        original(db, rows, collection_id)
        thread = Thread(target=lambda: done.setdefault("status", revoke(w.api.admin_browser(), w.ok).status_code))
        thread.start()
        wait_for_lock_wait(w.api)  # the revoke waits on the item lock this response holds
        done["thread"] = thread
    monkeypatch.setattr("app.search._lock_hits", lock_then_revoke)
    response = search(w.admin)
    # The response passed its last check before t_rev and is sent whole (a stated limit).
    assert hit_items(response) == {w.ok}
    done["thread"].join(10)
    assert done["status"] == 200
    monkeypatch.setattr("app.search._lock_hits", original)
    assert_gone_for_new_requests(w)


# ----------------------------------------------------------------------------- helpers shared with test_common_rag


def common_chunk_texts(w) -> set[str]:
    with w.api.engine.connect() as conn:
        return set(conn.scalars(text("SELECT text FROM chunks WHERE item_id=:i"), {"i": uuid.UUID(w.ok)}).all())
