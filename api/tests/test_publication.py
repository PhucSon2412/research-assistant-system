"""M6b-2a publication (metadata only), revocation, cleanup and restore replay (PostgreSQL + MinIO).

``world(index_a=True)`` indexes A's private copy with the fake embedding provider
of ``test_private_index`` (in-memory settings copy, no network); nothing here
calls OpenRouter, and no common copy is ever indexed or sent anywhere.
"""
from __future__ import annotations

import hashlib
import io
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Thread

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db as app_db
from app import publication
from app.api import common_publish
from app.api.routes import publications
from app.main import app
from tests.conftest import drop_database
from tests.test_review_approval import (A_SHA, ADMIN_EVIDENCE, ADMIN_LICENSE, ADMIN_SOURCE, Faulty, approve,
                                        object_keys, private_snapshot)
from tests.test_review_requests import A_BODY, B_BODY, submit, world, write
from tests.original_fixtures import upload_api, base_upload_api  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401

B_SHA = hashlib.sha256(B_BODY).hexdigest()
SOURCE_URL = "https://source.example.org/p.md"
MISSING = {"error": "not_found"}
# Paper text, rights findings and the checksum must never reach audit rows, tombstones or logs.
FORBIDDEN_IN_LOGS = ("Reviewable", "seven units", "BSECRET", ADMIN_SOURCE, ADMIN_EVIDENCE, ADMIN_LICENSE, SOURCE_URL,
                     "Bản chung đã kiểm", A_SHA, B_SHA)


def new_copy(api, owner, admin, ids, checksum=A_SHA) -> str:
    """The owner's request approved into a new pending common copy; returns its item ID."""
    rid = submit(owner, ids).json()["request"]["id"]
    response = approve(admin, api, rid, file_checksum_sha256=checksum)
    assert response.status_code == 200, response.text
    return response.json()["request"]["pending_publication_id"]


def publish(admin, item_id, checksum=A_SHA, **overrides):
    return write(admin, "POST", f"/admin/common-copies/{item_id}/publish",
                 {"confirm_publish": True, "file_checksum_sha256": checksum} | overrides)


def revoke(admin, item_id, reason="rights_recorded_in_error", **overrides):
    return write(admin, "POST", f"/admin/common-copies/{item_id}/revoke",
                 {"confirm_revoke": True, "reason_code": reason} | overrides)


def state(api, item_id) -> str:
    return api.scalar("SELECT publication_state FROM library_items WHERE id=:i", i=uuid.UUID(item_id))


def common_key(api, item_id) -> str:
    return api.scalar("SELECT storage_key FROM source_files WHERE item_id=:i", i=uuid.UUID(item_id))


def common_file(api, item_id) -> str:
    return str(api.scalar("SELECT id FROM source_files WHERE item_id=:i", i=uuid.UUID(item_id)))


def live_publications(api) -> int:
    return api.scalar("SELECT count(*) FROM common_publications WHERE revoked_at IS NULL")


def use_storage(fake):
    app.dependency_overrides[publications._storage] = lambda: fake


def listed_ids(browser, scope="all") -> set[str]:
    return {i["id"] for i in browser.get(f"/library/items?scope={scope}&limit=100").json()["items"]}


def content_routes(item_id, file_id) -> list[str]:
    return [f"/library/items/{item_id}/chunks", f"/library/items/{item_id}/references",
            f"/library/items/{item_id}/coverage", f"/library/items/{item_id}/metadata-candidates",
            f"/files/{file_id}/original"]


def assert_no_content_access(browser, item_id, file_id, who):
    for path in content_routes(item_id, file_id):
        response = browser.get(path)
        assert (response.status_code, response.json()) == (404, MISSING), (who, path)
    searched = write(browser, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()
    assert item_id not in str(searched) and file_id not in str(searched), who
    assert write(browser, "POST", "/search", {"query": "Reviewable", "mode": "keyword",
                                              "item_id": item_id}).json() == MISSING, who
    assert write(browser, "POST", "/rag/ask", {"item_id": item_id, "question": "Reviewable result",
                                               "mode": "keyword"}).json() == MISSING, who


def assert_hidden(browser, item_id, file_id, who):
    for scope in ("all", "common", "mine"):
        listed = browser.get(f"/library/items?scope={scope}&limit=100").json()
        assert item_id not in str(listed), (who, scope)
        assert browser.get(f"/library/items/count?scope={scope}").json()["total"] == listed["total"], (who, scope)
    assert browser.get(f"/library/items/{item_id}").json() == MISSING, who
    assert write(browser, "PATCH", f"/library/items/{item_id}", {"title": "x"}).json() == MISSING, who
    box = write(browser, "POST", "/collections", {"name": f"{who} probe"}).json()["collection"]
    assert write(browser, "POST", f"/collections/{box['id']}/items", {"item_id": item_id}).json() == MISSING, who
    assert_no_content_access(browser, item_id, file_id, who)


def no_secrets_in(api, caplog) -> None:
    dumped = " ".join(filter(None, (
        api.scalar("SELECT string_agg(details::text, ' ') FROM audit_events"),
        api.scalar("SELECT string_agg(t::text, ' ') FROM deletion_tombstones t"),
        caplog.text)))
    for secret in FORBIDDEN_IN_LOGS:
        assert secret not in dumped, secret


def test_publish_lists_metadata_only_and_nothing_else(upload_api, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch, index_a=True)
    legacy = str(api.add_item(owner=None, title="Legacy listed common"))
    copy = new_copy(api, a, admin, ids_a)
    file_id = common_file(api, copy)
    before = private_snapshot(api, ids_a["item_id"])
    provider_rows = api.scalar("SELECT (SELECT count(*) FROM embedding_calls) + (SELECT count(*) FROM search_usage)")

    # Before publishing, nobody sees the pending copy on a normal route.
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_hidden(browser, copy, file_id, who)
        assert legacy in listed_ids(browser), who

    response = publish(admin, copy)
    assert response.status_code == 200, response.text
    out = response.json()["item"]
    assert (out["publication_state"], out["published"], out["content_status"], out["fulltext_available"]) == (
        "listed", True, "metadata_only", False)
    # C2d-2b updated this shared note for the current separate search gates.
    assert "Đã công bố metadata" in out["note"] and "chỉ khả dụng khi" in out["note"]
    assert out["publication"]["verified_byte_size"] == len(A_BODY) and out["tombstone"] is None
    # Download is recorded, but these rights carry a text condition the system cannot enforce,
    # so the original stays closed (M6b-2b1; the permitted case is in test_common_original).
    assert out["rights"]["scope"]["original_download"] is True and out["rights"]["scope"]["conditions"]
    assert out["original_download"] == "blocked_by_conditions"

    # Every account now sees the metadata, and only the metadata.
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert {copy, legacy} <= listed_ids(browser, "common"), who
        assert browser.get("/library/items/count?scope=common").json()["total"] == 2, who
        detail = browser.get(f"/library/items/{copy}").json()["item"]
        assert (detail["visibility"], detail["content_status"], detail["title"]) == (
            "common", "metadata_only", "Bản chung đã kiểm"), who
        assert detail["files"] == [] and detail["jobs"] == [] and detail["can_edit"] is (who == "admin"), who
        assert [v["source_url"] for v in detail["versions"]] == [SOURCE_URL], who
        assert "verified_source" not in str(detail) and ADMIN_EVIDENCE not in str(detail), who
        assert_no_content_access(browser, copy, file_id, who)
        box = write(browser, "POST", "/collections", {"name": f"{who} reading"}).json()["collection"]
        assert write(browser, "POST", f"/collections/{box['id']}/items", {"item_id": copy}).status_code == 201, who
        listed = browser.get(f"/collections/{box['id']}/items").json()
        assert listed["total"] == 1 and listed["items"][0]["content_status"] == "metadata_only", who
    assert write(b, "PATCH", f"/library/items/{copy}", {"title": "B edit"}).json() == {"error": "forbidden"}

    # A's private copy still searches for A only; nothing was derived or sent for the common copy.
    hits = write(a, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()["hits"]
    assert hits and {h["item_id"] for h in hits} == {ids_a["item_id"]}
    assert write(b, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()["hits"] == []
    for table in ("ingestion_jobs", "chunks", "sections", "index_generations", "pdf_pages", "reference_entries"):
        assert api.scalar(f"SELECT count(*) FROM {table} WHERE item_id=:i", i=uuid.UUID(copy)) == 0, table
    assert api.scalar("SELECT (SELECT count(*) FROM embedding_calls) + (SELECT count(*) FROM search_usage)") == \
        provider_rows
    assert private_snapshot(api, ids_a["item_id"]) == before
    assert state(api, legacy) == "listed"

    # Admin-only views; the pending list is empty now.
    assert admin.get("/admin/pending-publications").json()["total"] == 0
    assert [i["id"] for i in admin.get("/admin/common-copies?state=listed").json()["items"]] == [copy]
    for browser in (a, b):
        for path in ("/admin/common-copies", f"/admin/common-copies/{copy}"):
            assert browser.get(path).status_code == 403
        assert publish(browser, copy).status_code == 403 and revoke(browser, copy).status_code == 403
    with api.setup_engine.connect() as conn:
        audit = conn.execute(text("SELECT outcome, target_id, details FROM audit_events"
                                  " WHERE action='common_copy.publish'")).one()
        rights_id = conn.scalar(text("SELECT id FROM access_rights WHERE item_id=:i"), {"i": uuid.UUID(copy)})
    assert (audit.outcome, audit.target_id) == ("success", copy)
    assert audit.details == {"access_rights_id": str(rights_id), "source_file_id": file_id}
    no_secrets_in(api, caplog)


def test_publish_refuses_missing_or_altered_common_object(upload_api, monkeypatch, caplog):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy = new_copy(api, a, admin, ids_a)
    key = common_key(api, copy)
    private_key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"]))
    integrity = {"error": "common_file_integrity_failed"}

    def last_reason():
        return api.scalar("""SELECT details->>'reason_code' FROM audit_events WHERE action='common_copy.publish'
            ORDER BY id DESC LIMIT 1""")

    # Same size, different bytes: the private file still matches, the common object does not.
    same_size = A_BODY.replace(b"seven", b"eight")
    assert len(same_size) == len(A_BODY)
    storage.put(key, io.BytesIO(same_size), "text/markdown")
    assert storage.open(private_key).read() == A_BODY
    assert publish(admin, copy).json() == integrity and last_reason() == "checksum_mismatch"
    storage.put(key, io.BytesIO(A_BODY + b"x"), "text/markdown")
    assert publish(admin, copy).json() == integrity and last_reason() == "checksum_mismatch"
    storage.delete(key)
    assert publish(admin, copy).json() == integrity and last_reason() == "object_missing"

    class Down:
        def open(self, key):
            raise ConnectionError("synthetic S3 outage")

    use_storage(Down())
    response = publish(admin, copy)
    assert (response.status_code, response.json()) == (503, {"error": "publication_storage_unavailable"})
    app.dependency_overrides.pop(publications._storage, None)
    assert state(api, copy) == "pending" and live_publications(api) == 0
    assert api.scalar("SELECT count(*) FROM audit_events WHERE action='common_copy.publish' AND outcome='failure'") == 4

    storage.put(key, io.BytesIO(A_BODY), "text/markdown")
    assert publish(admin, copy).status_code == 200 and state(api, copy) == "listed"
    no_secrets_in(api, caplog)


def test_publish_needs_confirmation_and_live_rights_at_the_current_epoch(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch)
    legacy = str(api.add_item(owner=None, title="Legacy"))
    copy = new_copy(api, a, admin, ids_a)

    assert publish(admin, copy, checksum="0" * 64).json() == {"error": "checksum_confirmation_mismatch"}
    for overrides in ({"confirm_publish": False}, {"file_checksum_sha256": "ABC"}, {"title": "x"}):
        assert publish(admin, copy, **overrides).status_code == 422, overrides
    assert write(admin, "POST", f"/admin/common-copies/{copy}/publish",
                 {"confirm_publish": True, "file_checksum_sha256": A_SHA}, origin=None).status_code == 403
    assert write(admin, "POST", f"/admin/common-copies/{copy}/publish",
                 {"confirm_publish": True, "file_checksum_sha256": A_SHA}, csrf=False).status_code == 403
    for other in (uuid.uuid4(), "nope", ids_a["item_id"], ids_b["item_id"], legacy):
        assert publish(admin, other).json() == MISSING, other  # private and legacy items are not reviewed copies
        assert revoke(admin, other).json() == MISSING, other
        assert admin.get(f"/admin/common-copies/{other}").json() == MISSING, other
    assert state(api, copy) == "pending"

    # A later rights change (epoch bump) makes the recorded rights stale: no publication.
    with api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"), {"i": uuid.UUID(copy)})
    assert publish(admin, copy).json() == {"error": "rights_not_publishable"}
    with pytest.raises(IntegrityError, match="exactly one live access rights"), api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET publication_state='listed' WHERE id=:i"), {"i": uuid.UUID(copy)})
    reasons = api.scalar("""SELECT string_agg(details->>'reason_code', ',' ORDER BY id) FROM audit_events
        WHERE action='common_copy.publish'""")
    assert reasons.split(",") == ["checksum_confirmation_mismatch", "epoch_mismatch"]

    # A fresh copy publishes once; a repeat is refused and changes nothing.
    fresh = new_copy(api, a, admin, ids_a)
    assert publish(admin, fresh).status_code == 200
    assert publish(admin, fresh).json() == {"error": "already_published"}
    assert live_publications(api) == 1 and state(api, copy) == "pending"


def test_exact_checksum_duplicate_is_refused_and_told_only_to_the_admin(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    first = new_copy(api, a, admin, ids_a)
    rid2 = submit(a, ids_a).json()["request"]["id"]
    second = approve(admin, api, rid2).json()["request"]["pending_publication_id"]
    assert admin.get(f"/admin/common-copies/{second}").json()["item"]["duplicate_published_item_id"] is None

    assert publish(admin, first).status_code == 200
    assert admin.get(f"/admin/common-copies/{second}").json()["item"]["duplicate_published_item_id"] == first
    assert publish(admin, second).json() == {"error": "duplicate_published_checksum"}
    assert state(api, second) == "pending" and live_publications(api) == 1
    # Nothing is merged, and neither User learns about the duplicate or the second copy.
    for browser in (a, b):
        assert first in listed_ids(browser) and second not in listed_ids(browser)
    mine = a.get(f"/review-requests/{rid2}").json()["request"]
    assert second not in str(mine) and first not in str(mine) and "duplicate" not in str(mine)

    # The rule is about live publications: after revoking the first, the second may be published.
    assert revoke(admin, first, "duplicate_copy").status_code == 200
    assert publish(admin, second).status_code == 200
    assert [i["id"] for i in admin.get("/admin/common-copies?state=listed").json()["items"]] == [second]


def test_concurrent_same_checksum_publishes_one_wins_by_the_db_constraint(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    admin2 = api.admin_browser()
    copies = [new_copy(api, a, admin, ids_a) for _ in range(2)]
    # Control: switch off the application pre-check so only the partial unique index decides.
    monkeypatch.setattr(common_publish, "live_duplicate", lambda db, checksum, item_id: None)
    barrier = Barrier(2)
    for browser in (admin, admin2):
        browser.csrf()

    def race(pair):
        browser, item_id = pair
        barrier.wait()
        return publish(browser, item_id)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(race, ((admin, copies[0]), (admin2, copies[1]))))
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json() == {"error": "duplicate_published_checksum"}
    assert live_publications(api) == 1 and sorted(state(api, c) for c in copies) == ["listed", "pending"]

    # Two publishes of the same copy: one wins, the other sees it already published.
    third = new_copy(api, a, admin, ids_a)
    revoke(admin, next(c for c in copies if state(api, c) == "listed"))
    barrier = Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(race, ((admin, third), (admin2, third))))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert {r.json().get("error") for r in results if r.status_code == 409} <= {
        "already_published", "duplicate_published_checksum"}
    assert live_publications(api) == 1


RACE_ROUNDS = 25


def admin_principal(api):
    from app.authz import Principal
    with api.setup_engine.connect() as conn:
        user_id, email = conn.execute(text("SELECT id, email FROM users WHERE role='admin'")).one()
    return Principal(user_id=user_id, role="admin", session_id=uuid.uuid4(), email=email, display_name=None)


def revoke_in_own_session(api, copy):
    """A separate real runtime Admin/session/CSRF HTTP revoke."""
    admin = api.admin_browser()
    admin.csrf()
    response = revoke(admin, copy, 'rights_withdrawn')
    assert response.status_code == 200, response.text
    return response


def last_publish_audit(api) -> tuple[str, str | None]:
    with api.setup_engine.connect() as conn:
        row = conn.execute(text("""SELECT outcome, details->>'reason_code' FROM audit_events
            WHERE action='common_copy.publish' ORDER BY id DESC LIMIT 1""")).one()
    return tuple(row)


def assert_revoked_everywhere(api, copy, browsers, *, published: bool):
    """Final DB state and access after any publish/revoke order: revoked, nothing live, nothing served."""
    item = uuid.UUID(copy)
    assert state(api, copy) == "revoked"
    assert api.scalar("SELECT revoked_at IS NOT NULL FROM access_rights WHERE item_id=:i", i=item)
    assert api.scalar("SELECT count(*) FROM common_publications WHERE item_id=:i AND revoked_at IS NULL", i=item) == 0
    assert api.scalar("SELECT count(*) FROM common_publications WHERE item_id=:i", i=item) == (1 if published else 0)
    assert api.scalar("SELECT count(*) FROM deletion_tombstones WHERE item_id=:i", i=item) == 1
    for who, browser in browsers:
        assert_hidden(browser, copy, common_file(api, copy), who)


def test_revoke_committed_before_publish_locks_answers_common_copy_revoked(upload_api, monkeypatch):
    """Revoke wins: it commits after the publish request started, before publish takes the item lock."""
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy = new_copy(api, a, admin, ids_a)
    real_copy = common_publish._lock_item
    fired = []

    def revoke_then_copy(db, principal, item_id, *, writer=False):
        if not fired:
            fired.append(True)
            revoke_in_own_session(api, copy)  # publish holds no row lock yet
        return real_copy(db, principal, item_id, writer=writer)

    monkeypatch.setattr(common_publish, "_lock_item", revoke_then_copy)
    response = publish(admin, copy)
    assert fired and (response.status_code, response.json()) == (409, {"error": "common_copy_revoked"})
    assert last_publish_audit(api) == ("denied", "revoked")
    assert_revoked_everywhere(api, copy, (("a", a), ("b", b), ("admin", admin)), published=False)


def test_revoke_during_publish_checks_never_answers_rights_not_publishable(upload_api, monkeypatch):
    """The window found in review: a revoke committing while publish checks state and rights.

    Before the fix publish read the item and the rights in two unlocked statements, so a revoke
    committed between them answered ``rights_not_publishable``. Now the checks run on the locked
    item: the revoke either committed first (``common_copy_revoked``) or waits and revokes after.
    """
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy = new_copy(api, a, admin, ids_a)
    real_publishable = common_publish._target
    outcome = {}

    real_accept = common_publish.accept
    def accepting(*args, **kwargs):
        outcome["accepting"] = True
        return real_accept(*args, **kwargs)
    def revoke_between_reads(db, principal, item, confirmed_checksum, **kwargs):
        if outcome.get("accepting") and "thread" not in outcome:
            outcome["thread"] = Thread(target=lambda: revoke_in_own_session(api, copy))
            outcome["thread"].start()
            outcome["thread"].join(.2)
            outcome["revoke_first"] = not outcome["thread"].is_alive()
        return real_publishable(db, principal, item, confirmed_checksum, **kwargs)
    monkeypatch.setattr(common_publish, "accept", accepting)
    monkeypatch.setattr(common_publish, "_target", revoke_between_reads)
    response = publish(admin, copy)
    outcome["thread"].join(30)
    assert not outcome["thread"].is_alive()
    if outcome["revoke_first"]:
        assert (response.status_code, response.json()) == (409, {"error": "common_copy_revoked"})
    else:  # the revoke waited on publish's lock: publish won, then the revoke applied
        assert response.status_code == 200, response.text
    assert last_publish_audit(api) != ("denied", "rights_revoked")
    assert_revoked_everywhere(api, copy, (("a", a), ("b", b), ("admin", admin)),
                              published=not outcome["revoke_first"])


def test_publish_waiting_on_a_revoke_that_holds_the_lock_answers_common_copy_revoked(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    admin2 = api.admin_browser()
    admin2.csrf()
    copy = new_copy(api, a, admin, ids_a)
    real_cancel = publication._cancel_jobs
    outcome = {}

    def lock_waiters() -> int:
        return api.scalar("""SELECT count(*) FROM pg_stat_activity
            WHERE datname = current_database() AND wait_event_type = 'Lock'""")

    def cancel_while_publish_waits(db, item_id, now):
        # Revoke holds the item row lock here; start publish and wait until it blocks on that lock.
        outcome["thread"] = Thread(target=lambda: outcome.update(publish=publish(admin2, copy)))
        outcome["thread"].start()
        for _ in range(200):
            if lock_waiters():
                outcome["waited"] = True
                break
            outcome["thread"].join(0.05)
        return real_cancel(db, item_id, now)

    monkeypatch.setattr(publication, "_cancel_jobs", cancel_while_publish_waits)
    assert revoke(admin, copy).status_code == 200
    outcome["thread"].join(30)
    assert outcome.get("waited") is True
    assert (outcome["publish"].status_code, outcome["publish"].json()) == (409, {"error": "common_copy_revoked"})
    assert last_publish_audit(api) == ("denied", "revoked")
    assert_revoked_everywhere(api, copy, (("a", a), ("b", b), ("admin", admin)), published=False)


def test_publish_holding_the_lock_wins_and_revoke_then_revokes_it(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    admin2 = api.admin_browser()
    admin2.csrf()
    copy = new_copy(api, a, admin, ids_a)
    key = common_key(api, copy)
    outcome = {}

    real_binding = common_publish._binding
    def revoke_during_binding(*args, **kwargs):
        if "thread" not in outcome:
            outcome["thread"] = Thread(target=lambda: outcome.update(revoke=revoke(admin2, copy)))
            outcome["thread"].start()
            outcome["thread"].join(.2)
            outcome["blocked"] = outcome["thread"].is_alive()
        return real_binding(*args, **kwargs)
    monkeypatch.setattr(common_publish, "_binding", revoke_during_binding)
    assert publish(admin, copy).status_code == 200
    outcome["thread"].join(30)
    assert outcome["blocked"] is True and outcome["revoke"].status_code == 200
    assert last_publish_audit(api) == ("success", None)
    assert_revoked_everywhere(api, copy, (("a", a), ("b", b), ("admin", admin)), published=True)
    assert publish(admin, copy).json() == {"error": "common_copy_revoked"}  # and after it, refused


def test_publish_racing_revoke_never_leaves_a_revoked_copy_listed(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    admin2 = api.admin_browser()
    for browser in (admin, admin2):
        browser.csrf()
    seen = {}
    for _ in range(RACE_ROUNDS):
        copy = new_copy(api, a, admin, ids_a)
        barrier = Barrier(2)

        def race(action):
            barrier.wait()
            return action()

        with ThreadPoolExecutor(2) as pool:
            published, revoked = pool.map(race, (lambda: publish(admin, copy), lambda: revoke(admin2, copy)))
        assert revoked.status_code == 200, revoked.text
        outcome = (published.status_code, published.json().get("error"))
        assert outcome in ((200, None), (409, "common_copy_revoked")), outcome
        seen[outcome] = seen.get(outcome, 0) + 1
        assert state(api, copy) == "revoked" and live_publications(api) == 0
        had_publication = api.scalar("SELECT count(*) FROM common_publications WHERE item_id=:i", i=uuid.UUID(copy))
        assert had_publication == (1 if published.status_code == 200 else 0)
    assert sum(seen.values()) == RACE_ROUNDS
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        listed = browser.get("/library/items?scope=common&limit=100").json()
        assert listed["total"] == 0, who  # every raced copy ended revoked and unserved


def test_revoke_hides_the_copy_from_t_rev_and_touches_nothing_else(upload_api, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch, index_a=True)
    legacy = str(api.add_item(owner=None, title="Legacy"))
    copy = new_copy(api, a, admin, ids_a)
    other = new_copy(api, b, admin, ids_b, B_SHA)
    assert publish(admin, copy).status_code == 200 and publish(admin, other, B_SHA).status_code == 200
    file_id = common_file(api, copy)
    box = write(b, "POST", "/collections", {"name": "B reading"}).json()["collection"]
    for item in (copy, other):
        assert write(b, "POST", f"/collections/{box['id']}/items", {"item_id": item}).status_code == 201
    with api.setup_engine.begin() as conn:  # a queued common job must be cancelled (pinned to the reviewed file, 0025)
        job = conn.scalar(text("""INSERT INTO ingestion_jobs(origin, scope_visibility, item_id, version_id,
            source_file_id, idempotency_key, rights_epoch_at_start)
            SELECT 'promotion', 'common', item_id, version_id, source_file_id, 'm6b2a-fixture', 0
            FROM access_rights WHERE item_id=:i RETURNING id"""), {"i": uuid.UUID(copy)})
    before_a, before_b = private_snapshot(api, ids_a["item_id"]), private_snapshot(api, ids_b["item_id"])
    recorded_cols = ("verified_source, verified_source_url, license_url, evidence_note, checked_on, reviewer_user_id,"
                     " recorded_at, scope_audience, permits_original_download, permits_fulltext_search,"
                     " permits_external_processing, scope_conditions, rights_epoch_at_grant, file_checksum_sha256")
    rights_before = api.scalar(f"SELECT ROW({recorded_cols})::text FROM access_rights WHERE item_id=:i",
                               i=uuid.UUID(copy))
    keys = object_keys(storage)

    response = revoke(admin, copy, "rights_withdrawn")
    assert response.status_code == 200, response.text
    assert response.json()["cancelled_jobs"] == 1
    out = response.json()["item"]
    assert (out["publication_state"], out["published"], out["rights"]["revocation_reason_code"]) == (
        "revoked", False, "rights_withdrawn")
    tomb = out["tombstone"]
    assert (tomb["reason_code"], tomb["prior_publication_state"], tomb["purged_at"], tomb["overdue"]) == (
        "rights_withdrawn", "listed", None, False)

    # From the commit, no normal route serves the copy, even though its membership row and object remain.
    assert api.scalar("SELECT count(*) FROM collection_items WHERE item_id=:i", i=uuid.UUID(copy)) == 1
    assert key_present(storage, common_key(api, copy))
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_hidden(browser, copy, file_id, who)
        assert {other, legacy} <= listed_ids(browser), who
    assert [i["id"] for i in b.get(f"/collections/{box['id']}/items").json()["items"]] == [other]
    assert b.get(f"/collections/{box['id']}/items").json()["total"] == 1

    with api.setup_engine.connect() as conn:
        item = conn.execute(text("SELECT rights_epoch, publication_state FROM library_items WHERE id=:i"),
                            {"i": uuid.UUID(copy)}).one()
        rights = conn.execute(text("SELECT revoked_at, revocation_reason_code, rights_epoch_at_revoke"
                                   " FROM access_rights WHERE item_id=:i"), {"i": uuid.UUID(copy)}).one()
        cancelled = conn.execute(text("SELECT cancel_requested, cancel_requested_at FROM ingestion_jobs WHERE id=:j"),
                                 {"j": job}).one()
    assert tuple(item) == (1, "revoked")
    assert rights.revoked_at == api.clock.now and tuple(rights)[1:] == ("rights_withdrawn", 1)
    assert cancelled.cancel_requested is True and cancelled.cancel_requested_at == api.clock.now
    # Only the revocation fields changed; what the Admin recorded stays as written.
    assert api.scalar(f"SELECT ROW({recorded_cols})::text FROM access_rights WHERE item_id=:i",
                      i=uuid.UUID(copy)) == rights_before
    # A's and B's private items, the other common copy and every object are untouched.
    assert private_snapshot(api, ids_a["item_id"]) == before_a and private_snapshot(api, ids_b["item_id"]) == before_b
    assert state(api, other) == "listed" and state(api, legacy) == "listed"
    assert object_keys(storage) == keys
    hits = write(a, "POST", "/search", {"query": "Reviewable seven units", "mode": "keyword"}).json()["hits"]
    assert {h["item_id"] for h in hits} == {ids_a["item_id"]}
    assert a.get(f"/files/{ids_a['file_id']}/original").content == A_BODY

    # Final: repeats and a publish are refused; the DB refuses any way back.
    assert revoke(admin, copy).json() == {"error": "common_copy_revoked"}
    assert publish(admin, copy).json() == {"error": "common_copy_revoked"}
    for statement, message in (
            ("UPDATE library_items SET publication_state='listed' WHERE id=:i", "not allowed"),
            ("UPDATE access_rights SET revoked_at=NULL, revoked_by_user_id=NULL, revocation_reason_code=NULL,"
             " rights_epoch_at_revoke=NULL WHERE item_id=:i", "final"),
            ("UPDATE access_rights SET evidence_note='rewritten' WHERE item_id=:i", "final"),
            ("DELETE FROM deletion_tombstones WHERE item_id=:i", "never deleted"),
            ("UPDATE deletion_tombstones SET reason_code='other' WHERE item_id=:i", "cleanup progress"),
            ("DELETE FROM common_publications WHERE item_id=:i", "never deleted"),
            ("UPDATE library_items SET rights_epoch=0 WHERE id=:i", "never decreases")):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            conn.execute(text(statement), {"i": uuid.UUID(copy)})
    with pytest.raises(DBAPIError), api.setup_engine.begin() as conn:  # FK from retention records, and the trigger
        conn.execute(text("TRUNCATE deletion_tombstones"))
    with pytest.raises(IntegrityError, match="never deleted|append-only"), api.setup_engine.begin() as conn:
        conn.execute(text("TRUNCATE deletion_tombstones CASCADE"))
    with api.setup_engine.connect() as conn:
        audit = conn.execute(text("SELECT details FROM audit_events WHERE action='common_copy.revoke'"
                                  " AND outcome='success'")).scalar_one()
    assert audit == {"reason_code": "rights_withdrawn", "from_state": "listed", "tombstone_id": tomb["id"],
                     "retained_fields": [],
                     "cancelled_jobs": 1}
    no_secrets_in(api, caplog)


def key_present(storage, key) -> bool:
    return key in object_keys(storage)


def test_cancel_pending_copy_revokes_it_without_a_publication(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    copy = approve(admin, api, rid).json()["request"]["pending_publication_id"]
    assert revoke(admin, copy, "rights_recorded_in_error", confirm_revoke=False).status_code == 422
    assert revoke(admin, copy, "not-a-reason").status_code == 422

    response = revoke(admin, copy, "rights_recorded_in_error")
    assert response.status_code == 200 and response.json()["cancelled_jobs"] == 0
    tomb = response.json()["item"]["tombstone"]
    assert tomb["prior_publication_state"] == "pending"
    assert api.scalar("SELECT count(*) FROM common_publications") == 0
    assert admin.get("/admin/pending-publications").json()["total"] == 0
    assert admin.get(f"/admin/pending-publications/{copy}").json() == MISSING
    assert [i["id"] for i in admin.get("/admin/common-copies?state=revoked").json()["items"]] == [copy]
    assert publish(admin, copy).json() == {"error": "common_copy_revoked"}
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_hidden(browser, copy, common_file(api, copy), who)
    # The owner's request stays approved; the owner learns nothing of the copy or its revocation.
    mine = a.get(f"/review-requests/{rid}").json()["request"]
    assert mine["status"] == "approved" and copy not in str(mine) and "revoke" not in str(mine)
    assert a.get(f"/files/{ids_a['file_id']}/original").content == A_BODY


def test_cleanup_removes_only_the_common_object_and_is_safe_to_repeat(upload_api, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    private_key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"]))
    copies = [new_copy(api, a, admin, ids_a) for _ in range(4)]
    assert publish(admin, copies[0]).status_code == 200
    box = write(b, "POST", "/collections", {"name": "B"}).json()["collection"]
    write(b, "POST", f"/collections/{box['id']}/items", {"item_id": copies[0]})
    keys = {c: common_key(api, c) for c in copies}
    # copies[2]'s object also backs a private file of A (a physical object shared by a valid item).
    with api.setup_engine.begin() as conn:
        version = conn.scalar(text("INSERT INTO paper_versions(item_id, version_label) VALUES (:i, 'v2') RETURNING id"),
                              {"i": uuid.UUID(ids_a["item_id"])})
        conn.execute(text("""INSERT INTO source_files(item_id, version_id, storage_key, verified_mime, byte_size,
            checksum_sha256, file_source) VALUES (:i, :v, :k, 'text/markdown', :n, :h, 'upload')"""),
            {"i": uuid.UUID(ids_a["item_id"]), "v": version, "k": keys[copies[2]], "n": len(A_BODY), "h": A_SHA})
    for c in copies[:3]:
        assert revoke(admin, c).status_code == 200
    storage.delete(keys[copies[1]])  # e.g. deleted by hand, or by a run whose commit was lost
    now = api.clock.now

    # Storage down: nothing is marked purged, the error code is kept, the next run retries.
    class Down:
        def size(self, key):
            raise ConnectionError("synthetic S3 outage")

    counts = publication.cleanup_revoked(api.setup_engine, Down(), now, 50)
    assert counts == {"error:storage_unavailable": 2, "shared_kept": 1}
    assert api.scalar("SELECT count(*) FROM deletion_tombstones WHERE purged_at IS NULL AND cleanup_attempts=1"
                      " AND last_cleanup_error='storage_unavailable'") == 2

    counts = publication.cleanup_revoked(api.setup_engine, storage, now + timedelta(minutes=15), 50)
    assert counts == {"deleted": 1, "already_absent": 1}
    remaining = object_keys(storage)
    assert keys[copies[0]] not in remaining and keys[copies[1]] not in remaining
    assert {private_key, keys[copies[2]], keys[copies[3]]} <= remaining  # private, shared and unrevoked kept
    assert storage.open(private_key).read() == A_BODY
    assert api.scalar("SELECT count(*) FROM collection_items WHERE item_id=:i", i=uuid.UUID(copies[0])) == 0
    with api.setup_engine.connect() as conn:
        outcomes = dict(conn.execute(text("SELECT item_id::text, object_outcome FROM deletion_tombstones")).all())
    assert outcomes == {copies[0]: "deleted", copies[1]: "already_absent", copies[2]: "shared_kept"}
    detail = admin.get(f"/admin/common-copies/{copies[0]}").json()["item"]["tombstone"]
    assert detail["object_outcome"] == "deleted" and detail["purged_at"] and detail["cleanup_attempts"] == 2
    assert detail["last_cleanup_error"] is None
    # A's private file sharing the key still opens for A (and only A).
    shared_file = api.scalar("SELECT id FROM source_files WHERE storage_key=:k AND item_id=:i",
                             k=keys[copies[2]], i=uuid.UUID(ids_a["item_id"]))
    assert a.get(f"/files/{shared_file}/original").content == A_BODY
    assert b.get(f"/files/{shared_file}/original").status_code == 404

    # Rerunning is a no-op.
    assert publication.cleanup_revoked(api.setup_engine, storage, now + timedelta(hours=1), 50) == {}
    assert state(api, copies[3]) == "pending"

    # A derived full-text row is removed with the copy (M6b-2b2a; full coverage in test_common_cleanup);
    # while cleanup keeps failing it stays, and the Admin page flags the tombstone overdue after 24 hours.
    with api.setup_engine.begin() as conn:
        conn.execute(text("""INSERT INTO metadata_candidates(item_id, version_id, source_file_id, field_name, value,
            locator_kind, start_offset, end_offset, pipeline_version)
            SELECT item_id, version_id, id, 'title', 'T', 'markdown', 0, 1, 'fixture' FROM source_files
            WHERE item_id=:i"""),
            {"i": uuid.UUID(copies[3])})
    assert revoke(admin, copies[3]).status_code == 200
    assert publication.cleanup_revoked(api.setup_engine, Down(), now + timedelta(hours=2), 50) == {
        "error:storage_unavailable": 1}
    assert api.scalar("SELECT count(*) FROM metadata_candidates WHERE item_id=:i", i=uuid.UUID(copies[3])) == 1
    assert key_present(storage, keys[copies[3]])
    overdue = admin.get(f"/admin/common-copies/{copies[3]}")
    assert overdue.json()["item"]["tombstone"]["last_cleanup_error"] == "storage_unavailable"
    api.clock.advance(hours=25)
    admin = api.admin_browser()  # the old session went idle
    assert admin.get(f"/admin/common-copies/{copies[3]}").json()["item"]["tombstone"]["overdue"] is True
    assert publication.cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
    assert api.scalar("SELECT count(*) FROM metadata_candidates WHERE item_id=:i", i=uuid.UUID(copies[3])) == 0
    assert not key_present(storage, keys[copies[3]])
    no_secrets_in(api, caplog)


def journal_revocations() -> list[dict]:
    """Bodies of the revocation records in this test's journal file (M6c-1)."""
    from app import journal
    from app.config import get_settings

    return [r.body for r in journal.read(get_settings().recovery.journal_path).records if r.kind == "revocation"]


def test_restored_backup_is_gated_until_recover_replays_the_journal(upload_api, monkeypatch, admin_engine, app_env):
    """The M6b-2a gap, closed by M6c-1: a backup older than a revocation serves nothing until
    ``recover`` has replayed the journal (kept outside the database) and cleaned up."""
    from fastapi.testclient import TestClient

    from app import recovery
    from app.config import get_settings

    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy = new_copy(api, a, admin, ids_a)
    assert publish(admin, copy).status_code == 200
    key = common_key(api, copy)
    live_url = api.setup_engine.url
    runtime_live_url = api.engine.url

    # "Backup": a copy of the database taken while the copy was published.
    api.setup_engine.dispose()
    api.engine.dispose()  # distinct runtime harness pool before snapshot termination
    app_db.get_engine().dispose()
    with admin_engine.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                          " WHERE datname=:d AND pid <> pg_backend_pid()"), {"d": live_url.database})
    snapshot_url = live_url.set(database=f"ras_test_{uuid.uuid4().hex[:12]}")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{snapshot_url.database}" TEMPLATE "{live_url.database}"'))
    snapshot = app_db.make_engine(snapshot_url)
    try:
        assert revoke(admin, copy, "rights_withdrawn").status_code == 200
        assert publication.cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
        journal = journal_revocations()
        assert [e["item_id"] for e in journal] == [copy]
        for secret in FORBIDDEN_IN_LOGS:
            assert secret not in str(journal)

        # Restore the old database and the object store backup; the journal is kept.
        app_env(snapshot_url.set(username=runtime_live_url.username, password=runtime_live_url.password))
        storage.put(key, io.BytesIO(A_BODY), "text/markdown")
        with snapshot.connect() as conn:  # the snapshot itself still says listed...
            assert conn.scalar(text("SELECT publication_state FROM library_items WHERE id=:i"),
                               {"i": uuid.UUID(copy)}) == "listed"
        # ...but nothing is served, to anyone, before the replay.
        for browser in (a, b, admin):
            for path in ("/library/items?limit=100", f"/library/items/{copy}", f"/files/{common_file(api, copy)}/original",
                         "/auth/session"):
                response = browser.get(path)
                assert (response.status_code, response.json()) == (503, {"error": "service_recovering"}), path
        assert TestClient(app).get("/readyz").status_code == 503
        assert TestClient(app).get("/healthz").json() == {"status": "ok"}
        assert recovery.evaluate(get_settings(), snapshot).reason == "db_behind_journal"

        from app import journal as auth_journal
        existing_records = len(auth_journal.read(get_settings().recovery.journal_path).records)
        report = recovery.recover(snapshot, storage, get_settings(), api.clock.now)
        # Runtime identity/approval journal prefix is present in the snapshot;
        # both replay passes validate it, alongside the missing revoke.
        assert report.replayed == {"present": 2 * existing_records,
                                   "recovery_begin": 1, "revoked": 1, "recovery_end": 1}
        assert report.cleanup == {"deleted": 1} and report.gate == "ready"
        assert report.reconciliation_complete and not report.safety_ready  # scoped business cutover only
        assert copy not in listed_ids(b) and b.get(f"/library/items/{copy}").json() == MISSING
        with snapshot.connect() as conn:
            row = conn.execute(text("SELECT i.publication_state, i.rights_epoch, r.revocation_reason_code,"
                                    " p.revoked_at IS NOT NULL FROM library_items i JOIN access_rights r"
                                    " ON r.item_id=i.id JOIN common_publications p ON p.item_id=i.id WHERE i.id=:i"),
                               {"i": uuid.UUID(copy)}).one()
            assert tuple(row) == ("revoked", 1, "rights_withdrawn", True)
            # the restored metadata is removed again too
            assert conn.scalar(text("SELECT title FROM library_items WHERE id=:i"), {"i": uuid.UUID(copy)}) is None
        assert not key_present(storage, key)
        existing_records = len(auth_journal.read(get_settings().recovery.journal_path).records)
        again = recovery.recover(snapshot, storage, get_settings(), api.clock.now)  # safe to repeat
        assert again.replayed == {"present": 2 * existing_records + 1,
                                 "recovery_begin": 1, "recovery_end": 1} and again.cleanup == {}
        assert storage.open(api.scalar("SELECT storage_key FROM source_files WHERE id=:f",
                                       f=uuid.UUID(ids_a["file_id"]))).read() == A_BODY
    finally:
        snapshot.dispose()
        app_db.get_engine().dispose()
        app_db.get_engine.cache_clear()
        drop_database(admin_engine, snapshot_url)


def test_schema_keeps_publication_and_revocation_consistent(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    copies = [new_copy(api, a, admin, ids_a) for _ in range(2)]
    admin_id = api.scalar("SELECT id FROM users WHERE role='admin'")
    user_id = api.scalar("SELECT id FROM users WHERE email='a@example.org'")
    ids = [uuid.UUID(c) for c in copies]
    list_sql = "UPDATE library_items SET publication_state='listed' WHERE id=:i"
    record_sql = """INSERT INTO common_publications(item_id, access_rights_id, file_checksum_sha256,
        verified_byte_size, published_at, published_by_user_id)
        SELECT item_id, id, file_checksum_sha256, 10, now(), :by FROM access_rights WHERE item_id=:i"""

    def fails(message, *statements):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            for statement, params in statements:
                conn.execute(text(statement) if isinstance(statement, str) else statement, params)

    fails("only an admin publishes", (list_sql, {"i": ids[0]}), (record_sql, {"i": ids[0], "by": user_id}))
    fails("needs its publication record", ("UPDATE library_items SET publication_state='listed' WHERE id=:i",
                                           {"i": ids[0]}))
    fails("indexed only while listed", ("UPDATE library_items SET content_status='indexed', active_version_id="
                                "(SELECT id FROM paper_versions WHERE item_id=:i) WHERE id=:i", {"i": ids[0]}))
    fails("live access rights", (record_sql, {"i": ids[0], "by": admin_id}))  # still pending
    with api.setup_engine.begin() as conn:
        conn.execute(text(list_sql), {"i": ids[0]})
        conn.execute(text(record_sql), {"i": ids[0], "by": admin_id})
    # Control for the concurrency test: the partial unique index alone refuses a second live checksum.
    fails("uq_common_publications_live_checksum", (list_sql, {"i": ids[1]}),
          (record_sql, {"i": ids[1], "by": admin_id}))
    fails("only update to a publication record",
          ("UPDATE common_publications SET verified_byte_size=11 WHERE item_id=:i", {"i": ids[0]}))

    revoke_item = "UPDATE library_items SET publication_state='revoked', rights_epoch=rights_epoch+:bump WHERE id=:i"
    revoke_rights = ("UPDATE access_rights SET revoked_at=now(), revoked_by_user_id=:by, revocation_reason_code='other',"
                     " rights_epoch_at_revoke=(SELECT rights_epoch FROM library_items WHERE id=:i) WHERE item_id=:i")
    revoke_pub = "UPDATE common_publications SET revoked_at=now() WHERE item_id=:i"
    tombstone = """INSERT INTO deletion_tombstones(target_kind, item_id, reason_code, prior_publication_state,
        rights_epoch, recorded_at, recorded_by_user_id, purge_due_at)
        VALUES ('common_copy', :i, 'other', 'listed', 1, now(), :by, now() + interval '1 day')"""
    p = {"i": ids[0], "by": admin_id, "bump": 1}
    fails("must bump the rights epoch", (revoke_item, p | {"bump": 0}))
    fails("rights and publication revoked", (revoke_item, p), (tombstone, p))
    fails("only an admin revokes", (revoke_item, p), (revoke_rights, p | {"by": user_id}))
    fails("needs its tombstone", (revoke_item, p), (revoke_rights, p), (revoke_pub, p))
    fails("need their common copy revoked", ("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i", p),
          (revoke_rights, p))
    fails("must be revoked", (tombstone, {"i": ids[1], "by": admin_id}))
    # Legacy listed common items have no rights and no transition.
    legacy = api.add_item(owner=None, title="Legacy")
    fails("only a common copy with recorded rights", (revoke_item, {"i": legacy, "bump": 1}))
    # A complete revocation by SQL still lacks its journal record (M6c-1): only `publication.revoke` revokes.
    fails("needs its revocation journal record",
          *((statement, p) for statement in (revoke_item, revoke_rights, revoke_pub, tombstone)))
    assert state(api, copies[0]) == "listed" and live_publications(api) == 1


# --- metadata after revocation (M6b-2a review) -----------------------------------------------------

REMOVABLE = {"title", "source_url", "original_filename", "verified_source", "license_url", "evidence_note",
             "scope_conditions"}
CONDITIONS = "Ghi nguồn khi trích dẫn"
ADMIN_STRINGS = ("Bản chung đã kiểm", SOURCE_URL, ADMIN_SOURCE, ADMIN_EVIDENCE, ADMIN_LICENSE, CONDITIONS)


def copy_metadata(api, item_id) -> dict:
    with api.setup_engine.connect() as conn:
        return dict(conn.execute(text("""SELECT i.title, v.source_url, v.source_identifier, v.version_label,
            f.original_filename, r.verified_source, r.verified_source_url, r.license_url, r.evidence_note,
            r.scope_conditions, r.metadata_scrubbed_at IS NOT NULL AS scrubbed
            FROM library_items i JOIN paper_versions v ON v.item_id=i.id JOIN source_files f ON f.item_id=i.id
            JOIN access_rights r ON r.item_id=i.id WHERE i.id=:i"""), {"i": uuid.UUID(item_id)}).mappings().one())


def private_rows(api, ids) -> dict:
    """Everything of A's private copy and review requests that removal must never touch."""
    with api.setup_engine.connect() as conn:
        return {table: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table} WHERE {col}=:i ORDER BY id"),
                                                       {"i": uuid.UUID(ids["item_id"])}).all()]
                for table, col in (("library_items", "id"), ("paper_versions", "item_id"),
                                   ("source_files", "item_id"), ("review_requests", "item_id"))}


def other_reference_paths(admin, a, b, copy, rid) -> str:
    """Every route or export that could still name the copy after cleanup, concatenated."""
    responses = [admin.get(f"/admin/common-copies/{copy}"), admin.get("/admin/common-copies?state=revoked"),
                 admin.get(f"/admin/review-requests/{rid}"), admin.get("/admin/review-requests?status=closed"),
                 admin.get("/admin/pending-publications"), a.get(f"/review-requests/{rid}"), a.get("/review-requests")]
    for browser in (a, b, admin):
        responses += [browser.get("/library/items?limit=100"), browser.get(f"/library/items/{copy}")]
    return " ".join(r.text for r in responses) + str(journal_revocations())


def test_cleanup_removes_revoked_metadata_by_default(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    copy = approve(admin, api, rid).json()["request"]["pending_publication_id"]
    assert publish(admin, copy).status_code == 200
    box = write(b, "POST", "/collections", {"name": "B"}).json()["collection"]
    write(b, "POST", f"/collections/{box['id']}/items", {"item_id": copy})
    filename = api.scalar("SELECT original_filename FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"]))
    before = private_rows(api, ids_a)

    assert revoke(admin, copy).status_code == 200
    # Blocked from t_rev, while the metadata is still there awaiting cleanup.
    assert copy_metadata(api, copy)["title"] == "Bản chung đã kiểm"
    for who, browser in (("a", a), ("b", b), ("admin", admin)):
        assert_hidden(browser, copy, common_file(api, copy), who)
    assert b.get(f"/collections/{box['id']}/items").json()["total"] == 0

    assert publication.cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
    meta = copy_metadata(api, copy)
    assert meta.pop("scrubbed") is True and set(meta.values()) == {None}
    with api.setup_engine.connect() as conn:
        tomb = conn.execute(text("SELECT purged_at, scrubbed_fields, retained_fields FROM deletion_tombstones")).one()
    assert tomb.purged_at is not None and set(tomb.scrubbed_fields) == REMOVABLE and tomb.retained_fields == []
    # Nowhere in the database: the Admin's strings are gone and the file name survives only on private files.
    dumped = api.dump_all_text()
    for value in ADMIN_STRINGS:
        assert value not in dumped, value
    assert api.scalar("""SELECT count(*) FROM source_files f JOIN library_items i ON i.id=f.item_id
        WHERE f.original_filename=:n AND i.visibility='common'""", n=filename) == 0
    assert api.scalar("SELECT original_filename FROM source_files WHERE id=:f", f=uuid.UUID(ids_a["file_id"])) ==         filename
    # Nor through any API route or the journal.
    seen = other_reference_paths(admin, a, b, copy, rid)
    for value in ADMIN_STRINGS:
        assert value not in seen, value
    detail = admin.get(f"/admin/common-copies/{copy}").json()["item"]
    assert (detail["title"], detail["file"]["filename"], detail["version"]["source_url"]) == (None, None, None)
    assert detail["rights"]["metadata_removed_at"] and detail["retention_bases"] == []
    assert set(detail["tombstone"]["removed_fields"]) == REMOVABLE
    # A's private item, version, file (name included) and review request are untouched.
    assert private_rows(api, ids_a) == before
    assert a.get(f"/files/{ids_a['file_id']}/original").content == A_BODY
    # Removed values cannot be written back, and nothing else of the rights changes.
    for statement, message in (
            ("UPDATE library_items SET title='again' WHERE id=:i", "can only be removed"),
            ("UPDATE paper_versions SET source_url='https://x.test/p' WHERE item_id=:i", "source removed"),
            ("UPDATE source_files SET original_filename='again.md' WHERE item_id=:i", "immutable"),
            ("UPDATE access_rights SET evidence_note='again' WHERE item_id=:i", "final")):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            conn.execute(text(statement), {"i": uuid.UUID(copy)})
    assert publication.cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {}  # rerun is a no-op


def test_revocation_keeps_only_fields_with_a_recorded_basis(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, _ = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    copy = approve(admin, api, rid).json()["request"]["pending_publication_id"]
    assert publish(admin, copy).status_code == 200
    basis = {"policy_source": "Synthetic retention policy RP-7 section 2", "scope": "Admin-only audit, 12 months",
             "reason": "Synthetic: answer a rights holder follow-up about this revocation"}
    keep = [basis | {"field": "title"}, basis | {"field": "evidence_note"}]

    # Invalid bases revoke nothing.
    assert revoke(admin, copy, retain=keep).json() == {"error": "retention_confirmation_required"}
    assert revoke(admin, copy, retain=keep * 2, confirm_retention_basis=True).status_code == 422
    for bad in (basis | {"field": "checksum"}, basis | {"field": "title", "reason": "  "},
                basis | {"field": "title", "value": "x"}):
        assert revoke(admin, copy, retain=[bad], confirm_retention_basis=True).status_code == 422, bad
    assert state(api, copy) == "listed"

    response = revoke(admin, copy, "rights_withdrawn", retain=keep, confirm_retention_basis=True)
    assert response.status_code == 200, response.text
    for who, browser in (("a", a), ("b", b), ("admin", admin)):  # a kept title opens nothing
        assert_hidden(browser, copy, common_file(api, copy), who)
    assert publication.cleanup_revoked(api.setup_engine, storage, api.clock.now, 50) == {"deleted": 1}
    meta = copy_metadata(api, copy)
    assert (meta["title"], meta["evidence_note"]) == ("Bản chung đã kiểm", ADMIN_EVIDENCE)
    assert {k for k, v in meta.items() if k != "scrubbed" and v is not None} == {"title", "evidence_note"}
    detail = admin.get(f"/admin/common-copies/{copy}").json()["item"]
    assert set(detail["tombstone"]["removed_fields"]) == REMOVABLE - {"title", "evidence_note"}
    assert detail["tombstone"]["retained_fields"] == ["evidence_note", "title"]
    assert [(r["field"], r["policy_source"], r["scope"], r["reason"], r["confirmed_by_email"])
            for r in detail["retention_bases"]] == [
        ("evidence_note", basis["policy_source"], basis["scope"], basis["reason"], "admin@example.org"),
        ("title", basis["policy_source"], basis["scope"], basis["reason"], "admin@example.org")]
    dumped = api.dump_all_text()
    for value in (SOURCE_URL, ADMIN_SOURCE, ADMIN_LICENSE, CONDITIONS):
        assert value not in dumped, value
    # Audit keeps field names only, never the kept values or the basis text.
    audits = api.scalar("SELECT string_agg(details::text, ' ') FROM audit_events WHERE action LIKE 'common_copy.%'")
    assert '"retained_fields": ["evidence_note", "title"]' in audits
    for value in ("Bản chung đã kiểm", ADMIN_EVIDENCE, basis["policy_source"], basis["reason"]):
        assert value not in audits, value
    # Bases are append-only and must precede the purge.
    with api.setup_engine.connect() as conn:
        tomb_id = conn.scalar(text("SELECT id FROM deletion_tombstones"))
        admin_id = conn.scalar(text("SELECT id FROM users WHERE role='admin'"))
    late = """INSERT INTO revocation_metadata_retentions(tombstone_id, item_id, field, policy_source, scope, reason,
        confirmed_by_user_id, recorded_at) VALUES (:t, :i, 'license_url', 'p', 's', 'r', :a, now())"""
    for statement, message in ((late, "before cleanup"),
                               ("UPDATE revocation_metadata_retentions SET reason='changed'", "append-only"),
                               ("DELETE FROM revocation_metadata_retentions", "append-only")):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            conn.execute(text(statement), {"t": tomb_id, "i": uuid.UUID(copy), "a": admin_id})
    # The journal carries the bases so a replay keeps the same decision.
    (entry,) = journal_revocations()
    assert [r["field"] for r in entry["retentions"]] == ["evidence_note", "title"]


def test_db_refuses_a_purge_that_leaves_metadata_without_basis(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, admin, ids_a, _ = world(api, storage, monkeypatch)
    copy = new_copy(api, a, admin, ids_a)
    assert revoke(admin, copy).status_code == 200
    purge = """UPDATE deletion_tombstones SET purged_at=now(), object_outcome='no_object', scrubbed_fields='{}',
        retained_fields='{}' WHERE item_id=:i"""
    with pytest.raises(IntegrityError, match="cleanup must remove evidence_note, license_url, original_filename, "
                                             "scope_conditions, source_url, title, verified_source"), \
            api.setup_engine.begin() as conn:
        conn.execute(text(purge), {"i": uuid.UUID(copy)})
    with pytest.raises(IntegrityError, match="field_lists"), api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE deletion_tombstones SET scrubbed_fields='{}' WHERE item_id=:i"),
                     {"i": uuid.UUID(copy)})
    # A pending (not revoked) copy's metadata cannot be removed through these paths.
    live = new_copy(api, a, admin, ids_a)
    for statement, message in (("UPDATE source_files SET original_filename=NULL WHERE item_id=:i", "immutable"),
                               ("UPDATE access_rights SET evidence_note=NULL, metadata_scrubbed_at=now()"
                                " WHERE item_id=:i", "only update to access rights")):
        with pytest.raises(IntegrityError, match=message), api.setup_engine.begin() as conn:
            conn.execute(text(statement), {"i": uuid.UUID(live)})
    # The private original's name is never removable.
    with pytest.raises(IntegrityError, match="immutable"), api.setup_engine.begin() as conn:
        conn.execute(text("UPDATE source_files SET original_filename=NULL WHERE id=:f"),
                     {"f": uuid.UUID(ids_a["file_id"])})
