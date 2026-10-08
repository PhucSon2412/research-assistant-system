"""M6b-1 Admin approval into a separate, pending common copy (PostgreSQL + MinIO).

Since C2d-5b the HTTP approve is runtime (own proofs/copy/acceptance Sessions);
fault hooks target ``app.api.review_approval`` instead of the removed legacy
``promotion.approve`` internals, with the same product invariants.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api import review_approval
from app.api.routes import reviews
from app.cli import reconcile_originals
from app.main import app
from app.models.enums import AuditOutcome
from tests.test_review_requests import (A_BODY, DECLARED_RIGHTS, DECLARED_SOURCE, _fixture_rows, close_journaled,
                                        journal_sql_close, original_url, submit, world, write)
from tests.test_uploads import upload_api  # noqa: F401  (fixture)
from tests.test_c2c1_bootstrap import installation  # noqa: F401  (runtime fixture, C2d-5b)

A_SHA = hashlib.sha256(A_BODY).hexdigest()
ADMIN_SOURCE = "Admin-checked: synthetic file supplied by the test author, identical bytes"
ADMIN_EVIDENCE = "Admin-checked: synthetic written permission EVID-7 for this exact file"
ADMIN_LICENSE = "https://licenses.example.org/synthetic-permission-7"


def approve_body(api, **overrides) -> dict:
    scope = {"audience": "instance_users", "original_download": True, "fulltext_search": True,
             "external_processing": False, "conditions": "Ghi nguồn khi trích dẫn"}
    return {"confirm_verified": True, "file_checksum_sha256": A_SHA, "title": "Bản chung đã kiểm",
            "verified_source": ADMIN_SOURCE, "verified_source_url": "https://source.example.org/p.md",
            "license_url": ADMIN_LICENSE, "evidence_note": ADMIN_EVIDENCE,
            "checked_on": api.clock.now.date().isoformat(), "scope": scope} | overrides


def approve(admin, api, rid, **overrides):
    return write(admin, "POST", f"/admin/review-requests/{rid}/approve", approve_body(api, **overrides))


def object_keys(storage) -> set[str]:
    return {key for key, _ in storage.orphan_keys("")}


def common_count(api) -> int:
    return api.scalar("SELECT count(*) FROM library_items WHERE visibility='common' AND publication_state='pending'")


def nothing_recorded(api) -> bool:
    return (common_count(api) == 0 and api.scalar("SELECT count(*) FROM access_rights") == 0
            and api.scalar("SELECT count(*) FROM review_requests WHERE status='approved'") == 0)


def private_snapshot(api, item_id) -> dict:
    with api.engine.connect() as conn:
        def rows(sql):
            return [tuple(r) for r in conn.execute(text(sql), {"i": uuid.UUID(item_id)}).all()]
        return {
            "item": rows("SELECT id, visibility, owner_user_id, publication_state, content_status, title, rights_epoch,"
                         " active_version_id, active_index_generation_id, updated_at FROM library_items WHERE id=:i"),
            "versions": rows("SELECT * FROM paper_versions WHERE item_id=:i ORDER BY id"),
            "files": rows("SELECT * FROM source_files WHERE item_id=:i ORDER BY id"),
            "chunks": rows("SELECT count(*) FROM chunks WHERE item_id=:i"),
            "generations": rows("SELECT id, state FROM index_generations WHERE item_id=:i ORDER BY id"),
            "collections": rows("SELECT collection_id, item_id FROM collection_items WHERE item_id=:i"),
            "jobs": rows("SELECT id, state FROM ingestion_jobs WHERE item_id=:i"),
        }


class Faulty:
    """Real storage with one hook, to fail or race at an exact step."""

    def __init__(self, storage, *, before_put=None, put=None, read_new=None):
        self.storage, self.before_put, self._put, self.read_new = storage, before_put, put, read_new
        self.keys_put: list[str] = []

    def open(self, key):
        if self.read_new and key in self.keys_put:
            return self.read_new(self.storage, key)
        return self.storage.open(key)

    def put(self, key, file, mime):
        self.keys_put.append(key)
        if self.before_put:
            self.before_put()
        if self._put:
            return self._put(key, file, mime)
        return self.storage.put(key, file, mime)

    def delete(self, key):
        return self.storage.delete(key)


def use_storage(fake):
    app.dependency_overrides[reviews._storage] = lambda: fake


def test_approve_creates_one_pending_common_copy_with_admin_rights(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch, index_a=True)
    collection = write(a, "POST", "/collections", {"name": "A reading"}).json()["collection"]
    assert write(a, "POST", f"/collections/{collection['id']}/items", {"item_id": ids_a["item_id"]}).status_code == 201
    rid = submit(a, ids_a).json()["request"]["id"]
    before = private_snapshot(api, ids_a["item_id"])
    keys_before = object_keys(storage)
    provider_rows = api.scalar("SELECT (SELECT count(*) FROM embedding_calls) + (SELECT count(*) FROM search_usage)")
    assert admin.get(original_url(rid, ids_a["file_id"])).status_code == 200

    response = approve(admin, api, rid)
    assert response.status_code == 200, response.text
    decided = response.json()["request"]
    assert (decided["status"], decided["close_reason_code"], decided["publication"]) == (
        "approved", "approved_by_admin", False)
    assert decided["review_available"] is False and "chờ công bố" in decided["pending_note"]
    common_id = decided["pending_publication_id"]

    # Exactly one common item/version/file with its own object and rights.
    with api.engine.connect() as conn:
        item = conn.execute(text("SELECT * FROM library_items WHERE visibility='common'")).mappings().one()
        version = conn.execute(text("SELECT * FROM paper_versions WHERE item_id=:i"), {"i": item["id"]}).mappings().one()
        common_file = conn.execute(text("SELECT * FROM source_files WHERE item_id=:i"), {"i": item["id"]}).mappings().one()
        rights = conn.execute(text("SELECT * FROM access_rights")).mappings().one()
        admin_id = conn.scalar(text("SELECT id FROM users WHERE role='admin'"))
        private_key = conn.scalar(text("SELECT storage_key FROM source_files WHERE id=:f"),
                                  {"f": uuid.UUID(ids_a["file_id"])})
    assert str(item["id"]) == common_id and item["owner_user_id"] is None
    assert (item["publication_state"], item["content_status"], item["title"]) == (
        "pending", "metadata_only", "Bản chung đã kiểm")
    assert item["active_version_id"] is None and item["active_index_generation_id"] is None
    assert version["source_url"] == "https://source.example.org/p.md" and version["coverage"] is None
    assert common_file["version_id"] == version["id"] and common_file["job_id"] is None
    assert (common_file["checksum_sha256"], common_file["byte_size"], common_file["file_source"]) == (
        A_SHA, len(A_BODY), "review_approval")
    assert common_file["storage_key"] != private_key and common_file["storage_key"].startswith("originals/")
    copied = storage.open(common_file["storage_key"]).read()
    assert copied == A_BODY and hashlib.sha256(copied).hexdigest() == A_SHA
    assert storage.open(private_key).read() == A_BODY
    assert object_keys(storage) == keys_before | {common_file["storage_key"]}
    assert (rights["item_id"], rights["version_id"], rights["source_file_id"]) == (
        item["id"], version["id"], common_file["id"])
    assert (rights["file_checksum_sha256"], rights["reviewer_user_id"], str(rights["origin_review_request_id"])) == (
        A_SHA, admin_id, rid)
    assert rights["checked_on"] == api.clock.now.date() and rights["recorded_at"] == api.clock.now
    assert (rights["verified_source"], rights["license_url"], rights["evidence_note"]) == (
        ADMIN_SOURCE, ADMIN_LICENSE, ADMIN_EVIDENCE)
    assert (rights["scope_audience"], rights["permits_original_download"], rights["permits_fulltext_search"],
            rights["permits_external_processing"], rights["scope_conditions"]) == (
        "instance_users", True, True, False, "Ghi nguồn khi trích dẫn")
    assert rights["rights_epoch_at_grant"] == 0
    # The User's claim is never copied into recorded rights.
    for claim in (DECLARED_SOURCE, DECLARED_RIGHTS, "https://example.org/synthetic.pdf"):
        assert claim not in str(dict(rights))

    # A's private item, versions, files, chunks, index and collection are unchanged.
    assert private_snapshot(api, ids_a["item_id"]) == before
    # No index, job or provider work for the common copy.
    assert api.scalar("SELECT count(*) FROM ingestion_jobs WHERE item_id=:i", i=item["id"]) == 0
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=item["id"]) == 0
    assert api.scalar("SELECT count(*) FROM index_generations WHERE item_id=:i", i=item["id"]) == 0
    assert api.scalar("SELECT (SELECT count(*) FROM embedding_calls) + (SELECT count(*) FROM search_usage)") == \
        provider_rows

    # The review grant is over: the old URL and metadata are gone.
    assert admin.get(original_url(rid, ids_a["file_id"])).status_code == 404
    closed = admin.get(f"/admin/review-requests/{rid}").json()["request"]
    assert closed["status"] == "approved" and closed["pending_publication_id"] == common_id
    assert not {"file", "declaration", "requester_email", "original_url"} & set(closed)
    mine = a.get(f"/review-requests/{rid}").json()["request"]
    assert mine["status"] == "approved" and mine["publication"] is False
    assert common_id not in str(mine) and ADMIN_EVIDENCE not in str(mine)

    # Only the dedicated Admin route shows the pending copy and its rights.
    listed = admin.get("/admin/pending-publications").json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == common_id
    detail = admin.get(f"/admin/pending-publications/{common_id}").json()["item"]
    assert (detail["publication_state"], detail["published"]) == ("pending", False)
    assert detail["rights"]["origin_review_request_id"] == rid
    assert detail["rights"]["reviewer_email"] == "admin@example.org"
    assert detail["file"]["checksum_sha256"] == A_SHA and "storage_key" not in str(detail)
    for browser in (a, b):
        assert browser.get("/admin/pending-publications").status_code == 403
        assert browser.get(f"/admin/pending-publications/{common_id}").status_code == 403
    for path in (f"/admin/pending-publications/{uuid.uuid4()}", "/admin/pending-publications/nope",
                 f"/admin/pending-publications/{ids_a['item_id']}"):
        assert admin.get(path).status_code == 404

    # Audit: IDs and codes only.
    with api.engine.connect() as conn:
        audit = conn.execute(text("SELECT outcome, target_id, details FROM audit_events"
                                  " WHERE action='review.request.approve'")).one()
    assert (audit.outcome, audit.target_id) == ("success", rid)
    assert audit.details == {"common_item_id": common_id, "source_file_id": str(common_file["id"]),
                             "access_rights_id": str(rights["id"])}
    dumped = api.scalar("SELECT string_agg(details::text, ' ') FROM audit_events")
    for secret in (ADMIN_SOURCE, ADMIN_EVIDENCE, ADMIN_LICENSE, DECLARED_RIGHTS, A_SHA, "Bản chung", "Reviewable"):
        assert secret not in dumped


def test_pending_copy_is_invisible_on_every_normal_route(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch, index_a=True)
    legacy = api.add_item(owner=None, title="Legacy listed common")
    rid = submit(a, ids_a).json()["request"]["id"]
    common_id = approve(admin, api, rid).json()["request"]["pending_publication_id"]
    common_file = str(api.scalar("SELECT id FROM source_files WHERE item_id=:i", i=uuid.UUID(common_id)))
    common_version = str(api.scalar("SELECT id FROM paper_versions WHERE item_id=:i", i=uuid.UUID(common_id)))
    missing = {"error": "not_found"}

    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        # Existing listed common items keep their visibility.
        assert str(legacy) in str(browser.get("/library/items?limit=100").json()), who
        for scope in ("all", "common", "mine"):
            listed = browser.get(f"/library/items?scope={scope}&limit=100").json()
            assert common_id not in str(listed), (who, scope)
            count = browser.get(f"/library/items/count?scope={scope}").json()["total"]
            assert count == listed["total"], (who, scope)
        assert browser.get("/library/items/count?scope=common").json()["total"] == 1, who
        for path in (f"/library/items/{common_id}", f"/library/items/{common_id}/chunks",
                     f"/library/items/{common_id}/references", f"/library/items/{common_id}/coverage",
                     f"/library/items/{common_id}/metadata-candidates", f"/files/{common_file}/original"):
            response = browser.get(path)
            assert (response.status_code, response.json()) == (404, missing), (who, path)
        assert write(browser, "PATCH", f"/library/items/{common_id}", {"title": "x"}).json() == missing, who
        box = write(browser, "POST", "/collections", {"name": f"{who} box"}).json()["collection"]
        assert write(browser, "POST", f"/collections/{box['id']}/items",
                     {"item_id": common_id}).json() == missing, who
        assert browser.get(f"/collections/{box['id']}/items").json()["total"] == 0, who
        searched = write(browser, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()
        assert common_id not in str(searched) and common_file not in str(searched), who
        assert write(browser, "POST", "/search", {"query": "Reviewable", "mode": "keyword",
                                                  "item_id": common_id}).status_code == 404, who
        assert write(browser, "POST", "/rag/ask", {"item_id": common_id, "question": "Reviewable result",
                                                   "mode": "keyword"}).json() == missing, who
        assert browser.get(f"/discovery/papers/{uuid.uuid4()}/citing-items").status_code == 404, who
    # A's own private copy still works for A, and only for A.
    hits = write(a, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()["hits"]
    assert hits and {h["item_id"] for h in hits} == {ids_a["item_id"]}
    chunk_id = hits[0]["chunk_id"]
    assert a.get(f"/search/chunks/{chunk_id}").status_code == 200
    assert b.get(f"/search/chunks/{chunk_id}").status_code == 404
    assert admin.get(f"/search/chunks/{chunk_id}").status_code == 404
    assert a.get(f"/files/{ids_a['file_id']}/original").content == A_BODY
    # The Admin still has no normal-route read of A's private item or of B's.
    for item in (ids_a["item_id"], ids_b["item_id"]):
        assert admin.get(f"/library/items/{item}").status_code == 404
    assert admin.get(f"/files/{ids_a['file_id']}/original").status_code == 404
    # Nothing derived exists for the pending copy.
    assert api.scalar("SELECT count(*) FROM chunks WHERE item_id=:i OR version_id=:v",
                      i=uuid.UUID(common_id), v=uuid.UUID(common_version)) == 0


def test_approval_needs_an_open_granted_request(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    keys = object_keys(storage)
    closed = {"error": "review_request_closed"}

    rid = submit(a, ids_a).json()["request"]["id"]
    assert write(a, "POST", f"/review-requests/{rid}/withdraw").status_code == 200
    assert approve(admin, api, rid).json() == closed
    rid = submit(a, ids_a).json()["request"]["id"]
    assert write(admin, "POST", f"/admin/review-requests/{rid}/reject", {"reason_code": "other"}).status_code == 200
    assert approve(admin, api, rid).json() == closed
    rid = submit(a, ids_a).json()["request"]["id"]
    with api.engine.begin() as conn:  # a later revoke/delete bumps the item's epoch
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                     {"i": uuid.UUID(ids_a["item_id"])})
    assert approve(admin, api, rid).json() == {"error": "review_grant_inactive"}
    assert nothing_recorded(api) and object_keys(storage) == keys

    assert write(a, "POST", f"/review-requests/{rid}/withdraw").status_code == 200
    rid = submit(a, ids_a).json()["request"]["id"]
    assert approve(admin, api, rid, file_checksum_sha256="0" * 64).json() == {
        "error": "checksum_confirmation_mismatch"}
    assert approve(b, api, rid).status_code == 403
    assert approve(admin, api, uuid.uuid4()).json() == {"error": "not_found"}
    assert approve(admin, api, "not-a-uuid").json() == {"error": "not_found"}
    assert write(admin, "POST", f"/admin/review-requests/{rid}/approve", approve_body(api), origin=None).status_code == 403
    assert write(admin, "POST", f"/admin/review-requests/{rid}/approve", approve_body(api), csrf=False).status_code == 403
    assert nothing_recorded(api)

    assert approve(admin, api, rid).status_code == 200
    # Repeats and later closes change nothing.
    assert approve(admin, api, rid).json() == closed
    assert write(admin, "POST", f"/admin/review-requests/{rid}/reject", {"reason_code": "other"}).json() == closed
    assert write(a, "POST", f"/review-requests/{rid}/withdraw").json() == closed
    assert common_count(api) == 1 and api.scalar("SELECT count(*) FROM access_rights") == 1
    assert object_keys(storage) - keys and len(object_keys(storage) - keys) == 1
    # The owner may submit the same file again; that is a new request, not a second copy.
    rid2 = submit(a, ids_a).json()["request"]["id"]
    assert admin.get(original_url(rid2, ids_a["file_id"])).status_code == 200
    reasons = api.scalar("""SELECT string_agg(details->>'reason_code', ',' ORDER BY id) FROM audit_events
        WHERE action='review.request.approve' AND outcome='denied'""")
    assert reasons.split(",") == ["request_closed", "request_closed", "grant_inactive",
                                  "checksum_confirmation_mismatch", "request_closed"]


def test_admin_must_record_own_evidence_and_scope(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    scope = approve_body(api)["scope"]
    tomorrow2 = (api.clock.now.date() + timedelta(days=2)).isoformat()
    for overrides, code in (({"license_url": None, "evidence_note": "  "}, "rights_evidence_required"),
                            ({"scope": scope | {"fulltext_search": False, "external_processing": True}},
                             "rights_scope_inconsistent"),
                            ({"checked_on": tomorrow2}, "checked_on_in_future")):
        assert approve(admin, api, rid, **overrides).json() == {"error": code}
    for overrides in ({"confirm_verified": False}, {"verified_source": "   "}, {"title": ""},
                      {"license_url": "javascript:alert(1)"}, {"declared_rights": DECLARED_RIGHTS},
                      {"scope": scope | {"audience": "public"}}, {"scope": {"audience": "instance_users"}},
                      {"file_checksum_sha256": "ABC"}):
        rejected = approve(admin, api, rid, **overrides)
        assert rejected.status_code == 422, overrides
        assert "javascript" not in rejected.text
    assert nothing_recorded(api)
    # Either a license URL or an evidence note is enough; free-reading alone is not asked for.
    assert approve(admin, api, rid, license_url=None).status_code == 200
    assert api.scalar("SELECT license_url IS NULL AND evidence_note IS NOT NULL FROM access_rights")


def test_concurrent_approvals_create_one_copy(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    admin2 = api.admin_browser()
    rid = submit(a, ids_a).json()["request"]["id"]
    keys = object_keys(storage)
    barrier = Barrier(2)
    for browser in (admin, admin2):
        browser.csrf()

    def race(browser):
        barrier.wait()
        return approve(browser, api, rid)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(race, (admin, admin2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert common_count(api) == 1 and api.scalar("SELECT count(*) FROM access_rights") == 1
    assert len(object_keys(storage) - keys) == 1  # the loser's copy was deleted


def test_approve_racing_withdraw_has_one_winner(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    keys = object_keys(storage)
    for _ in range(3):
        rid = submit(a, ids_a).json()["request"]["id"]
        barrier = Barrier(2)
        a.csrf(), admin.csrf()

        def race(action):
            barrier.wait()
            return action()

        with ThreadPoolExecutor(2) as pool:
            withdrawn, approved = pool.map(race, (lambda: write(a, "POST", f"/review-requests/{rid}/withdraw"),
                                                  lambda: approve(admin, api, rid)))
        status = api.scalar("SELECT status FROM review_requests WHERE id=:r", r=uuid.UUID(rid))
        assert sorted([withdrawn.status_code, approved.status_code]) == [200, 409]
        assert (status == "approved") == (approved.status_code == 200)
    copies = api.scalar("SELECT count(*) FROM access_rights")
    assert common_count(api) == copies and len(object_keys(storage) - keys) == copies


@pytest.mark.parametrize("closing", ["withdraw", "reject", "epoch"])
def test_close_committed_during_copy_wins(upload_api, monkeypatch, closing):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    keys = object_keys(storage)
    admin2 = api.admin_browser()
    a.csrf(), admin2.csrf()

    def close():
        if closing == "withdraw":  # the runtime routes, journaled (C2d-5a)
            assert write(a, "POST", f"/review-requests/{rid}/withdraw").status_code == 200
            return
        if closing == "reject":
            assert write(admin2, "POST", f"/admin/review-requests/{rid}/reject",
                         {"reason_code": "other"}).status_code == 200
            return
        with api.engine.begin() as conn:
            conn.execute(text("""UPDATE library_items SET rights_epoch=rights_epoch+1
                WHERE id=(SELECT item_id FROM review_requests WHERE id=:r)"""), {"r": uuid.UUID(rid)})

    fake = Faulty(storage, before_put=close)
    use_storage(fake)
    response = approve(admin, api, rid)
    expected = "review_grant_inactive" if closing == "epoch" else "review_request_closed"
    assert (response.status_code, response.json()) == (409, {"error": expected})
    assert nothing_recorded(api) and len(fake.keys_put) == 1
    assert object_keys(storage) == keys  # the copy nothing references was removed


def test_integrity_and_storage_failures_record_nothing(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    keys = object_keys(storage)
    private_key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"]))

    def put_fails(key, file, mime):
        storage.put(key, file, mime)  # a partial object may exist when the error surfaces
        raise ConnectionError("synthetic S3 failure")

    class Body(io.BytesIO):
        def iter_chunks(self, chunk_size):
            while block := self.read(chunk_size):
                yield block

    for fake, reason in ((Faulty(storage, put=put_fails), "copy_failed"),
                         (Faulty(storage, read_new=lambda real, key: Body(A_BODY.replace(b"seven", b"eight"))),
                          "copy_mismatch")):
        use_storage(fake)
        response = approve(admin, api, rid)
        assert (response.status_code, response.json()) == (503, {"error": "approval_storage_unavailable"})
        assert nothing_recorded(api) and object_keys(storage) == keys, reason
        assert api.scalar("""SELECT details->>'reason_code' FROM audit_events WHERE action='review.request.approve'
            ORDER BY id DESC LIMIT 1""") == reason
    app.dependency_overrides.pop(reviews._storage, None)

    # The private object no longer matches what the owner submitted.
    storage.put(private_key, io.BytesIO(A_BODY.replace(b"seven", b"eight")), "text/markdown")
    response = approve(admin, api, rid)
    assert (response.status_code, response.json()) == (409, {"error": "review_file_integrity_failed"})
    assert nothing_recorded(api) and object_keys(storage) == keys
    assert api.scalar("SELECT status FROM review_requests WHERE id=:r", r=uuid.UUID(rid)) == "open"
    storage.put(private_key, io.BytesIO(A_BODY), "text/markdown")

    # A database error after the copy rolls everything back and removes the copy.
    real_audit = review_approval.record_event

    def audit_fails(db, **kw):
        if kw["outcome"] is AuditOutcome.SUCCESS:
            raise OperationalError("INSERT audit_events", None, Exception("synthetic DB failure"))
        return real_audit(db, **kw)

    monkeypatch.setattr(review_approval, "record_event", audit_fails)
    response = approve(admin, api, rid)
    assert (response.status_code, response.json()) == (503, {"error": "approval_unavailable"})
    assert nothing_recorded(api) and object_keys(storage) == keys
    assert api.scalar("SELECT status FROM review_requests WHERE id=:r", r=uuid.UUID(rid)) == "open"
    monkeypatch.setattr(review_approval, "record_event", real_audit)
    assert admin.get(original_url(rid, ids_a["file_id"])).status_code == 200  # the grant is still intact
    assert approve(admin, api, rid).status_code == 200


def test_uncertain_commit_keeps_referenced_copy_and_reconcile_removes_only_orphans(upload_api, monkeypatch):
    from sqlalchemy.orm import Session
    from app.models import AccessRights
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    keys = object_keys(storage)

    # The commit lands but its acknowledgement is lost.
    class LostAck(Session):
        def commit(self):
            super().commit()
            raise OperationalError("COMMIT", None, Exception("synthetic lost acknowledgement"))

    monkeypatch.setattr(review_approval, "Session", LostAck)
    response = approve(admin, api, rid)
    assert (response.status_code, response.json()) == (503, {"error": "approval_outcome_uncertain"})
    monkeypatch.setattr(review_approval, "Session", Session)
    assert common_count(api) == 1 and api.scalar("SELECT status FROM review_requests") == "approved"
    kept = api.scalar("SELECT storage_key FROM source_files WHERE file_source='review_approval'")
    assert object_keys(storage) == keys | {kept} and storage.open(kept).read() == A_BODY
    assert approve(admin, api, rid).json() == {"error": "review_request_closed"}  # a retry adds nothing

    # The connection dies before any commit: nothing is committed, but the
    # rollback cannot be confirmed, so the copy stays for the orphan sweep.
    # (Legacy promotion answered "uncertain" from a failed SELECT; the runtime
    # outcome knows commit never started, so it is approval_unavailable.)
    rid2 = submit(a, ids_a).json()["request"]["id"]

    class Down(Session):
        down = False

        def flush(self, *args, **kwargs):
            if any(isinstance(o, AccessRights) for o in self.new):
                Down.down = True
                raise OperationalError("INSERT access_rights", None, Exception("synthetic DB down"))
            return super().flush(*args, **kwargs)

        def rollback(self):
            if Down.down:
                Down.down = False
                super().invalidate()
                raise OperationalError("ROLLBACK", None, Exception("synthetic DB down"))
            return super().rollback()

    monkeypatch.setattr(review_approval, "Session", Down)
    response = approve(admin, api, rid2)
    assert (response.status_code, response.json()) == (503, {"error": "approval_unavailable"})
    monkeypatch.setattr(review_approval, "Session", Session)
    assert common_count(api) == 1
    orphans = object_keys(storage) - keys - {kept}
    assert len(orphans) == 1
    orphan = orphans.pop()
    assert api.scalar("SELECT count(*) FROM source_files WHERE storage_key=:k", k=orphan) == 0

    # Within the grace period nothing is swept; after it only the unreferenced copy goes.
    reconcile_originals.main()
    assert orphan in object_keys(storage)
    later = reconcile_originals.utcnow() + timedelta(hours=25)
    monkeypatch.setattr(reconcile_originals, "utcnow", lambda: later)
    reconcile_originals.main()
    assert object_keys(storage) == keys | {kept}
    assert admin.get(original_url(rid2, ids_a["file_id"])).status_code == 200  # rid2 is still open
    assert approve(admin, api, rid2).status_code == 200


# --- schema -----------------------------------------------------------------

RIGHTS = text("""INSERT INTO access_rights(item_id,version_id,source_file_id,file_checksum_sha256,origin_kind,
    origin_review_request_id,verified_source,license_url,evidence_note,checked_on,reviewer_user_id,recorded_at,
    scope_audience,permits_original_download,permits_fulltext_search,permits_external_processing,
    rights_epoch_at_grant)
    VALUES(:item,:version,:file,:hash,'review_request',:rr,'checked',:license,:note,CURRENT_DATE,:reviewer,now(),
    'instance_users',true,:fulltext,:processing,0) RETURNING id""")
APPROVE = text("""UPDATE review_requests SET status='approved', closed_at=now(), closed_by_user_id=:by,
    close_reason_code=:code WHERE id=:rr""")


def _open_request(conn, ids) -> uuid.UUID:
    a = ids["a"]
    return conn.scalar(text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,
        file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at)
        VALUES(:i,:v,:f,:u,'open',:h,0,'n','src','rights',now()) RETURNING id"""),
        {"i": a["item"], "v": a["v1"], "f": a["f1"], "u": a["user"], "h": a["h1"]})


def _common(conn, *, state="pending", key="common-copy", checksum="1" * 64):
    item = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common',:s,'C')"
                            " RETURNING id"), {"s": state})
    version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
    file = conn.scalar(text("""INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,
        checksum_sha256) VALUES(:i,:v,:k,'text/markdown',10,:h) RETURNING id"""),
        {"i": item, "v": version, "k": key, "h": checksum})
    return item, version, file


def test_schema_binds_rights_to_exact_common_file_and_approved_request(api):
    with api.engine.begin() as conn:
        ids = _fixture_rows(conn)
        rr = _open_request(conn, ids)
    admin, a = ids["admin"], ids["a"]

    def params(item, version, file, **overrides):
        return {"item": item, "version": version, "file": file, "hash": a["h1"], "rr": rr, "license": None,
                "note": "evidence", "reviewer": admin, "fulltext": True, "processing": False} | overrides

    def fails(build, message: str | None = None):
        with pytest.raises(IntegrityError) as caught, api.engine.begin() as conn:
            build(conn)
        if message:
            assert message in str(caught.value)

    def full(conn, *, approve_code="approved_by_admin", by=admin, **overrides):
        item, version, file = _common(conn, **{k: overrides.pop(k) for k in ("state", "key", "checksum")
                                              if k in overrides})
        conn.execute(RIGHTS, params(item, version, file, **overrides))
        conn.execute(APPROVE, {"by": by, "code": approve_code, "rr": rr})
        return item

    fails(lambda c: full(c, key="fixture-a-1"), "own object key")            # the private object's key
    fails(lambda c: full(c, checksum="2" * 64, hash="2" * 64),                # not the request's checksum
          "fk_access_rights_review_request_checksum")
    fails(lambda c: full(c, state="listed"), "pending common copy")
    fails(lambda c: full(c, reviewer=a["user"]), "admin reviewer")
    fails(lambda c: full(c, by=a["user"]), "only an admin")
    fails(lambda c: full(c, approve_code="other"), "ck_review_requests_approved")
    fails(lambda c: full(c, note=None), "evidence_required")                 # no license URL or evidence
    fails(lambda c: full(c, fulltext=False, processing=True), "processing_needs_fulltext")
    fails(lambda c: c.execute(RIGHTS, params(a["item"], a["v1"], a["f1"])),  # a private item cannot hold rights
          "pending common copy")

    def file_of_other_version(conn):
        item, version, _ = _common(conn)
        _, _, other = _common(conn, key="other-copy")
        conn.execute(RIGHTS, params(item, version, other))
    fails(file_of_other_version, "fk_access_rights_file_version_checksum")

    # Deferred checks: each of approval, rights and pending item needs the others at commit.
    fails(lambda c: c.execute(APPROVE, {"by": admin, "code": "approved_by_admin", "rr": rr}),
          "needs its access rights")
    fails(lambda c: _common(c), "pending common copy needs its access rights")

    def rights_without_approval(conn):
        item, version, file = _common(conn)
        conn.execute(RIGHTS, params(item, version, file))
    fails(rights_without_approval, "approved")

    fails(full, "needs its revocation journal record")  # complete, but the close is not journaled (0028)
    with api.engine.begin() as conn:
        common = full(conn)
        journal_sql_close(conn, rr)

    def second_rights(conn):  # a second publication candidate from the same request
        item, version, file = _common(conn, key="second-copy")
        conn.execute(RIGHTS, params(item, version, file))
    fails(second_rights, "uq_access_rights_origin_review_request_id")
    # M6b-2a: rights change only by revocation; listing needs a publication record at commit.
    for statement, message in (("UPDATE access_rights SET verified_source='changed'", "only update to access rights"),
                               ("DELETE FROM access_rights", "never deleted"),
                               ("UPDATE library_items SET publication_state='listed' WHERE id=:c",
                                "needs its publication record"),
                               ("UPDATE review_requests SET status='open', closed_at=NULL, closed_by_user_id=NULL,"
                                " close_reason_code=NULL", "final")):
        fails(lambda c, s=statement: c.execute(text(s), {"c": common}), message)
    # Every new common row states its publication state; private rows have none.
    fails(lambda c: c.execute(text("INSERT INTO library_items(visibility,title) VALUES('common','no state')")),
          "publication_state_valid")
    fails(lambda c: c.execute(text("INSERT INTO library_items(visibility,owner_user_id,publication_state)"
                                   " VALUES('private',:o,'listed')"), {"o": a["user"]}), "publication_state_valid")
    assert api.scalar("SELECT count(*) FROM access_rights") == 1
