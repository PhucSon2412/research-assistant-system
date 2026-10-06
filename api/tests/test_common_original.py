"""M6b-2b1: original download of a published common copy under its AccessRights (PostgreSQL + MinIO).

Synthetic Markdown and fixture accounts only. Nothing here indexes a common copy,
creates a job for it or calls a provider.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import io
import logging
import threading
import time
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import common_original
from app.api import original_downloads
from app import config as app_config
from app import publication
from app.api.routes import uploads
from app.authz import Principal
from app.main import app
from app.models.enums import AuditOutcome, RevocationReason
from tests.helpers import ADMIN_EMAIL
from tests.test_ingest_worker import upload
from tests.test_publication import (MISSING, SOURCE_URL, common_file, common_key, publish, revoke,
                                    state)
from tests.test_review_approval import A_SHA, ADMIN_EVIDENCE, ADMIN_LICENSE, ADMIN_SOURCE, approve
from tests.test_review_requests import A_BODY, B_BODY, submit, world, write
from tests.original_fixtures import upload_api, base_upload_api  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # fixture dependency of upload_api

C_BODY = b"## Methods\nCommon copy C with a condition.\n"
D_BODY = b"## Methods\nPending copy D never published.\n"
E_BODY = b"## Methods\nListed copy E, epoch moved.\n"
INTEGRITY = {"error": "common_file_integrity_failed"}
FORBIDDEN_IN_LOGS = ("Reviewable", "seven units", "BSECRET", ADMIN_SOURCE, ADMIN_EVIDENCE, ADMIN_LICENSE, SOURCE_URL,
                     "Bản chung đã kiểm", "Ghi nguồn", A_SHA)


def scope(**overrides) -> dict:
    return {"audience": "instance_users", "original_download": True, "fulltext_search": True,
            "external_processing": False, "conditions": None} | overrides


def copy_of(api, owner, admin, ids, *, published: bool = True, **scope_overrides) -> tuple[str, str]:
    """The owner's file approved (with ``scope``) and, by default, published; returns (item ID, common file ID)."""
    checksum = api.scalar("SELECT checksum_sha256 FROM source_files WHERE id=:f", f=uuid.UUID(ids["file_id"]))
    rid = submit(owner, ids).json()["request"]["id"]
    response = approve(admin, api, rid, file_checksum_sha256=checksum, scope=scope(**scope_overrides))
    assert response.status_code == 200, response.text
    item = response.json()["request"]["pending_publication_id"]
    if published:
        assert publish(admin, item, checksum=checksum).status_code == 200
    return item, common_file(api, item)


def url(file_id) -> str:
    return f"/files/{file_id}/original"


def assert_missing(browser, path, who):
    response = browser.get(path)
    assert (response.status_code, response.json()) == (404, MISSING), (who, path)
    assert "content-disposition" not in response.headers, (who, path)


def download_audits(api, outcome: str | None = None) -> list[dict]:
    with api.engine.connect() as conn:
        rows = conn.execute(text("""SELECT outcome, target_type, target_id, details FROM audit_events
            WHERE action='common_copy.original.download' ORDER BY id""")).all()
    return [dict(r._mapping) for r in rows if outcome is None or r.outcome == outcome]


def no_secrets_in(api, caplog) -> None:
    dumped = " ".join(filter(None, (api.scalar("SELECT string_agg(details::text, ' ') FROM audit_events"),
                                    caplog.text)))
    for secret in FORBIDDEN_IN_LOGS:
        assert secret not in dumped, secret


def use_storage(fake):
    app.dependency_overrides[uploads._storage] = lambda: fake


def admin_principal(api) -> Principal:
    admin_id = api.scalar("SELECT id FROM users WHERE email=:e", e=ADMIN_EMAIL)
    session_id = api.scalar("SELECT id FROM sessions WHERE user_id=:u AND revoked_at IS NULL ORDER BY created_at DESC,id LIMIT 1",u=admin_id)
    return Principal(user_id=admin_id, role="admin", session_id=session_id, email=ADMIN_EMAIL, display_name=None)


def revoke_now(api, item_id) -> None:
    with Session(api.setup_engine, expire_on_commit=False) as db:
        publication.revoke(db, admin_principal(api), uuid.UUID(item_id), RevocationReason.RIGHTS_WITHDRAWN,
                           api.clock.now, timedelta(hours=24))


def test_private_stays_owner_only_and_permitted_common_opens_for_every_account(upload_api, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch)
    copy, file_id = copy_of(api, a, admin, ids_a)
    provider_rows = api.scalar("SELECT (SELECT count(*) FROM embedding_calls) + (SELECT count(*) FROM search_usage)")

    # Private originals: the owner only; Admin has no branch on the normal route.
    assert a.get(url(ids_a["file_id"])).content == A_BODY
    assert b.get(url(ids_b["file_id"])).content == B_BODY
    for who, browser, other in (("b", b, ids_a), ("admin", admin, ids_a), ("a", a, ids_b), ("admin", admin, ids_b)):
        assert_missing(browser, url(other["file_id"]), who)

    # The permitted common copy: A, B and Admin get exactly the reviewed bytes.
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        response = browser.get(url(file_id))
        assert response.status_code == 200, (who, response.text)
        assert response.content == A_BODY and hashlib.sha256(response.content).hexdigest() == A_SHA, who
        assert response.headers["cache-control"] == "no-store", who
        assert response.headers["content-disposition"].startswith("attachment;"), who
        assert response.headers["content-length"] == str(len(A_BODY)), who
        assert "amazonaws" not in str(response.headers) and "originals/" not in str(response.headers), who
        # The item page lists that one file, from the same gate, with its download path.
        detail = browser.get(f"/library/items/{copy}").json()["item"]
        assert detail["content_status"] == "metadata_only" and detail["jobs"] == [], who
        assert [(f["id"], f["original_url"], f["byte_size"]) for f in detail["files"]] == [
            (file_id, url(file_id), len(A_BODY))], who
        assert "storage_key" not in str(detail) and "originals/" not in str(detail), who
        # Download is not full text: chunks, search and RAG stay closed.
        for path in (f"/library/items/{copy}/chunks", f"/library/items/{copy}/coverage",
                     f"/library/items/{copy}/references"):
            assert_missing(browser, path, who)
        assert write(browser, "POST", "/rag/ask", {"item_id": copy, "question": "Reviewable result",
                                                   "mode": "keyword"}).json() == MISSING, who
    assert write(b, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()["hits"] == []
    assert admin.get(f"/admin/common-copies/{copy}").json()["item"]["original_download"] == "open"

    # The private item's own detail keeps listing its file (with a path) for its owner only.
    private = a.get(f"/library/items/{ids_a['item_id']}").json()["item"]["files"]
    assert [(f["id"], f["original_url"]) for f in private] == [(ids_a["file_id"], url(ids_a["file_id"]))]

    audits = download_audits(api)
    assert [r["outcome"] for r in audits] == ["success"] * 3
    assert {(r["target_type"], r["target_id"]) for r in audits} == {("source_file", file_id)}
    assert all(r["details"] == {"item_id": copy} for r in audits)
    for table in ("ingestion_jobs", "chunks", "index_generations"):
        assert api.scalar(f"SELECT count(*) FROM {table} WHERE item_id=:i", i=uuid.UUID(copy)) == 0, table
    assert api.scalar("SELECT (SELECT count(*) FROM embedding_calls) + (SELECT count(*) FROM search_usage)") == \
        provider_rows
    no_secrets_in(api, caplog)


def test_common_original_refused_unless_live_rights_open_that_exact_file(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch)
    allowed, allowed_file = copy_of(api, a, admin, ids_a)
    not_permitted, not_permitted_file = copy_of(api, b, admin, ids_b, original_download=False)
    conditioned, conditioned_file = copy_of(api, a, admin, upload(a, C_BODY), conditions="Ghi nguồn khi trích dẫn")
    pending, pending_file = copy_of(api, a, admin, upload(a, D_BODY), published=False)
    stale, stale_file = copy_of(api, a, admin, upload(a, E_BODY))

    # A legacy common item (listed, no AccessRights) that nevertheless has a file.
    legacy = api.add_item(owner=None, title="Legacy listed common")
    legacy_key = f"originals/{uuid.uuid4().hex}"
    storage.put(legacy_key, io.BytesIO(b"legacy bytes"), "text/markdown")
    # Another version and another file of the permitted copy, which its rights do not name.
    other_key = f"originals/{uuid.uuid4().hex}"
    storage.put(other_key, io.BytesIO(A_BODY), "text/markdown")
    with api.engine.begin() as conn:
        def add_file(item_id, version_id, key, data):
            return str(conn.scalar(text("""INSERT INTO source_files (item_id, version_id, storage_key, verified_mime,
                byte_size, checksum_sha256, original_filename, file_source)
                VALUES (:i, :v, :k, 'text/markdown', :n, :h, 'x.md', 'upload') RETURNING id"""),
                {"i": item_id, "v": version_id, "k": key, "n": len(data), "h": hashlib.sha256(data).hexdigest()}))
        legacy_file = add_file(legacy, conn.scalar(text("SELECT id FROM paper_versions WHERE item_id=:i"),
                                                   {"i": legacy}), legacy_key, b"legacy bytes")
        allowed_version = conn.scalar(text("SELECT version_id FROM source_files WHERE id=:f"),
                                      {"f": uuid.UUID(allowed_file)})
        same_version_other_file = add_file(uuid.UUID(allowed), allowed_version, other_key, A_BODY)
        new_version = conn.scalar(text("INSERT INTO paper_versions (item_id, version_label) VALUES (:i, 'v2')"
                                       " RETURNING id"), {"i": uuid.UUID(allowed)})
        other_version_file = add_file(uuid.UUID(allowed), new_version, other_key, A_BODY)
        # A later rights change on a listed copy: its recorded rights no longer match the epoch.
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": uuid.UUID(stale)})

    refused = {"not_permitted": not_permitted_file, "conditions": conditioned_file, "pending": pending_file,
               "legacy_no_rights": legacy_file, "same_version_other_file": same_version_other_file,
               "other_version_file": other_version_file, "epoch_mismatch": stale_file,
               "absent": str(uuid.uuid4())}
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert browser.get(url(allowed_file)).content == A_BODY, who
        for case, file_id in refused.items():
            assert_missing(browser, url(file_id), f"{who}:{case}")
        assert_missing(browser, "/files/not-a-uuid/original", who)
        # Metadata of every listed copy stays visible; only the permitted one lists a file.
        for item_id in (not_permitted, conditioned, stale):
            detail = browser.get(f"/library/items/{item_id}").json()["item"]
            assert detail["files"] == [] and detail["versions"], (who, item_id)
        assert [f["id"] for f in browser.get(f"/library/items/{allowed}").json()["item"]["files"]] == [allowed_file]
        assert_missing(browser, f"/library/items/{pending}", who)
    status = {i: admin.get(f"/admin/common-copies/{i}").json()["item"]["original_download"]
              for i in (not_permitted, conditioned, pending, stale, allowed)}
    # "open" comes from the download gate itself: the stale-epoch copy's recorded rights still say
    # download, no condition, but the route refuses it, and so does the Admin status.
    assert status == {not_permitted: "not_permitted", conditioned: "blocked_by_conditions", pending: "not_published",
                      stale: "rights_not_current", allowed: "open"}
    # Refusals by the first gate are plain 404s: no audit row, nothing read from storage.
    assert {r["target_id"] for r in download_audits(api)} == {allowed_file}

    # Revocation: from its commit every new request, a saved URL included, is refused.
    saved = url(allowed_file)
    assert revoke(admin, allowed).status_code == 200
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_missing(browser, saved, who)
        assert_missing(browser, f"/library/items/{allowed}", who)
    assert admin.get(f"/admin/common-copies/{allowed}").json()["item"]["original_download"] == "revoked"
    # A's private original is untouched by all of this.
    assert a.get(url(ids_a["file_id"])).content == A_BODY


def test_missing_replaced_or_unreadable_object_sends_no_bytes(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    # One spool slot: every refusal below must give it back, or the last download would be 503.
    monkeypatch.setenv("RAS_FILES__VERIFIED_DOWNLOAD_MAX_CONCURRENT", "1")
    app_config.get_settings.cache_clear()
    copy, file_id = copy_of(api, a, admin, ids_a)
    key = common_key(api, copy)
    private_key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"]))

    same_size = A_BODY.replace(b"seven", b"eight")
    assert len(same_size) == len(A_BODY)
    for data in (same_size, A_BODY + b"extra tail", A_BODY[:-1]):
        storage.put(key, io.BytesIO(data), "text/markdown")
        response = b.get(url(file_id))
        assert (response.status_code, response.json()) == (409, INTEGRITY)
        assert b"eight" not in response.content and b"Reviewable" not in response.content
        assert "content-disposition" not in response.headers
    assert storage.open(private_key).read() == A_BODY  # the private file still matches; it is not consulted
    storage.delete(key)
    response = b.get(url(file_id))
    assert (response.status_code, response.json()) == (409, INTEGRITY)

    class Down:
        def open(self, key):
            raise ConnectionError("synthetic S3 outage")

    class BreaksMidway:
        def open(self, key):
            class Body:
                def iter_chunks(self, chunk_size):
                    yield A_BODY[:10]
                    raise ConnectionError("synthetic reset")

                def close(self):
                    pass
            return Body()

    for fake in (Down(), BreaksMidway()):
        use_storage(fake)
        response = b.get(url(file_id))
        assert (response.status_code, response.json()) == (503, {"error": "file_unavailable"})
        assert A_BODY[:10] not in response.content
    app.dependency_overrides.pop(uploads._storage, None)

    reasons = [r["details"]["reason_code"] for r in download_audits(api, "failure")]
    assert reasons == ["checksum_mismatch"] * 3 + ["object_missing", "storage_unavailable", "storage_unavailable"]
    storage.put(key, io.BytesIO(A_BODY), "text/markdown")
    assert b.get(url(file_id)).content == A_BODY
    assert state(api, copy) == "listed"  # a failed download changes no state


def test_revoke_committed_during_verification_blocks_the_response(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy, file_id = copy_of(api, a, admin, ids_a)

    class RevokingStorage:
        """Revoke commits after the first gate query, while the object is being read."""

        def open(self, key):
            revoke_now(api, copy)
            return storage.open(key)

    use_storage(RevokingStorage())
    try:
        response = b.get(url(file_id))
    finally:
        app.dependency_overrides.pop(uploads._storage, None)
    assert (response.status_code, response.json()) == (404, MISSING)
    assert b"Reviewable" not in response.content
    assert [r["details"].get("reason_code") for r in download_audits(api)] == ["rights_changed"]
    assert state(api, copy) == "revoked"
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_missing(browser, url(file_id), who)


def test_epoch_change_or_session_end_during_verification_blocks_the_response(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy, file_id = copy_of(api, a, admin, ids_a)
    b_id = api.scalar("SELECT id FROM users WHERE email='b@example.org'")

    def during_read(change):
        class Hook:
            def open(self, key):
                with api.engine.begin() as conn:
                    change(conn)
                return storage.open(key)
        use_storage(Hook())

    def end_b_sessions(conn):
        from app.identity.sessions import revoke_user_sessions
        from app.identity.session_commit import commit_auth
        from app.models.enums import SessionRevokeReason
        from sqlalchemy.orm import Session
        with Session(api.engine) as db:
            revoke_user_sessions(db, user_id=b_id, reason=SessionRevokeReason.LOGOUT, now=api.clock.now)
            commit_auth(db)
    during_read(end_b_sessions)
    response = b.get(url(file_id))
    assert (response.status_code, response.json()["error"]) == (401, "session_expired")
    assert b"Reviewable" not in response.content

    during_read(lambda conn: conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                                          {"i": uuid.UUID(copy)}))
    response = a.get(url(file_id))
    assert (response.status_code, response.json()) == (404, MISSING)
    app.dependency_overrides.pop(uploads._storage, None)
    assert_missing(admin, url(file_id), "admin")


def test_revoke_after_the_last_check_waits_and_the_checked_response_completes(upload_api, monkeypatch):
    """The documented limit: a revoke committing after the final check does not stop that response."""
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy, file_id = copy_of(api, a, admin, ids_a)
    real_audit = original_downloads._audit
    revoker: dict = {}

    def audit_then_let_revoke_queue(db, principal, target, outcome, **details):
        if outcome is AuditOutcome.SUCCESS and "thread" not in revoker:
            # The download holds the item FOR SHARE here; the revoke must queue behind it.
            revoker["thread"] = threading.Thread(target=lambda: revoke_now(api, copy))
            revoker["thread"].start()
            deadline = time.monotonic() + 10
            with api.setup_engine.connect() as observer:
                while observer.scalar(text("""SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                        AND wait_event_type='Lock'""")) == 0:
                    observer.rollback()
                    assert time.monotonic() < deadline, "revoke never waited for the download's lock"
                    time.sleep(0.02)
            revoker["waited"] = True
        return real_audit(db, principal, target, outcome, **details)

    monkeypatch.setattr(original_downloads, "_audit", audit_then_let_revoke_queue)
    response = b.get(url(file_id))
    revoker["thread"].join(10)
    assert revoker.get("waited") and not revoker["thread"].is_alive()
    # Audit lock is no longer the response boundary: the queued revoke wins
    # before the NEW postcommit proof. Separate C2d-4c tests cover boundary-win.
    assert response.status_code == 404 and response.json() == MISSING
    assert "content-disposition" not in response.headers
    assert state(api, copy) == "revoked"
    # Every request after t_rev is refused, including the same saved URL.
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_missing(browser, url(file_id), who)


def test_spool_slots_bound_concurrent_verified_downloads(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    monkeypatch.setenv("RAS_FILES__VERIFIED_DOWNLOAD_MAX_CONCURRENT", "1")
    app_config.get_settings.cache_clear()
    copy, file_id = copy_of(api, a, admin, ids_a)
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def open(self, key):
            entered.set()
            assert release.wait(10)
            return storage.open(key)

    use_storage(Slow())
    first: dict = {}
    worker = threading.Thread(target=lambda: first.setdefault("response", a.get(url(file_id))))
    worker.start()
    assert entered.wait(10)
    busy = b.get(url(file_id))
    assert (busy.status_code, busy.json()) == (503, {"error": "download_busy"})
    release.set()
    worker.join(10)
    app.dependency_overrides.pop(uploads._storage, None)
    assert first["response"].status_code == 200 and first["response"].content == A_BODY
    # The slot came back when the first response's spool closed.
    assert b.get(url(file_id)).content == A_BODY
    assert admin.get(url(file_id)).content == A_BODY


def _advance_while_reading(api, storage, **delta):
    class Slow:
        def open(self, key):
            api.clock.advance(**delta)  # the object read takes this long
            return storage.open(key)
    use_storage(Slow())


def test_session_expiring_while_the_object_is_read_gets_no_bytes(upload_api, monkeypatch):
    """The last check reads the clock again: an idle or absolute expiry reached during the read refuses."""
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    _, file_id = copy_of(api, a, admin, ids_a)
    idle = app_config.get_settings().auth.session_idle

    def fresh_b():
        return api.user_browser("b@example.org")  # its session's last activity is clock.now

    def expire_b_sessions_in(**delta):
        with api.setup_engine.begin() as conn:  # privileged TTL corruption, never a runtime grant
            conn.execute(text("""UPDATE sessions SET absolute_expires_at=:t WHERE revoked_at IS NULL
                AND user_id=(SELECT id FROM users WHERE email='b@example.org')"""),
                         {"t": api.clock.now + timedelta(**delta)})

    try:
        # Idle: exactly at the boundary the session is expired (same rule as resolve_session).
        b = fresh_b()
        _advance_while_reading(api, storage, seconds=idle.total_seconds())
        response = b.get(url(file_id))
        assert (response.status_code, response.json()["error"]) == (401, "session_expired")
        assert b"Reviewable" not in response.content and "content-disposition" not in response.headers
        b = fresh_b()
        _advance_while_reading(api, storage, seconds=idle.total_seconds() - 1)
        assert b.get(url(file_id)).content == A_BODY

        # Absolute: the session is valid when the request starts and expires during the read.
        b = fresh_b()
        expire_b_sessions_in(minutes=5)
        _advance_while_reading(api, storage, minutes=5)
        response = b.get(url(file_id))
        assert (response.status_code, response.json()["error"]) == (401, "session_expired")
        assert b"Reviewable" not in response.content
        b = fresh_b()
        expire_b_sessions_in(minutes=5)
        _advance_while_reading(api, storage, minutes=4)
        assert b.get(url(file_id)).content == A_BODY
    finally:
        app.dependency_overrides.pop(uploads._storage, None)


def test_verified_original_frees_slot_and_file_on_every_exit():
    slots = threading.BoundedSemaphore(1)

    def slot_free() -> bool:
        if slots.acquire(blocking=False):
            slots.release()
            return True
        return False

    def held() -> common_original.VerifiedOriginal:
        assert slots.acquire(blocking=False)
        verified = common_original.VerifiedOriginal(slots)
        verified.open_spool().write(b"x" * (3 * common_original.STREAM_CHUNK_BYTES))
        return verified

    # Closed before the first read (a generator's finally would not run here).
    verified = held()
    spool = verified._hold.spool
    assert not slot_free()
    verified.close()
    assert slot_free() and spool.closed and verified.closed
    verified.close()  # idempotent: the semaphore is not over-released
    assert slot_free()
    assert list(verified) == []

    # Closed after one chunk; and read to the end.
    verified = held()
    assert len(next(verified)) == common_original.STREAM_CHUNK_BYTES
    verified.close()
    assert slot_free()
    verified = held()
    assert sum(len(block) for block in verified) == 3 * common_original.STREAM_CHUNK_BYTES
    assert slot_free() and verified.closed

    # Dropped without close (a response that was never sent): the finalizer frees both.
    verified = held()
    spool = verified._hold.spool
    del verified
    gc.collect()
    assert slot_free() and spool.closed


ASGI_TIMEOUT_S = 30


def _asgi_download(api, file_id, session_token, *, spec_version, leave):
    """Drive the ASGI app directly as a client that goes away at an exact point; returns what it received.

    ``leave``: ``start_error`` (ASGI 2.4: the send of the response start fails), ``after_headers`` (ASGI 2.3:
    the disconnect is delivered once the headers arrive), ``after_first_chunk`` (once one body chunk
    arrived). The disconnect is held back by an event until that point, and whatever the app sends after
    it is dropped, as an ASGI 2.3 server drops sends to a closed connection. So what reaches the client
    does not depend on how the stream task, the disconnect listener, the gate middleware's inner task and
    the thread pool are scheduled (the cause of the earlier intermittent failure). A download that does not
    end after the client left fails the test after ``ASGI_TIMEOUT_S``.
    """
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": url(file_id), "raw_path": url(file_id).encode(),
             "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"cookie", f"ras_session={session_token}".encode())]}
    received: list[dict] = []
    requested = {"done": False}

    async def run():
        gone = asyncio.Event()  # set at the point the client leaves

        async def receive():
            if not requested["done"]:
                requested["done"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                if leave == "start_error":
                    raise OSError("synthetic: client closed the connection")
                received.append(message)
                if leave == "after_headers":
                    gone.set()
                return
            if gone.is_set():
                return  # the connection is closed: dropped, never delivered
            received.append(message)
            gone.set()  # after_first_chunk: the client leaves once this chunk has arrived

        try:
            await asyncio.wait_for(app(scope, receive, send), ASGI_TIMEOUT_S)
        except TimeoutError:
            raise AssertionError("the download did not end after the client left") from None
        except Exception:  # ClientDisconnect or a cancelled stream: what matters is what was released
            pass

    asyncio.run(run())
    return received


def test_early_disconnect_or_spool_failure_gives_the_slot_back(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    monkeypatch.setenv("RAS_FILES__VERIFIED_DOWNLOAD_MAX_CONCURRENT", "1")
    app_config.get_settings.cache_clear()
    _, file_id = copy_of(api, a, admin, ids_a)
    spools: list = []
    real_temporary_file = common_original.tempfile.TemporaryFile

    def recording(*args, **kwargs):
        spool = real_temporary_file(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(common_original.tempfile, "TemporaryFile", recording)
    token = b.session_token()

    def bodies(received) -> bytes:
        return b"".join(m.get("body", b"") for m in received if m["type"] == "http.response.body")

    # ASGI 2.4: the first send fails (ClientDisconnect, Starlette skips background tasks).
    received = _asgi_download(api, file_id, token, spec_version="2.4", leave="start_error")
    assert received == [] and spools[-1].closed
    # ASGI 2.3, the client leaves right after the headers: no byte of the body reaches it.
    received = _asgi_download(api, file_id, token, spec_version="2.3", leave="after_headers")
    assert [m["type"] for m in received] == ["http.response.start"] and spools[-1].closed
    # ASGI 2.3, the client leaves after one chunk: it has that chunk (bytes already sent are not recalled).
    received = _asgi_download(api, file_id, token, spec_version="2.3", leave="after_first_chunk")
    assert bodies(received) == A_BODY[:common_original.STREAM_CHUNK_BYTES] and spools[-1].closed
    assert len(spools) == 3 and all(spool.closed for spool in spools)
    # With one slot, a leak from any of these disconnects would make this 503 download_busy.
    assert b.get(url(file_id)).content == A_BODY
    assert all(spool.closed for spool in spools)

    # The temp file cannot be created: controlled 503, and the slot is not kept.
    def failing(*args, **kwargs):
        raise OSError("synthetic: no space left for the spool")

    monkeypatch.setattr(common_original.tempfile, "TemporaryFile", failing)
    response = b.get(url(file_id))
    assert (response.status_code, response.json()) == (503, {"error": "file_unavailable"})
    monkeypatch.setattr(common_original.tempfile, "TemporaryFile", real_temporary_file)
    assert b.get(url(file_id)).content == A_BODY
    assert admin.get(url(file_id)).content == A_BODY
