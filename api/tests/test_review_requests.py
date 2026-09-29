"""M6a owner review requests for one exact private version/file (PostgreSQL + MinIO)."""
from __future__ import annotations

import hashlib
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.routes import reviews
from app.ingest.worker import process_job
from app.main import app
from tests.helpers import WEB_ORIGIN
from tests.test_ingest_worker import upload
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_uploads import upload_api  # noqa: F401  (fixture)

A_BODY = b"## Methods\nReviewable result is seven units.\n"
B_BODY = b"## Methods\nReviewable secret BSECRET.\n"
DECLARED_SOURCE = "Synthetic PDF written for this test by the requester"
DECLARED_RIGHTS = "Synthetic claim: CC BY 4.0 per requester, unverified"


def write(browser, method: str, path: str, body=None, *, origin: str | None = WEB_ORIGIN, csrf: bool = True):
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    if csrf:
        headers["x-csrf-token"] = browser.csrf()
    return browser.client.request(method, path, json=body, headers=headers)


def request_body(browser, item_id, version_id, file_id, **overrides) -> dict:
    return {"acknowledged": True, "notice_version": browser.get("/review-requests/notice").json()["version"],
            "item_id": str(item_id), "version_id": str(version_id), "source_file_id": str(file_id),
            "declared_source": DECLARED_SOURCE, "declared_source_url": "https://example.org/synthetic.pdf",
            "declared_rights": DECLARED_RIGHTS, "declared_license_url": None} | overrides


def submit(browser, ids: dict, **overrides):
    return write(browser, "POST", "/review-requests",
                 request_body(browser, ids["item_id"], ids["version_id"], ids["file_id"], **overrides))


def add_file(api, storage, item_id, data: bytes, version_id: str | None = None) -> tuple[str, str]:
    """Fixture: another file of an existing private item, in a new version unless one is given."""
    key = f"originals/{uuid.uuid4().hex}"
    storage.put(key, io.BytesIO(data), "text/markdown")
    with api.engine.begin() as conn:
        version = uuid.UUID(version_id) if version_id else conn.scalar(text(
            "INSERT INTO paper_versions (item_id, version_label) VALUES (:i, 'v2') RETURNING id"),
            {"i": uuid.UUID(item_id)})
        file_id = conn.scalar(text("""INSERT INTO source_files (item_id, version_id, storage_key, verified_mime,
            byte_size, checksum_sha256, original_filename, file_source)
            VALUES (:i, :v, :k, 'text/markdown', :n, :h, 'second.md', 'upload') RETURNING id"""),
            {"i": uuid.UUID(item_id), "v": version, "k": key, "n": len(data), "h": hashlib.sha256(data).hexdigest()})
    return str(version), str(file_id)


def world(api, storage, monkeypatch, *, index_a: bool = False):
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    if index_a:
        settings = settings_with_fake_key()
        fake_provider(monkeypatch)
        ids_a = upload(a, A_BODY)
        assert process_job(uuid.UUID(ids_a["job_id"]), storage=storage) == "awaiting_index"
        assert process_job(uuid.UUID(ids_a["job_id"]), settings, storage) == "ready"
    else:
        ids_a = upload(a, A_BODY)
    ids_b = upload(b, B_BODY)
    return a, b, admin, ids_a, ids_b


def original_url(request_id, file_id) -> str:
    return f"/admin/review-requests/{request_id}/files/{file_id}/original"


def test_owner_submits_exact_file_and_admin_reviews_only_that_file(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch, index_a=True)
    v2, f2 = add_file(api, storage, ids_a["item_id"], b"# Second version\n\nOther body.\n")
    _, f1b = add_file(api, storage, ids_a["item_id"], b"# Same version, other file\n", ids_a["version_id"])

    created = submit(a, ids_a)
    assert created.status_code == 201, created.text
    rr = created.json()["request"]
    rid = rr["id"]
    assert rr["status"] == "open" and rr["publication"] is False
    assert rr["declaration"]["verified"] is False and "chưa được Admin xác minh" in rr["declaration"]["note"]
    assert rr["file"]["id"] == ids_a["file_id"] and rr["version"]["id"] == ids_a["version_id"]
    assert rr["file"]["checksum_sha256"] == hashlib.sha256(A_BODY).hexdigest()

    # Wrong pairs and other owners' files look exactly like missing ones.
    missing = {"error": "not_found"}
    for body in (request_body(a, ids_a["item_id"], v2, ids_a["file_id"]),        # file not in that version
                 request_body(a, ids_a["item_id"], ids_a["version_id"], f2),    # file of another version
                 request_body(a, ids_a["item_id"], v2, f1b),
                 request_body(a, ids_b["item_id"], ids_a["version_id"], ids_a["file_id"]),
                 request_body(a, ids_b["item_id"], ids_b["version_id"], ids_b["file_id"]),
                 request_body(a, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())):
        denied = write(a, "POST", "/review-requests", body)
        assert (denied.status_code, denied.json()) == (404, missing)
    assert submit(b, ids_a).json() == missing
    assert submit(a, ids_a).json() == {"error": "review_request_open"}

    # B sees nothing of A's request by ID, list, count or item filter.
    for path in (f"/review-requests/{rid}", f"/review-requests/{uuid.uuid4()}", "/review-requests/not-a-uuid"):
        assert (b.get(path).status_code, b.get(path).json()) == (404, missing)
    assert b.get("/review-requests").json()["total"] == 0
    assert b.get(f"/review-requests?item_id={ids_a['item_id']}").json() == {
        "requests": [], "total": 0, "limit": 20, "offset": 0}
    assert write(b, "POST", f"/review-requests/{rid}/withdraw").json() == missing
    for path in ("/admin/review-requests", f"/admin/review-requests/{rid}", original_url(rid, ids_a["file_id"])):
        assert b.get(path).status_code == 403
    assert a.get("/review-requests").json()["total"] == 1

    # Admin: list/detail with the review metadata, and exactly one original.
    listed = admin.get("/admin/review-requests").json()
    assert listed["total"] == 1 and listed["requests"][0]["id"] == rid
    detail = admin.get(f"/admin/review-requests/{rid}").json()["request"]
    assert detail["review_available"] is True and detail["requester_email"] == "a@example.org"
    assert detail["original_url"] == original_url(rid, ids_a["file_id"])
    assert "chunks" not in str(detail) and "coverage" not in detail["version"]
    opened = admin.get(detail["original_url"])
    assert opened.status_code == 200 and opened.content == A_BODY
    assert opened.headers["cache-control"] == "no-store"
    assert opened.headers["content-disposition"].startswith("attachment;")
    for path in (original_url(rid, f2), original_url(rid, f1b), original_url(rid, ids_b["file_id"]),
                 original_url(uuid.uuid4(), ids_a["file_id"]), original_url("x", "y")):
        assert (admin.get(path).status_code, admin.get(path).json()) == (404, missing)

    # The normal routes keep refusing the Admin A's private data.
    chunk_id = api.scalar("SELECT id FROM chunks WHERE item_id=:i LIMIT 1", i=uuid.UUID(ids_a["item_id"]))
    assert a.get(f"/search/chunks/{chunk_id}").status_code == 200
    for path in (f"/files/{ids_a['file_id']}/original", f"/files/{f2}/original", f"/files/{f1b}/original",
                 f"/library/items/{ids_a['item_id']}", f"/library/items/{ids_a['item_id']}/chunks",
                 f"/library/items/{ids_a['item_id']}/references", f"/library/items/{ids_a['item_id']}/coverage",
                 f"/search/chunks/{chunk_id}", f"/review-requests/{rid}"):
        assert admin.get(path).status_code == 404, path
    assert ids_a["item_id"] not in str(admin.get("/library/items").json())
    assert admin.get("/library/items/count").json()["total"] == 0
    assert admin.get("/review-requests").json()["total"] == 0
    searched = write(admin, "POST", "/search", {"query": "Reviewable seven", "mode": "keyword"})
    assert searched.status_code == 200 and searched.json()["hits"] == []
    assert write(admin, "POST", "/search", {"query": "Reviewable", "mode": "keyword",
                                            "item_id": ids_a["item_id"]}).status_code == 404
    assert write(admin, "POST", "/rag/ask", {"item_id": ids_a["item_id"], "question": "Reviewable result",
                                             "mode": "keyword"}).json() == missing
    collection = write(admin, "POST", "/collections", {"name": "Review scratch"}).json()["collection"]
    assert write(admin, "POST", f"/collections/{collection['id']}/items",
                 {"item_id": ids_a["item_id"]}).status_code == 404
    # The owner's own access is unchanged.
    assert a.get(f"/files/{ids_a['file_id']}/original").content == A_BODY
    assert a.get(f"/library/items/{ids_a['item_id']}").status_code == 200


def test_withdraw_and_reject_end_review_for_new_requests(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    old_url = original_url(rid, ids_a["file_id"])
    assert admin.get(old_url).status_code == 200

    withdrawn = write(a, "POST", f"/review-requests/{rid}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["request"]["status"] == "withdrawn"
    assert withdrawn.json()["request"]["close_reason_code"] == "withdrawn_by_requester"
    assert admin.get(old_url).status_code == 404
    closed = admin.get(f"/admin/review-requests/{rid}").json()["request"]
    assert closed["review_available"] is False and closed["status"] == "withdrawn"
    assert not {"item", "file", "version", "declaration", "requester_email", "original_url"} & set(closed)
    assert admin.get("/admin/review-requests").json()["total"] == 0
    closed_list = admin.get("/admin/review-requests?status=closed").json()
    assert closed_list["total"] == 1
    assert DECLARED_RIGHTS not in str(closed_list) and "paper.md" not in str(closed_list)
    assert write(a, "POST", f"/review-requests/{rid}/withdraw").json() == {"error": "review_request_closed"}
    assert write(admin, "POST", f"/admin/review-requests/{rid}/reject",
                 {"reason_code": "other"}).json() == {"error": "review_request_closed"}

    # Resubmitting the same file is a new request; the old URL stays dead.
    second = submit(a, ids_a)
    assert second.status_code == 201
    rid2 = second.json()["request"]["id"]
    assert rid2 != rid
    assert admin.get(original_url(rid2, ids_a["file_id"])).status_code == 200
    assert admin.get(old_url).status_code == 404

    assert write(admin, "POST", f"/admin/review-requests/{rid2}/reject",
                 {"reason_code": "not-a-code"}).status_code == 422
    rejected = write(admin, "POST", f"/admin/review-requests/{rid2}/reject",
                     {"reason_code": "rights_not_established", "note": "  Thiếu bằng chứng giấy phép.  "})
    assert rejected.status_code == 200 and rejected.json()["request"]["status"] == "rejected"
    assert admin.get(original_url(rid2, ids_a["file_id"])).status_code == 404
    assert write(admin, "POST", f"/admin/review-requests/{rid2}/reject",
                 {"reason_code": "other"}).json() == {"error": "review_request_closed"}
    assert write(a, "POST", f"/review-requests/{rid2}/withdraw").json() == {"error": "review_request_closed"}
    mine = a.get(f"/review-requests/{rid2}").json()["request"]
    assert (mine["status"], mine["close_reason_code"], mine["rejection_note"]) == (
        "rejected", "rights_not_established", "Thiếu bằng chứng giấy phép.")
    assert write(b, "POST", f"/admin/review-requests/{rid2}/reject", {"reason_code": "other"}).status_code == 403
    assert submit(a, ids_a).status_code == 201
    statuses = api.scalar("SELECT string_agg(status, ',' ORDER BY submitted_at, status) FROM review_requests")
    assert sorted(statuses.split(",")) == ["open", "rejected", "withdrawn"]


def test_rights_epoch_change_and_close_during_verification_block_review(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    url = original_url(rid, ids_a["file_id"])

    # A close committed after the first grant check but before any byte is sent wins.
    class ClosingStorage:
        def open(self, key):
            with api.engine.begin() as conn:
                conn.execute(text("""UPDATE review_requests SET status='withdrawn', closed_at=submitted_at,
                    closed_by_user_id=requester_user_id, close_reason_code='withdrawn_by_requester'
                    WHERE id=:r"""), {"r": uuid.UUID(rid)})
            return storage.open(key)

    app.dependency_overrides[reviews._storage] = lambda: ClosingStorage()
    try:
        assert admin.get(url).status_code == 404
    finally:
        app.dependency_overrides.pop(reviews._storage, None)
    assert api.scalar("""SELECT details->>'reason_code' FROM audit_events WHERE action='review.original.view'
        AND outcome='denied'""") == "request_closed"

    # A later revoke/delete that bumps the item's rights epoch also ends the grant.
    rid2 = submit(a, ids_a).json()["request"]["id"]
    assert admin.get(original_url(rid2, ids_a["file_id"])).status_code == 200
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                     {"i": uuid.UUID(ids_a["item_id"])})
    assert admin.get(original_url(rid2, ids_a["file_id"])).status_code == 404
    stale = admin.get(f"/admin/review-requests/{rid2}").json()["request"]
    assert stale["status"] == "open" and stale["review_available"] is False and "file" not in stale
    assert "file" not in admin.get("/admin/review-requests").json()["requests"][0]
    # The owner can still withdraw it and submit again.
    assert write(a, "POST", f"/review-requests/{rid2}/withdraw").status_code == 200
    rid3 = submit(a, ids_a).json()["request"]["id"]
    assert admin.get(original_url(rid3, ids_a["file_id"])).status_code == 200


def test_replaced_object_is_not_served_for_review(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"]))
    storage.put(key, io.BytesIO(A_BODY.replace(b"seven", b"eight")), "text/markdown")  # same size
    response = admin.get(original_url(rid, ids_a["file_id"]))
    assert (response.status_code, response.json()) == (409, {"error": "review_file_integrity_failed"})
    assert b"eight" not in response.content
    assert api.scalar("""SELECT details->>'reason_code' FROM audit_events WHERE action='review.original.view'
        AND outcome='failure'""") == "checksum_mismatch"


def test_concurrent_submissions_leave_one_open_request(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, _, ids_a, _ = world(api, storage, monkeypatch)
    a2 = api.user_browser("a@example.org")
    for _ in range(3):
        barrier = Barrier(2)
        bodies = [request_body(browser, ids_a["item_id"], ids_a["version_id"], ids_a["file_id"])
                  for browser in (a, a2)]
        for browser in (a, a2):
            browser.csrf()

        def race(pair):
            browser, body = pair
            barrier.wait()
            return write(browser, "POST", "/review-requests", body)

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(race, zip((a, a2), bodies)))
        assert sorted(r.status_code for r in results) == [201, 409]
        assert {r.json().get("error") for r in results if r.status_code == 409} == {"review_request_open"}
        assert api.scalar("SELECT count(*) FROM review_requests WHERE status='open'") == 1
        winner = next(r for r in results if r.status_code == 201).json()["request"]["id"]
        assert write(a, "POST", f"/review-requests/{winner}/withdraw").status_code == 200
    assert api.scalar("SELECT count(*) FROM review_requests") == 3
    assert api.scalar("SELECT count(*) FROM audit_events WHERE action='review.request.submit'") == 3


def test_session_csrf_origin_and_role_checks(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    guest = api.browser()
    assert guest.get("/review-requests").status_code == 401
    assert guest.get("/admin/review-requests").status_code == 401
    assert write(guest, "POST", "/review-requests", {}).status_code in (401, 403)
    body = request_body(a, ids_a["item_id"], ids_a["version_id"], ids_a["file_id"])
    assert write(a, "POST", "/review-requests", body, csrf=False).json() == {"error": "csrf_failed"}
    assert write(a, "POST", "/review-requests", body, origin="https://evil.test").json() == {"error": "csrf_failed"}
    assert write(a, "POST", "/review-requests", body, origin=None).json() == {"error": "csrf_failed"}
    assert write(a, "POST", "/review-requests", body | {"notice_version": "old"}).json() == {
        "error": "notice_changed"}
    for bad in ({"acknowledged": False}, {"declared_rights": "   "}, {"declared_source_url": "javascript:x"},
                {"declared_license_url": "https://x.test/a b"}, {"unexpected": True}):
        rejected = write(a, "POST", "/review-requests", body | bad)
        assert rejected.status_code == 422
        assert "javascript" not in rejected.text and "x.test" not in rejected.text
    assert write(admin, "POST", "/review-requests", body).json() == {"error": "forbidden"}
    assert api.scalar("SELECT count(*) FROM review_requests") == 0

    rid = write(a, "POST", "/review-requests", body).json()["request"]["id"]
    assert write(a, "POST", f"/review-requests/{rid}/withdraw", origin=None).status_code == 403
    assert write(a, "POST", f"/review-requests/{rid}/withdraw", csrf=False).status_code == 403
    assert write(admin, "POST", f"/admin/review-requests/{rid}/reject", {"reason_code": "other"},
                 origin=None).status_code == 403
    assert write(admin, "POST", f"/admin/review-requests/{rid}/reject", {"reason_code": "other"},
                 csrf=False).status_code == 403
    assert write(admin, "POST", f"/review-requests/{rid}/withdraw").status_code == 404
    assert api.scalar("SELECT status FROM review_requests") == "open"
    api.clock.advance(minutes=31)  # idle session expiry
    assert admin.get(original_url(rid, ids_a["file_id"])).status_code == 401


def test_audit_holds_ids_and_reason_codes_only(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    assert admin.get(original_url(rid, ids_a["file_id"])).status_code == 200
    assert write(a, "POST", f"/review-requests/{rid}/withdraw").status_code == 200
    assert admin.get(original_url(rid, ids_a["file_id"])).status_code == 404
    rid2 = submit(a, ids_a).json()["request"]["id"]
    assert write(admin, "POST", f"/admin/review-requests/{rid2}/reject",
                 {"reason_code": "insufficient_evidence", "note": "Ghi chú riêng NOTE-SECRET"}).status_code == 200
    with api.engine.connect() as conn:
        rows = conn.execute(text("""SELECT action, outcome, target_id, details FROM audit_events
            WHERE target_type='review_request' ORDER BY id""")).all()
    assert [(r.action, r.outcome, r.target_id) for r in rows] == [
        ("review.request.submit", "success", rid), ("review.original.view", "success", rid),
        ("review.request.withdraw", "success", rid), ("review.original.view", "denied", rid),
        ("review.request.submit", "success", rid2), ("review.request.reject", "success", rid2)]
    assert rows[0].details == {"item_id": ids_a["item_id"], "version_id": ids_a["version_id"],
                               "source_file_id": ids_a["file_id"]}
    assert rows[1].details == {"source_file_id": ids_a["file_id"]}
    assert rows[3].details == {"reason_code": "request_closed"}
    assert rows[5].details == {"reason_code": "insufficient_evidence"}
    dumped = str(rows)
    for secret in (DECLARED_SOURCE, DECLARED_RIGHTS, "NOTE-SECRET", "synthetic.pdf", "Reviewable", "paper.md",
                   hashlib.sha256(A_BODY).hexdigest()):
        assert secret not in dumped


def _fixture_rows(conn):
    """Two owners' items with files, inserted directly for constraint tests."""
    ids = {}
    for name in ("a", "b"):
        user = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES(:e,:v) RETURNING id"),
                           {"e": f"{name}-schema@example.org", "v": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title)"
                                " VALUES('private',:o,'Fixture') RETURNING id"), {"o": user})
        v1 = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        v2 = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        files = [conn.scalar(text("""INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,
            byte_size,checksum_sha256) VALUES(:i,:v,:k,'text/markdown',10,:h) RETURNING id"""),
            {"i": item, "v": v, "k": f"fixture-{name}-{n}", "h": str(n) * 64}) for n, v in ((1, v1), (2, v2))]
        ids[name] = {"user": user, "item": item, "v1": v1, "v2": v2, "f1": files[0], "f2": files[1],
                     "h1": "1" * 64, "h2": "2" * 64}
    ids["admin"] = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('adm-schema@example.org',"
                                    "'$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA','admin') RETURNING id"))
    ids["common"] = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common','listed','C') RETURNING id"))
    return ids


INSERT = text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,
    file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at)
    VALUES(:item,:version,:file,:user,:status,:hash,0,'v1','src','rights',now()) RETURNING id""")


def test_schema_binds_item_version_file_owner_and_final_states(api):
    with api.engine.begin() as conn:
        ids = _fixture_rows(conn)
    a, b = ids["a"], ids["b"]
    good = {"item": a["item"], "version": a["v1"], "file": a["f1"], "user": a["user"], "status": "open",
            "hash": a["h1"]}
    bad_rows = [good | {"version": b["v1"]},                      # version of another item
                good | {"file": a["f2"]},                         # file of another version
                good | {"version": a["v2"], "file": a["f1"]},
                good | {"hash": "f" * 64},                        # checksum not the file's
                good | {"user": b["user"]},                       # requester is not the owner
                good | {"user": ids["admin"]},
                good | {"item": ids["common"]},                   # common items have no owner
                good | {"status": "rejected"}]                    # requests are born open
    for row in bad_rows:
        with pytest.raises(IntegrityError), api.engine.begin() as conn:
            conn.execute(INSERT, row)
    with api.engine.begin() as conn:
        first = conn.scalar(INSERT, good)
    with pytest.raises(IntegrityError), api.engine.begin() as conn:
        conn.execute(INSERT, good)                                # second open request for that file
    with api.engine.begin() as conn:                              # another version of the same item is fine
        conn.scalar(INSERT, good | {"version": a["v2"], "file": a["f2"], "hash": a["h2"]})

    close = "UPDATE review_requests SET status=:s, closed_at=now(), closed_by_user_id=:by, close_reason_code=:c"
    for params, extra in (({"s": "withdrawn", "by": b["user"], "c": "withdrawn_by_requester"}, ""),
                          ({"s": "rejected", "by": a["user"], "c": "other"}, ""),        # not an admin
                          ({"s": "rejected", "by": ids["admin"], "c": "withdrawn_by_requester"}, ""),
                          ({"s": "withdrawn", "by": a["user"], "c": "withdrawn_by_requester"},
                           ", rejection_note='x'")):
        with pytest.raises(IntegrityError), api.engine.begin() as conn:
            conn.execute(text(close + extra + " WHERE id=:r"), params | {"r": first})
    with pytest.raises(IntegrityError), api.engine.begin() as conn:
        conn.execute(text("UPDATE review_requests SET source_file_id=:f WHERE id=:r"), {"f": a["f2"], "r": first})
    with pytest.raises(IntegrityError), api.engine.begin() as conn:
        conn.execute(text("UPDATE review_requests SET declared_rights='changed' WHERE id=:r"), {"r": first})
    with api.engine.begin() as conn:
        conn.execute(text(close + ", rejection_note='Thiếu giấy phép' WHERE id=:r"),
                     {"s": "rejected", "by": ids["admin"], "c": "rights_not_established", "r": first})
    for statement in ("UPDATE review_requests SET status='open', closed_at=NULL, closed_by_user_id=NULL,"
                      " close_reason_code=NULL, rejection_note=NULL WHERE id=:r",
                      "UPDATE review_requests SET rejection_note='edited' WHERE id=:r"):
        with pytest.raises(IntegrityError), api.engine.begin() as conn:
            conn.execute(text(statement), {"r": first})
    with api.engine.begin() as conn:                              # resubmission after close is allowed
        conn.scalar(INSERT, good)
    assert api.scalar("SELECT count(*) FROM review_requests WHERE status='open'") == 2
