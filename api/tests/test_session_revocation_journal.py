"""M6c-2b2a: real PostgreSQL/MinIO, temporary independent journal, no provider.

Frozen clock stays inside both expiry windows; restored old cookie AND CSRF
must fail because of revocation. Faults are injected, not power-loss tests.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from threading import Barrier, Event, Thread

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import journal, recovery
from app.clock import utcnow
from app.audit import record_event
from app.config import get_settings, PasswordPolicy
from app.identity import login as login_mod, password_reset, users
from app.identity.session_commit import commit_auth
from app.identity.sessions import issue_session, revoke_session, revoke_user_sessions
from app.identity.tokens import hash_token
from app.models import User, UserSession
from app.models.enums import SessionRevokeReason, UserStatus, AuditActorType, AuditOutcome
from tests.conftest import upgrade, alembic_config
from tests.helpers import ADMIN_EMAIL, USER_PASSWORD, Browser
from tests.test_recovery import backup, gate, fsync_fails
from tests.test_uploads import upload_api  # noqa: F401

EMAIL = "session.a@example.org"
NEW_PASSWORD = "new-Password-session-987"


def legacy_session(engine,uid,now,lifetimes):
    """Honest 0029 snapshot data, before generation/routines existed."""
    sid=uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sessions(id,user_id,token_hash,csrf_token_hash,created_at,last_activity_at,absolute_expires_at) VALUES(:s,:u,:t,:c,:at,:at,:end)"),
                     {"s":sid,"u":uid,"t":"a"*64,"c":"b"*64,"at":now,"end":now+lifetimes.session_absolute})
    return SimpleNamespace(session_id=sid)


@pytest.fixture
def world(upload_api):
    h, storage = upload_api
    admin_id = h.bootstrap_admin()
    uid = h.create_user(EMAIL)
    bid = h.create_user("session.b@example.org")
    admin = h.admin_browser()
    a = [h.user_browser(EMAIL) for _ in range(3)]
    b = h.user_browser("session.b@example.org")
    ids = [uuid.UUID(p.get("/auth/session").json()["session"]["id"]) for p in a]
    saved = [dict(p.client.cookies) for p in a]
    return SimpleNamespace(h=h, storage=storage, admin=admin, admin_id=admin_id, uid=uid, bid=bid,
                           a=a, b=b, ids=ids, saved=saved, b_saved=dict(b.client.cookies))


def browser(cookies):
    b = Browser()
    for name, value in cookies.items():
        b.client.cookies.set(name, value)
    return b


def revocations():
    return [r for r in journal.read(get_settings().recovery.journal_path).records if r.kind == journal.SESSION_REVOCATION]


def perform(w, how):
    if how == "logout":
        return w.a[1].post("/auth/logout"), {w.ids[1]}, "logout"
    if how == "one":
        return w.a[0].post(f"/auth/sessions/{w.ids[1]}/revoke"), {w.ids[1]}, "user_revoked"
    if how == "others":
        return w.a[0].post("/auth/sessions/revoke-others"), set(w.ids[1:]), "user_revoked"
    if how == "replace":
        return w.a[1].login(EMAIL, USER_PASSWORD), {w.ids[1]}, "replaced"
    if how == "cross_user_replace":
        return w.a[1].login("session.b@example.org", USER_PASSWORD), {w.ids[1]}, "replaced"
    if how == "reset":
        with Session(w.h.engine) as db:
            issued = password_reset.request_reset(db, email=EMAIL, now=w.h.clock.now,
                                                 lifetime=get_settings().auth.password_reset)
        return w.h.browser().post("/auth/password-reset/confirm", {"token": issued.token,
                    "new_password": NEW_PASSWORD}), set(w.ids), "password_reset"
    return w.admin.post(f"/admin/users/{w.uid}/disable"), set(w.ids), "user_disabled"


@pytest.mark.parametrize("how", ["logout", "one", "others", "replace", "cross_user_replace", "reset", "disable"])
def test_restore_keeps_every_revocation_reason_and_saved_cookie_csrf_blocked(world, admin_engine, app_env, how):
    w = world
    with backup(w.h, admin_engine) as snap:
        response, ended, reason = perform(w, how)
        assert response.status_code in (200, 204), response.text
        assert {uuid.UUID(r.body["session_id"]) for r in revocations()} == ended
        assert {r.body["reason_code"] for r in revocations()} == {reason}
        data = get_settings().recovery.journal_path.read_text(encoding="ascii")
        for secret in [EMAIL, ADMIN_EMAIL, USER_PASSWORD, NEW_PASSWORD] + [v for c in w.saved for v in c.values()]:
            assert secret not in data
        for r in revocations():
            assert set(r.body) == {"session_id", "user_id", "reason_code", "revoked_at"}
        assert gate(snap.engine) == "db_behind_journal"
        recovery.hold(snap.engine, get_settings(), w.h.clock.now)
        app_env(snap.url)
        for cookies in w.saved + [w.b_saved]:
            assert browser(cookies).get("/auth/session").json() == {"error": "service_recovering"}
            assert browser(cookies).post("/auth/sessions/revoke-others").status_code == 503
        w.h.clock.advance(minutes=1)
        with snap.engine.connect() as conn:
            for row in conn.execute(text("SELECT last_activity_at, absolute_expires_at FROM sessions")):
                assert w.h.clock.now < row.last_activity_at + get_settings().auth.session_idle
                assert w.h.clock.now < row.absolute_expires_at
        assert recovery.recover(snap.engine, w.storage, get_settings(), w.h.clock.now).gate == "open"
        for sid, cookies in zip(w.ids, w.saved):
            expected = 401 if sid in ended else 200
            assert browser(cookies).get("/auth/session").status_code == expected
            # Fresh jar per call: first 401 clears the session cookie, and must not explain the second 401.
            post = browser(cookies).post("/auth/sessions/revoke-others") if sid in ended else None
            if post:
                assert (post.status_code, post.json()) == (401, {"error": "session_expired"})
        assert browser(w.b_saved).get("/auth/session").status_code == 200
        assert recovery.recover(snap.engine, w.storage, get_settings(), w.h.clock.now).gate == "open"
        with snap.engine.connect() as conn:
            assert set(conn.scalars(text("SELECT id FROM sessions WHERE revoked_at IS NOT NULL"))) == ended
            # C1 replays the status restriction; it never reconstructs a verifier.
            assert conn.scalar(text("SELECT status FROM users WHERE id=:u"), {"u": w.uid}) == ("disabled" if how=="disable" else "active")


def test_sessions_and_users_created_then_revoked_after_backup_stay_absent(world, admin_engine, app_env):
    w = world
    with backup(w.h, admin_engine) as snap:
        w.h.create_user("after.backup@example.org")
        newer = w.h.user_browser("after.backup@example.org")
        saved = dict(newer.client.cookies)
        sid = newer.get("/auth/session").json()["session"]["id"]
        assert newer.post("/auth/logout").status_code == 204
        app_env(snap.url)
        assert recovery.recover(snap.engine, w.storage, get_settings(), w.h.clock.now).gate == "open"
        assert recovery.recover(snap.engine, w.storage, get_settings(), w.h.clock.now).gate == "open"
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM users WHERE email='after.backup@example.org'")) == 0
            assert conn.scalar(text("SELECT count(*) FROM sessions WHERE id=:s"), {"s": sid}) == 0
            assert conn.scalar(text("SELECT result FROM revocation_journal_records WHERE session_id=:s"),
                               {"s": sid}) == "recorded_session_absent"
        assert browser(saved).get("/auth/session").status_code == 401
        assert browser(w.b_saved).get("/auth/session").status_code == 200


def sample(w, **changes):
    return {"session_id": str(w.ids[0]), "user_id": str(w.uid), "reason_code": "logout",
            "revoked_at": w.h.clock.now.isoformat()} | changes


@pytest.mark.parametrize("field", ["session_id", "user_id", "reason_code", "revoked_at"])
@pytest.mark.parametrize("shape", ["missing", "null"])
def test_reader_and_db_refuse_missing_null_session_fields_at_insert(world, field, shape):
    w = world
    state = journal.read(get_settings().recovery.journal_path)
    body = sample(w)
    if shape == "missing":
        del body[field]
    else:
        body[field] = None
    with pytest.raises(journal.JournalError):
        journal.next_record(state.head, journal.SESSION_REVOCATION, body)
    # Intentionally bypass the app reader to test the DB check independently.
    line = json.dumps({"body": body, "kind": journal.SESSION_REVOCATION})
    with Session(w.h.setup_engine) as db, pytest.raises(IntegrityError):
        db.add(recovery._mirror(SimpleNamespace(seq=state.head.seq+1, kind=journal.SESSION_REVOCATION, hash="f"*64, line=line),
              "revoke", "session_revoked", None, confirmed=False, session_id=w.ids[0], session_user_id=w.uid))
        db.flush()  # before append, not merely at commit
    assert journal.read(get_settings().recovery.journal_path).head == state.head


@pytest.mark.parametrize("bad", ["session", "user", "mirror_null_session", "mirror_null_user", "other_owner"])
def test_wrong_mirror_identity_is_rejected_before_append(world, bad):
    w = world
    state = journal.read(get_settings().recovery.journal_path)
    body = sample(w, **({"session_id": str(uuid.uuid4())} if bad == "session" else
                       {"user_id": str(uuid.uuid4())} if bad in ("user", "other_owner") else {}))
    record = journal.next_record(state.head, journal.SESSION_REVOCATION, body)
    owner = uuid.UUID(body["user_id"]) if bad == "other_owner" else w.uid
    with Session(w.h.setup_engine) as db, pytest.raises(IntegrityError):
        db.add(recovery._mirror(record, "revoke", "session_revoked", None, confirmed=False,
                session_id=None if bad == "mirror_null_session" else w.ids[0],
                session_user_id=None if bad == "mirror_null_user" else owner))
        db.flush()


def test_db_refuses_unjournaled_revoke_reopen_or_changed_session_identity(world):
    w = world
    with pytest.raises(IntegrityError), w.h.setup_engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET revoked_at=:t, revoked_reason='logout' WHERE id=:s"),
                     {"s": w.ids[0], "t": w.h.clock.now})
    assert w.a[0].post("/auth/logout").status_code == 204
    for sql in ("revoked_at=NULL, revoked_reason=NULL", "user_id=:u", "id=:u",
                "revoked_reason='replaced'", "token_hash=repeat('d',64)"):
        with pytest.raises(IntegrityError), w.h.setup_engine.begin() as conn:
            conn.execute(text(f"UPDATE sessions SET {sql} WHERE id=:s"), {"s": w.ids[0], "u": w.bid})
    assert recovery.replay_record(w.h.setup_engine, revocations()[0], w.h.clock.now) == ("present", 0)


def test_replay_rejects_a_session_bound_to_another_user(world):
    w = world
    state = journal.read(get_settings().recovery.journal_path)
    record = journal.next_record(state.head, journal.SESSION_REVOCATION, sample(w, user_id=str(w.bid)))
    journal.append(get_settings().recovery.journal_path, state, [record])
    with pytest.raises(recovery.RecoveryError, match="record_names_another_session_user"):
        recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now)
    assert gate(w.h.engine) == "db_behind_journal"
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 0


def break_write(monkeypatch, shape):
    original = journal._write
    def broken(path, data, mode):
        if mode != "ab":
            return original(path, data, mode)
        if shape == "torn":
            with open(path, "ab") as out:
                out.write(data[:20])
        if shape == "prefix":
            first = data.index(b"\n") + 1
            with open(path, "ab") as out:
                out.write(data[:first+20])
        raise OSError("injected append failure")
    monkeypatch.setattr(journal, "_write", broken)
    return lambda: monkeypatch.setattr(journal, "_write", original)


@pytest.mark.parametrize("shape", ["none", "torn", "prefix", "fsync"])
def test_interrupted_bulk_write_commits_all_revocations_unconfirmed_until_recover(world, monkeypatch, shape):
    w = world
    fix = fsync_fails(monkeypatch) if shape == "fsync" else break_write(monkeypatch, shape)
    response, ended, _ = perform(w, "others")
    assert (response.status_code, response.json()) == (503, {"error": "session_journal_unavailable"})
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 2
    assert w.h.scalar("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed") == 2
    assert gate(w.h.engine) != "open"
    fix()
    if shape != "fsync":
        with pytest.raises(recovery.RecoveryError, match="journal_behind_db"):
            recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now)
    assert recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now,
                            confirm_db_current=shape != "fsync").gate == "open"
    assert recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now).gate == "open"
    assert w.a[0].get("/auth/session").status_code == w.b.get("/auth/session").status_code == 200
    for cookies in w.saved[1:]:
        assert browser(cookies).get("/auth/session").status_code == 401


def failing_commit(monkeypatch, lands):
    write, commit = recovery.record_auth_endings, Session.commit
    armed = [False]
    def record(*args, **kwargs):
        result = write(*args, **kwargs)
        armed[0] = bool(result)
        return result
    def fail(db):
        if armed[0]:
            armed[0] = False
            if lands:
                commit(db)
            raise RuntimeError("commit reply lost")
        return commit(db)
    monkeypatch.setattr(recovery, "record_auth_endings", record)
    monkeypatch.setattr(Session, "commit", fail)
    return lambda: (monkeypatch.setattr(recovery, "record_auth_endings", write),
                    monkeypatch.setattr(Session, "commit", commit))


@pytest.mark.parametrize("lands", [False, True])
@pytest.mark.parametrize("how", ["others", "reset", "disable", "replace"])
def test_commit_failure_or_unknown_outcome_replays_sessions_but_never_claims_auth_success(world, monkeypatch, lands, how):
    w = world
    fix=failing_commit(monkeypatch, lands)
    response, ended, _ = perform(w, how)
    assert response.status_code == 503
    auth_change=how in ("reset","disable")
    assert gate(w.h.engine) == ("db_behind_journal" if auth_change and not lands else "open")
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == (0 if auth_change and not lands else len(ended))
    if how == "disable":
        assert w.h.scalar("SELECT status FROM users WHERE id=:u", u=w.uid) == ("disabled" if lands else "active")
    if how == "reset":
        assert w.h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NOT NULL") == int(lands)
    if how == "replace":
        assert not any(h.startswith("ras_session=") for h in response.headers.get_list("set-cookie"))
    fix()
    recovery.recover(w.h.setup_engine,w.storage,get_settings(),w.h.clock.now)
    if how=="reset":
        assert w.h.browser().login(EMAIL,NEW_PASSWORD if lands else USER_PASSWORD).status_code==(200 if lands else 401)
    if how=="disable":
        assert w.h.scalar("SELECT status FROM users WHERE id=:u",u=w.uid)=="disabled"


def test_failed_commit_and_failed_replay_keep_gate_closed(world, monkeypatch):
    w = world
    failing_commit(monkeypatch, False)
    real = recovery.replay_record
    monkeypatch.setattr(recovery, "replay_record", lambda *a: (_ for _ in ()).throw(RuntimeError("replay down")))
    response, _, _ = perform(w, "others")
    assert response.json() == {"error": "session_revocation_pending_recovery"}
    assert gate(w.h.engine) == "db_behind_journal"
    monkeypatch.setattr(recovery, "replay_record", real)
    assert recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now).gate == "open"


def test_partial_bulk_journal_and_rollback_replays_prefix_then_retry_finishes_rest(world, monkeypatch):
    w = world
    fix = break_write(monkeypatch, "prefix")
    fix_commit = failing_commit(monkeypatch, False)
    response, _, _ = perform(w, "others")
    assert response.status_code == 503
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 0
    fix()
    assert recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now).gate == "open"
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 1
    # The injected commit error was consumed; retry journals only the remaining session.
    fix_commit()
    assert w.a[0].post("/auth/sessions/revoke-others").json() == {"revoked": 1}
    assert {uuid.UUID(r.body["session_id"]) for r in revocations()} == set(w.ids[1:])


def test_legacy_revoked_sessions_backfill_and_hold_on_unmigrated_snapshot(empty_db_url, journal_path, admin_engine, app_env):
    upgrade(empty_db_url, "0029")
    app_env(empty_db_url)
    eng = create_engine(empty_db_url)
    now = utcnow()
    genesis = journal.genesis(uuid.uuid4(), now, "new_install")
    journal.create(journal_path, [genesis])
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO recovery_registrations VALUES(1,:j,:h,'new_install',:t)"),
                     {"j": genesis.body["journal_id"], "h": genesis.hash, "t": now})
        conn.execute(text("INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,"
                          "journal_confirmed) VALUES(1,'genesis',:h,:l,'genesis','new_install',true)"),
                     {"h": genesis.hash, "l": genesis.line})
        uid = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('legacy@example.org',"
                               "'$argon2id$legacy-fixture') RETURNING id"))
    issued=legacy_session(eng,uid,now,get_settings().auth)
    # Backup while still live under 0029, then revoke under the old code.
    h = SimpleNamespace(engine=eng)
    with backup(h, admin_engine) as snap:
        with eng.begin() as conn:
            conn.execute(text("UPDATE sessions SET revoked_at=:t, revoked_reason='logout' WHERE id=:s"),
                         {"t": now, "s": issued.session_id})
        upgrade(empty_db_url)
        assert recovery.evaluate(get_settings(), eng).reason == "sessions_not_journaled"
        assert recovery.status(eng, get_settings())["unjournaled_sessions"] == 1
        assert recovery.recover(eng, None, get_settings(), now).sessions_imported == 1
        recovery.hold(snap.engine, get_settings(), now)  # seq/hash only, before 0030
        upgrade(snap.url)
        assert recovery.recover(snap.engine, None, get_settings(), now).reconciliation_complete
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT revoked_reason FROM sessions WHERE id=:s"),
                               {"s": issued.session_id}) == "logout"
    eng.dispose()


def test_db_checks_exact_reason_and_time_and_prevents_reinsert_of_journaled_live_session(world):
    w = world
    head = journal.read(get_settings().recovery.journal_path).head
    for changed in ({"reason_code": "replaced"}, {"revoked_at": "2000-01-01T00:00:00+00:00"}):
        record = journal.next_record(head, journal.SESSION_REVOCATION, sample(w, **changed))
        with Session(w.h.setup_engine) as db, pytest.raises(IntegrityError):
            db.add(recovery._mirror(record, "revoke", "session_revoked", None, confirmed=False,
                                   session_id=w.ids[0], session_user_id=w.uid))
            db.execute(text("UPDATE sessions SET revoked_at=:t,revoked_reason='logout' WHERE id=:s"),
                       {"t": w.h.clock.now, "s": w.ids[0]})
            db.commit()
    assert w.a[0].post("/auth/logout").status_code == 204
    with pytest.raises(IntegrityError), w.h.setup_engine.begin() as conn:
        # Mimic a deleted/pruned session reappearing with the old cookie hash.
        conn.execute(text("DELETE FROM sessions WHERE id=:s"), {"s": w.ids[0]})
        conn.execute(text("INSERT INTO sessions(id,user_id,token_hash,csrf_token_hash,created_at,last_activity_at,"
                          "absolute_expires_at) VALUES(:s,:u,:h,:c,:t,:t,:e)"),
                     {"s": w.ids[0], "u": w.uid, "h": hash_token(w.saved[0]["ras_session"]),
                      "c": hash_token(w.saved[0]["ras_csrf"]), "t": w.h.clock.now,
                      "e": w.h.clock.now + get_settings().auth.session_absolute})


@pytest.mark.parametrize("how", ["reset", "disable"])
def test_fsync_failure_preserves_whole_auth_transaction_but_reports_error(world, monkeypatch, how):
    w = world
    fix = fsync_fails(monkeypatch)
    response, _, _ = perform(w, how)
    assert response.json() == {"error": "auth_journal_unavailable"}
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 3
    if how == "reset":
        assert w.h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NOT NULL") == 1
    else:
        assert w.h.scalar("SELECT status FROM users WHERE id=:u", u=w.uid) == "disabled"
    assert gate(w.h.engine) == "journal_unconfirmed"
    fix()
    assert recovery.recover(w.h.setup_engine, w.storage, get_settings(), w.h.clock.now).gate == "open"


def test_neither_journal_nor_commit_takes_batch_no_success_and_retry_is_safe(world, monkeypatch):
    w = world
    fix_write = break_write(monkeypatch, "none")
    fix_commit = failing_commit(monkeypatch, False)
    response, _, _ = perform(w, "others")
    assert response.status_code == 503
    assert gate(w.h.engine) == "open"
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 0
    fix_write()
    fix_commit()
    assert w.a[0].post("/auth/sessions/revoke-others").json() == {"revoked": 2}


def test_register_existing_imports_pre_migration_revocations(empty_db_url, journal_path, app_env):
    upgrade(empty_db_url, "0029")
    app_env(empty_db_url)
    eng = create_engine(empty_db_url)
    now = utcnow()
    with eng.begin() as conn:
        uid = conn.scalar(text("INSERT INTO users(email,password_verifier)"
                               " VALUES('legacy@example.org','$argon2id$fixture') RETURNING id"))
    sid=legacy_session(eng,uid,now,get_settings().auth).session_id
    with eng.begin() as conn:
        conn.execute(text("UPDATE sessions SET revoked_at=:t,revoked_reason='logout' WHERE id=:s"), {"t": now, "s": sid})
    upgrade(empty_db_url)
    with pytest.raises(recovery.RecoveryError, match="database_not_new"):
        recovery.init_new(eng, get_settings(), now)  # auth-only DB is not an empty install
    _, count = recovery.register_existing(eng, get_settings(), now)
    assert count == 1 and recovery.evaluate_integrity(get_settings(), eng).open
    assert recovery.evaluate(get_settings(), eng, administrative=True).reason=="auth_safety_pending"
    assert recovery.status(eng, get_settings())["unjournaled_sessions"] == 0
    with pytest.raises(Exception, match="auth safety history prevents downgrade"):
        command.downgrade(alembic_config(empty_db_url), "0029")
    eng.dispose()


@pytest.mark.parametrize("action", ["reset", "disable", "revoke"])
def test_login_committing_first_is_caught_by_waiting_reset_disable_or_bulk_revoke(world, monkeypatch, action):
    w = world
    issued, release, waiting = Event(), Event(), Event()
    results = {}
    with Session(w.h.engine) as db:
        reset = password_reset.request_reset(db, email=EMAIL, now=w.h.clock.now,
                                              lifetime=get_settings().auth.password_reset,clock=lambda:w.h.clock.now)
    admin_sid=uuid.UUID(w.admin.get("/auth/session").json()["session"]["id"])
    admin_csrf=w.admin.client.cookies.get("ras_csrf")
    real_commit = login_mod.commit_auth
    def pause_login(db):
        issued.set()
        assert release.wait(20)
        real_commit(db)
    monkeypatch.setattr(login_mod, "commit_auth", pause_login)
    import threading
    def observe(conn, cursor, sql, params, context, many):
        if threading.current_thread().name == "auth-decider" and "ras_auth_lock_users" in sql:
            waiting.set()
    event.listen(w.h.engine, "before_cursor_execute", observe)
    def login_now():
        try:
            with Session(w.h.engine) as db:
                _, result = login_mod.login(db, email=EMAIL, password=USER_PASSWORD, now=w.h.clock.now,
                                             settings=get_settings(),clock=lambda:w.h.clock.now)
                results["session"] = result.session_id
        except Exception as exc:
            results["login_error"] = type(exc).__name__
    def decide():
        try:
            with Session(w.h.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '10s'"))
                if action == "reset":
                    password_reset.confirm_reset(db, raw_token=reset.token, new_password=NEW_PASSWORD,
                                                 now=w.h.clock.now, policy=PasswordPolicy(),clock=lambda:w.h.clock.now)
                elif action == "disable":
                    users.set_user_status(db, user_id=w.uid, actor_id=w.admin_id,
                                          status=UserStatus.DISABLED, now=w.h.clock.now,
                                          actor_session_id=admin_sid,csrf_token=admin_csrf,clock=lambda:w.h.clock.now)
                else:
                    revoke_user_sessions(db, user_id=w.uid, reason=SessionRevokeReason.USER_REVOKED,
                                         now=w.h.clock.now, keep_session_id=w.ids[0])
                    commit_auth(db)
            results["decision"] = "committed"
        except Exception as exc:
            results["decision"] = type(exc).__name__
    first, second = Thread(target=login_now), Thread(target=decide, name="auth-decider")
    try:
        first.start()
        assert issued.wait(20)
        second.start()
        assert waiting.wait(20)  # observed at the DB lock attempt, not an arbitrary sleep
        assert second.is_alive()
        release.set()
        first.join(25)
        second.join(25)
        assert not first.is_alive() and not second.is_alive(), "deadlock"
        assert results.get("decision") == "committed" and "login_error" not in results, results
        assert w.h.scalar("SELECT revoked_at IS NOT NULL FROM sessions WHERE id=:s", s=results["session"])
        assert w.h.scalar("SELECT count(*) FROM sessions WHERE user_id=:u AND revoked_at IS NULL", u=w.uid) \
            == (1 if action == "revoke" else 0)
        assert gate(w.h.engine) == "open"
        assert w.b.get("/auth/session").status_code == 200
    finally:
        release.set()
        event.remove(w.h.engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("action", ["reset", "disable"])
def test_reset_disable_commits_while_login_hashes_without_locks_then_login_refuses(world, monkeypatch, action):
    w = world
    hashing, done = Event(), Event()
    real = login_mod.verify_password
    result = []
    def paused(*args):
        ok = real(*args)
        hashing.set()
        assert done.wait(20)
        return ok
    monkeypatch.setattr(login_mod, "verify_password", paused)
    def attempt():
        try:
            with Session(w.h.engine) as db:
                login_mod.login(db, email=EMAIL, password=USER_PASSWORD, now=w.h.clock.now, settings=get_settings())
            result.append("unexpected_success")
        except Exception as exc:
            result.append(type(exc).__name__)
    t = Thread(target=attempt)
    t.start()
    try:
        assert hashing.wait(20)
        response, _, _ = perform(w, action)
        assert response.status_code == 200
        done.set()
        t.join(25)
        assert not t.is_alive() and result == ["InvalidCredentials"]
        assert w.h.scalar("SELECT count(*) FROM sessions WHERE user_id=:u AND revoked_at IS NULL", u=w.uid) == 0
    finally:
        done.set()


def test_opposite_account_replacements_lock_users_in_same_order_without_deadlock(world, monkeypatch):
    w = world
    both = Barrier(2)
    real = login_mod.verify_password
    def verify(*args):
        ok = real(*args)
        both.wait(timeout=20)
        return ok
    monkeypatch.setattr(login_mod, "verify_password", verify)
    results = []
    def switch(email, previous):
        try:
            with Session(w.h.engine) as db:
                db.execute(text("SET LOCAL lock_timeout='10s'"))
                _, issued = login_mod.login(db, email=email, password=USER_PASSWORD, now=w.h.clock.now,
                                             settings=get_settings(), previous_token=previous,clock=lambda:w.h.clock.now)
                results.append(issued.session_id)
        except Exception as exc:
            results.append(type(exc).__name__)
    threads = [Thread(target=switch, args=("session.b@example.org", w.saved[0]["ras_session"])),
               Thread(target=switch, args=(EMAIL, w.b_saved["ras_session"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(25)
        assert not t.is_alive(), "deadlock"
    assert len(results) == 2 and all(isinstance(r, uuid.UUID) for r in results), results
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_reason='replaced'") == 2
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE id=ANY(:ids) AND revoked_at IS NULL", ids=results) == 2
    assert gate(w.h.engine) == "open"


def test_auth_user_lock_allows_audit_fk_under_other_journal_writer_without_lock_cycle(world):
    w = world
    auth_locked, journal_locked = Event(), Event()
    results = []
    def auth_writer():
        try:
            with Session(w.h.engine) as db:
                db.execute(text("SET LOCAL statement_timeout='10s'"))
                revoke_session(db, session_id=w.ids[1], user_id=w.uid, reason=SessionRevokeReason.USER_REVOKED,
                               now=w.h.clock.now)
                auth_locked.set()
                assert journal_locked.wait(20)
                commit_auth(db)
            results.append("auth_committed")
        except Exception as exc:
            results.append(type(exc).__name__)
    def journal_writer():
        try:
            assert auth_locked.wait(20)
            with Session(w.h.engine) as db:
                db.execute(text("SET LOCAL statement_timeout='10s'"))
                recovery._lock_journal(db)
                journal_locked.set()
                # Matches pending actor audit flushed by another journal transaction.
                record_event(db, action="test.journal_writer", outcome=AuditOutcome.SUCCESS,
                             actor_type=AuditActorType.USER, actor_user_id=w.uid)
                db.flush()  # FK takes KEY SHARE, compatible with auth's NO KEY UPDATE.
                db.commit()
            results.append("journal_committed")
        except Exception as exc:
            results.append(type(exc).__name__)
    threads = [Thread(target=auth_writer), Thread(target=journal_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(25)
        assert not t.is_alive(), "deadlock"
    assert sorted(results) == ["auth_committed", "journal_committed"], results
    assert w.h.scalar("SELECT revoked_reason FROM sessions WHERE id=:s", s=w.ids[1]) == "user_revoked"
