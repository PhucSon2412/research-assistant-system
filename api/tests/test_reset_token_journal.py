"""Reset endings across restore: real PostgreSQL/MinIO, temporary journal, no provider."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import journal, recovery
from app.clock import utcnow
from app.config import get_settings
from app.identity import password_reset, users
from app.identity.errors import InvalidToken
from app.identity.passwords import verify_password
from app.identity.reset_tokens import end_token, lock_open_tokens
from app.identity.session_commit import commit_auth
from app.identity.tokens import generate_token
from app.models.enums import UserStatus
from tests.conftest import alembic_config, upgrade
from tests.helpers import USER_PASSWORD
from tests.test_recovery import backup, fsync_fails, gate
from tests.test_session_revocation_journal import break_write, failing_commit, browser
from tests.test_uploads import upload_api  # noqa: F401

EMAIL = "reset.journal@example.org"
OTHER = "reset.other@example.org"
PASSWORD = "reset-journal-New-9876"
KIND = journal.RESET_TOKEN_CLOSURE


@pytest.fixture
def world(upload_api):
    h, storage = upload_api
    admin_id = h.bootstrap_admin()
    uid, bid = h.create_user(EMAIL), h.create_user(OTHER)
    public = h.browser()
    public.csrf()
    return SimpleNamespace(h=h, storage=storage, uid=uid, bid=bid, admin_id=admin_id,
                           public_cookies=dict(public.client.cookies))


def issue(w, email=EMAIL):
    with Session(w.h.engine) as db:
        return password_reset.request_reset(db, email=email, now=w.h.clock.now,
                                             lifetime=get_settings().auth.password_reset,clock=lambda:w.h.clock.now)


def confirm(w, token):
    # Preserve the pre-recovery double-submit cookie while /auth/csrf is gated.
    return browser(w.public_cookies).post("/auth/password-reset/confirm", {"token": token, "new_password": PASSWORD})


def token_id(w, uid=None):
    return w.h.scalar("SELECT id FROM password_reset_tokens WHERE user_id=:u AND used_at IS NULL AND revoked_at IS NULL",
                      u=uid or w.uid)


def records():
    return [r for r in journal.read(get_settings().recovery.journal_path).records if r.kind == KIND]


def recover(w, engine=None, **kwargs):
    return recovery.recover(engine or w.h.setup_engine, w.storage, get_settings(), w.h.clock.now, **kwargs)


@pytest.mark.slow
@pytest.mark.parametrize("sessions", [0, 3])
@pytest.mark.parametrize("how", ["consumed", "replaced", "completed", "disabled"])
def test_restore_endings_inside_ttl_twice_and_other_token_stays_valid(world, admin_engine, app_env, how, sessions):
    w = world
    saved = [dict(w.h.user_browser(EMAIL).client.cookies) for _ in range(sessions)]
    old, other = issue(w), issue(w, OTHER)
    tid = token_id(w)
    verifier = w.h.scalar("SELECT password_verifier FROM users WHERE id=:u", u=w.uid)
    with backup(w.h, admin_engine) as first, backup(w.h, admin_engine) as second:
        if how == "consumed":
            assert confirm(w, old.token).status_code == 200
        elif how == "replaced":
            assert issue(w).token != old.token
        elif how == "disabled":
            assert w.h.admin_browser().post(f"/admin/users/{w.uid}/disable").status_code==200
        else:
            # Partial unique permits only one open token in today's schema. Exercise the helper
            # used for the "other tokens" branch without weakening that constraint.
            with Session(w.h.engine) as db:
                db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [w.uid]})
                end_token(db, lock_open_tokens(db, w.uid)[0], kind="revoked", reason="reset_completed", now=w.h.clock.now)
                commit_auth(db)
        body = records()[0].body
        assert body["token_id"] == str(tid)
        assert body["reason_code"] == {"consumed": "password_reset", "replaced": "reset_replaced",
                                      "completed": "reset_completed", "disabled": "user_disabled"}[how]
        raw = get_settings().recovery.journal_path.read_text(encoding="ascii")
        assert all(v not in raw for v in [old.token, other.token, EMAIL, OTHER, PASSWORD, verifier])
        for snap in (first, second):
            recovery.hold(snap.engine, get_settings(), w.h.clock.now)
            app_env(snap.url)
            assert confirm(w, old.token).status_code == 503
            assert recover(w, snap.engine).gate == "open"
            assert recover(w, snap.engine).gate == "open"
            with snap.engine.connect() as conn:
                assert w.h.clock.now < conn.scalar(text("SELECT expires_at FROM password_reset_tokens WHERE id=:t"), {"t": tid})
                assert conn.scalar(text("SELECT status FROM users WHERE id=:u"), {"u": w.uid}) == ("disabled" if how=="disabled" else "active")
                assert conn.scalar(text("SELECT password_verifier FROM users WHERE id=:u"), {"u": w.uid}) == verifier
            assert (confirm(w, old.token).status_code, confirm(w, old.token).json()) == (400, {"error": "invalid_token"})
            for cookies in saved:
                if how in ("consumed", "disabled"):
                    assert browser(cookies).get("/auth/session").status_code == 401
            # A weak password exercises validity without ending the unrelated token/journal.
            assert w.h.browser().post("/auth/password-reset/confirm", {"token": other.token, "new_password": "short"}).json()["error"] == "weak_password"
        assert confirm(w, other.token).status_code == 200  # Unrelated token completes a real reset after restore.


@pytest.mark.slow
def test_post_snapshot_token_and_user_remain_absent(world, admin_engine, app_env):
    w = world
    with backup(w.h, admin_engine) as snap:
        new_uid = w.h.create_user("later.reset@example.org")
        issued = issue(w, "later.reset@example.org")
        tid = token_id(w, new_uid)
        assert confirm(w, issued.token).status_code == 200
        app_env(snap.url)
        assert recover(w, snap.engine).replayed["recorded_reset_token_absent"] == 1
        assert recover(w, snap.engine).gate == "open"
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM users WHERE id=:u"), {"u": new_uid}) == 0
            assert conn.scalar(text("SELECT count(*) FROM password_reset_tokens WHERE id=:t"), {"t": tid}) == 0
        assert confirm(w, issued.token).json() == {"error": "invalid_token"}


def sample(w, **changes):
    return dict(token_id=str(token_id(w)), user_id=str(w.uid), end_kind="consumed", reason_code="password_reset",
                ended_at=w.h.clock.now.isoformat()) | changes


def sql_mirror(db, body, **changes):
    # Bypass ALL Python parsing; raw SQL verifies the independent database contract.
    values = dict(t=body.get("token_id"), u=body.get("user_id"), k=body.get("end_kind"),
                  r=body.get("reason_code"), at=body.get("ended_at"),
                  line=json.dumps(dict(kind=KIND, body=body))) | changes
    values["seq"]=db.scalar(text("SELECT coalesce(max(seq),0)+1 FROM revocation_journal_records"))
    db.execute(text("INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,"
                    "reset_token_id,reset_token_user_id,reset_end_kind,reset_reason_code,reset_ended_at)"
                    " VALUES(:seq,'reset_token_closure',repeat('f',64),:line,'close','reset_token_ended',false,:t,:u,:k,:r,:at)"), values)


@pytest.mark.parametrize("field", ["token_id", "user_id", "end_kind", "reason_code", "ended_at"])
@pytest.mark.parametrize("shape", ["missing", "null"])
def test_parser_and_sql_missing_json_null(world, field, shape):
    w = world
    issue(w)
    good = sample(w)
    before=get_settings().recovery.journal_path.read_bytes()
    bad = dict(good)
    if shape == "missing":
        del bad[field]
    else:
        bad[field] = None
    with pytest.raises(journal.JournalError):
        journal.next_record(journal.read(get_settings().recovery.journal_path).head, KIND, bad)
    # Keep mirror columns valid so it is the BODY rejection being measured.
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, bad, t=good["token_id"], u=good["user_id"], k=good["end_kind"],
                   r=good["reason_code"], at=good["ended_at"])
    assert get_settings().recovery.journal_path.read_bytes()==before


@pytest.mark.parametrize("changes", [dict(token_id="bad"), dict(user_id="bad"), dict(end_kind="used"),
    dict(reason_code="user_disabled"), dict(reason_code="secret"), dict(raw_token="secret"), dict(token_hash="a"*64),
    dict(email=EMAIL), dict(ended_at="2026-10-03T12:00:00"), dict(ended_at="2026-02-30T00:00:00Z"),
    dict(ended_at="infinity"), dict(ended_at=None), dict(ended_at=123)])
def test_bad_fields_parser_and_direct_sql(world, changes):
    w = world
    issue(w)
    body = sample(w, **changes)
    with pytest.raises(journal.JournalError):
        journal.next_record(journal.read(get_settings().recovery.journal_path).head, KIND, body)
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, body)


@pytest.mark.parametrize("field", ["t", "u", "k", "r", "at"])
@pytest.mark.parametrize("shape", ["null", "different"])
def test_mirror_columns_must_match_at_insert(world, field, shape):
    w = world
    issue(w)
    value = None if shape == "null" else {"t": str(uuid.uuid4()), "u": str(w.bid), "k": "revoked",
            "r": "legacy_consumed", "at": (w.h.clock.now - timedelta(seconds=1)).isoformat()}[field]
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, sample(w), **{field: value})


def test_wrong_owner_and_db_finality(world):
    w = world
    issued = issue(w)
    tid = token_id(w)
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, sample(w, user_id=str(w.bid)))
    for sql in ("used_at=:t,closure_reason='password_reset'", "revoked_at=:t,closure_reason='user_disabled'"):
        with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
            conn.execute(text(f"UPDATE password_reset_tokens SET {sql} WHERE id=:i"), {"i": tid, "t": w.h.clock.now})
    assert confirm(w, issued.token).status_code == 200
    for sql in ("used_at=NULL,closure_reason=NULL", "used_at=used_at+interval '1 second'",
                "used_at=NULL,revoked_at=:t,closure_reason='user_disabled'", "user_id=:u", "id=:u",
                "token_hash=repeat('b',64)", "expires_at=expires_at+interval '1 hour'", "closure_reason='legacy_consumed'"):
        with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
            conn.execute(text(f"UPDATE password_reset_tokens SET {sql} WHERE id=:i"), {"i": tid, "t": w.h.clock.now, "u": w.bid})
    with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
        conn.execute(text("DELETE FROM password_reset_tokens WHERE id=:i"), {"i": tid})
        conn.execute(text("INSERT INTO password_reset_tokens(id,user_id,token_hash,created_at,expires_at)"
                          " VALUES(:i,:u,repeat('b',64),:t,:e)"),
                     {"i": tid, "u": w.uid, "t": w.h.clock.now, "e": w.h.clock.now + get_settings().auth.password_reset})


@pytest.mark.parametrize("change", ["kind", "time", "reason"])
def test_sql_deferred_trigger_matches_actual_token_ending(world, change):
    w = world
    issue(w)
    tid = token_id(w)
    body = sample(w)
    if change == "kind":
        body.update(end_kind="revoked", reason_code="reset_completed")
    elif change == "time":
        body["ended_at"] = (w.h.clock.now + timedelta(seconds=1)).isoformat()
    else:
        body["reason_code"] = "legacy_consumed"
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, body)
        db.execute(text("UPDATE password_reset_tokens SET used_at=:t,closure_reason='password_reset' WHERE id=:i"),
                   {"i": tid, "t": w.h.clock.now})
        db.commit()


@pytest.mark.slow
def test_absent_token_of_existing_user_is_not_recreated(world, admin_engine, app_env):
    w = world
    with backup(w.h, admin_engine) as snap:
        issued = issue(w)
        tid = token_id(w)
        assert confirm(w, issued.token).status_code == 200
        app_env(snap.url)
        assert recover(w, snap.engine).replayed["recorded_reset_token_absent"] == 1
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM password_reset_tokens WHERE id=:i"), {"i": tid}) == 0
            assert conn.scalar(text("SELECT count(*) FROM users WHERE id=:u"), {"u": w.uid}) == 1


def test_failed_replay_keeps_gate_closed_and_token_error_has_no_session_claim(world, monkeypatch):
    w = world
    issued = issue(w)
    fix_commit = failing_commit(monkeypatch, False)
    original = recovery.replay_record
    monkeypatch.setattr(recovery, "replay_record", lambda *a: (_ for _ in ()).throw(RuntimeError("replay down")))
    r = confirm(w, issued.token)
    assert (r.status_code, r.json()) == (503, {"error": "auth_outcome_uncertain"})
    assert gate(w.h.engine) == "db_behind_journal"
    monkeypatch.setattr(recovery, "replay_record", original)
    fix_commit()
    assert recover(w).gate == "open"
    assert confirm(w, issued.token).status_code == 400


@pytest.mark.slow
@pytest.mark.parametrize("change", ["owner", "kind", "time", "reason"])
@pytest.mark.parametrize("mirrored", [False, True])
def test_recover_checks_conflicts_even_with_existing_mirror(world, admin_engine, app_env, change, mirrored):
    w = world
    issued = issue(w)
    tid = token_id(w)
    with backup(w.h, admin_engine) as snap:
        assert confirm(w, issued.token).status_code == 200
        if mirrored:
            assert recover(w, snap.engine).gate == "open"
        # Simulate a corrupt restored snapshot via superuser; these writes must be impossible normally.
        with snap.engine.begin() as conn:
            conn.execute(text("ALTER TABLE password_reset_tokens DISABLE TRIGGER USER"))
            sql = {"owner": "user_id=:u", "kind": "used_at=NULL,revoked_at=:t,closure_reason='user_disabled'",
                   "time": "used_at=:t,closure_reason='password_reset'", "reason": "used_at=:n,closure_reason='legacy_consumed'"}[change]
            conn.execute(text(f"UPDATE password_reset_tokens SET {sql} WHERE id=:i"),
                         {"i": tid, "u": w.bid, "t": w.h.clock.now + timedelta(seconds=1), "n": w.h.clock.now})
            conn.execute(text("ALTER TABLE password_reset_tokens ENABLE TRIGGER USER"))
        app_env(snap.url)
        with pytest.raises(recovery.RecoveryError, match="another_reset_token_user|closure_conflict"):
            recover(w, snap.engine)
        assert gate(snap.engine) != "open"


@pytest.mark.parametrize("sessions", [0, 3])
@pytest.mark.parametrize("shape", ["none", "torn", "prefix", "fsync"])
def test_append_faults_atomic_token_and_session_batch(world, monkeypatch, shape, sessions):
    w = world
    for _ in range(sessions):
        w.h.user_browser(EMAIL)
    issued = issue(w)
    fix = fsync_fails(monkeypatch) if shape == "fsync" else break_write(monkeypatch, shape)
    response = confirm(w, issued.token)
    assert (response.status_code, response.json()) == (503, {"error": "auth_journal_unavailable"})
    assert w.h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NOT NULL") == 1
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == sessions
    assert w.h.scalar("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed") == sessions + 2  # credential + token + sessions
    assert verify_password(w.h.scalar("SELECT password_verifier FROM users WHERE id=:u", u=w.uid), PASSWORD)
    assert gate(w.h.engine) != "open"
    fix()
    assert recover(w, confirm_db_current=shape != "fsync").gate == "open"
    assert confirm(w, issued.token).json() == {"error": "invalid_token"}


@pytest.mark.parametrize("sessions", [0, 3])
@pytest.mark.parametrize("lands", [False, True])
@pytest.mark.parametrize("how", ["confirm", "disable", "request"])
def test_commit_faults_never_claim_success_or_send_link(world, monkeypatch, lands, sessions, how):
    w = world
    for _ in range(sessions):
        w.h.user_browser(EMAIL)
    issued = issue(w)
    tid = token_id(w)
    before = len(w.h.outbox().entries(now=w.h.clock.now))
    fix = failing_commit(monkeypatch, lands)
    if how == "confirm":
        response = confirm(w, issued.token)
    elif how == "disable":
        response = w.h.admin_browser().post(f"/admin/users/{w.uid}/disable")
    else:
        response = w.h.browser().post("/auth/password-reset/request", {"email": EMAIL})
    assert response.status_code == (202 if how == "request" else 503)
    assert len(w.h.outbox().entries(now=w.h.clock.now)) == before
    assert gate(w.h.engine) == ("db_behind_journal" if how in ("confirm","disable") and not lands else "open")
    assert bool(w.h.scalar("SELECT used_at IS NOT NULL OR revoked_at IS NOT NULL FROM password_reset_tokens WHERE id=:i", i=tid)) == (lands or how=="request")
    if how == "confirm":
        assert verify_password(w.h.scalar("SELECT password_verifier FROM users WHERE id=:u", u=w.uid), PASSWORD if lands else USER_PASSWORD)
    elif how == "disable":
        assert w.h.scalar("SELECT status FROM users WHERE id=:u", u=w.uid) == ("disabled" if lands else "active")
    else:
        assert w.h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NULL AND revoked_at IS NULL") == int(lands)
    fix()
    assert recover(w).gate == "open"


@pytest.mark.parametrize("shape", ["none", "torn", "prefix"])
@pytest.mark.parametrize("sessions", [0, 3])
def test_rollback_after_incomplete_append_only_complete_lines_replay(world, monkeypatch, shape, sessions):
    w = world
    for _ in range(sessions):
        w.h.user_browser(EMAIL)
    issued = issue(w)
    fix_write, fix_commit = break_write(monkeypatch, shape), failing_commit(monkeypatch, False)
    assert confirm(w, issued.token).status_code == 503
    assert w.h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NOT NULL") == 0
    complete = len(records())
    fix_write()
    fix_commit()
    assert recover(w).gate == "open"
    assert w.h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NOT NULL") == complete
    assert w.h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 0
    assert verify_password(w.h.scalar("SELECT password_verifier FROM users WHERE id=:u", u=w.uid), USER_PASSWORD)
    # A complete credential-only prefix quarantines the old credential and
    # fences its normal token even though no closure line survived.
    assert confirm(w, issued.token).status_code == (400 if shape=="prefix" else 200)


def test_public_request_generic_on_internal_failure_then_gate_rejects_new_requests(world, monkeypatch, caplog):
    w = world
    issue(w)
    unknown = w.h.browser().post("/auth/password-reset/request", {"email": "unknown@example.org"})
    fix = break_write(monkeypatch, "none")
    known = w.h.browser().post("/auth/password-reset/request", {"email": EMAIL})
    assert (known.status_code, known.text) == (unknown.status_code, unknown.text) == (202, '{"status":"accepted"}')
    assert not w.h.outbox().entries(now=w.h.clock.now)
    for email in (EMAIL, "unknown@example.org"):
        assert browser(w.public_cookies).post("/auth/password-reset/request", {"email": email}).status_code == 503
    assert EMAIL not in caplog.text
    fix()
    assert recover(w, confirm_db_current=True).gate == "open"


@pytest.mark.parametrize("lands", [False, True])
def test_first_request_commit_uncertain_has_no_batch_and_no_delivery(world, monkeypatch, lands):
    w = world
    original = Session.commit
    armed = [True]
    def lost_reply(db):
        if armed[0]:
            armed[0] = False
            if lands:
                original(db)
            raise RuntimeError("reply lost")
        original(db)
    monkeypatch.setattr(Session, "commit", lost_reply)
    response = w.h.browser().post("/auth/password-reset/request", {"email": EMAIL})
    assert response.status_code == 202
    assert not w.h.outbox().entries(now=w.h.clock.now)
    assert w.h.scalar("SELECT count(*) FROM password_reset_tokens") == int(lands)


@pytest.mark.parametrize("action", ["request", "disable"])
@pytest.mark.parametrize("first", ["confirm", "other"])
def test_confirm_races_request_disable_with_observed_user_lock(world, monkeypatch, action, first):
    w = world
    issued = issue(w)
    admin=w.h.admin_browser()
    admin_sid=uuid.UUID(admin.get("/auth/session").json()["session"]["id"])
    admin_csrf=admin.client.cookies.get("ras_csrf")
    held, waiting, release = Event(), Event(), Event()
    results = {}
    real = commit_auth
    def paused(db):
        if current_thread().name == "first":
            held.set()
            assert release.wait(20)
        return real(db)
    monkeypatch.setattr(password_reset, "commit_auth", paused)
    monkeypatch.setattr(users, "commit_auth", paused)
    def observe(conn, cursor, sql, params, context, many):
        if current_thread().name == "second" and "ras_auth_lock_users" in sql:
            waiting.set()
    event.listen(w.h.engine, "before_cursor_execute", observe)
    def run(which):
        try:
            with Session(w.h.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '10s'"))
                if which == "confirm":
                    password_reset.confirm_reset(db, raw_token=issued.token, new_password=PASSWORD,
                                                 now=w.h.clock.now, policy=get_settings().password,clock=lambda:w.h.clock.now)
                elif action == "request":
                    password_reset.request_reset(db, email=EMAIL, now=w.h.clock.now, lifetime=get_settings().auth.password_reset,clock=lambda:w.h.clock.now)
                else:
                    users.set_user_status(db, user_id=w.uid, actor_id=w.admin_id, status=UserStatus.DISABLED, now=w.h.clock.now,
                                          actor_session_id=admin_sid,csrf_token=admin_csrf,clock=lambda:w.h.clock.now)
            results[which] = "ok"
        except InvalidToken:
            results[which] = "invalid"
        except Exception as exc:
            results[which] = type(exc).__name__
    a, b = ("confirm", "other") if first == "confirm" else ("other", "confirm")
    t1, t2 = Thread(target=run, args=(a,), name="first"), Thread(target=run, args=(b,), name="second")
    try:
        t1.start()
        assert held.wait(20)
        t2.start()
        assert waiting.wait(20)
        release.set()
        t1.join(25)
        t2.join(25)
        assert not t1.is_alive() and not t2.is_alive(), "deadlock"
        assert results == {"other": "ok", "confirm": "ok" if first == "confirm" else "invalid"}
        assert gate(w.h.engine) == "open"
        assert len(records()) == 1
    finally:
        release.set()
        event.remove(w.h.engine, "before_cursor_execute", observe)


@pytest.mark.slow
@pytest.mark.parametrize("registered", [False, True])
def test_migrate_0030_backfill_hold_register_and_downgrade(empty_db_url, journal_path, app_env, admin_engine, registered,monkeypatch):
    from app import auth_safety
    monkeypatch.setattr(auth_safety,"cutover",lambda db:"ready")  # scoped pending result; actual old schemas/hold/replay
    upgrade(empty_db_url, "0030")
    app_env(empty_db_url)
    engine = create_engine(empty_db_url)
    now = utcnow()
    if registered:
        # Old schema registration: hold must work without any new mirror columns.
        genesis = journal.genesis(uuid.uuid4(), now, "new_install")
        journal.create(journal_path, [genesis])
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO recovery_registrations VALUES(1,:j,:h,'new_install',:t)"),
                         {"j": genesis.body["journal_id"], "h": genesis.hash, "t": now})
            conn.execute(text("INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed)"
                              " VALUES(1,'genesis',:h,:l,'genesis','new_install',true)"), {"h": genesis.hash, "l": genesis.line})
    with engine.begin() as conn:
        uid = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('legacy.reset@example.org','$argon2id$fixture') RETURNING id"))
        ids = []
        for end in ("used_at", "revoked_at"):
            _, digest = generate_token()
            ids.append(conn.scalar(text(f"INSERT INTO password_reset_tokens(user_id,token_hash,created_at,expires_at,{end})"
                                        " VALUES(:u,:h,:t,:e,:t) RETURNING id"),
                                   {"u": uid, "h": digest, "t": now, "e": now + get_settings().auth.password_reset}))
    with backup(SimpleNamespace(engine=engine), admin_engine) as snap:
        upgrade(empty_db_url)
        if registered:
            assert gate(engine) == "reset_tokens_not_journaled"
            assert recovery.status(engine, get_settings())["unjournaled_reset_tokens"] == 2
            assert recovery.recover(engine, None, get_settings(), now).reset_tokens_imported == 2
        else:
            with pytest.raises(recovery.RecoveryError, match="database_not_new"):
                recovery.init_new(engine, get_settings(), now)
            assert recovery.register_existing(engine, get_settings(), now)[1] == 2
        assert {r.body["reason_code"] for r in records()} == {"legacy_consumed", "legacy_revoked"}
        if registered:
            recovery.hold(snap.engine, get_settings(), now)  # Still schema 0030; seq/hash only.
        upgrade(snap.url)
        assert recovery.recover(snap.engine, None, get_settings(), now).gate == "open"
        assert recovery.recover(snap.engine, None, get_settings(), now).gate == "open"
        with pytest.raises(Exception, match="auth safety history prevents downgrade"):
            command.downgrade(alembic_config(empty_db_url), "0030")
    engine.dispose()


def test_expiry_alone_is_not_a_journal_event(world):
    w = world
    issued = issue(w)
    w.h.clock.advance(seconds=get_settings().auth.password_reset.total_seconds())
    assert confirm(w, issued.token).json() == {"error": "invalid_token"}
    assert records() == []
