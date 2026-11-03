"""Invitation closure decisions on real PostgreSQL/MinIO; external HTTP is blocked."""
from __future__ import annotations

import uuid
from datetime import timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import journal, recovery
from app.config import get_settings
from app.delivery import get_delivery
from app.identity import invitations, password_reset
from app.identity.invitation_endings import end_invitation, lock_email
from app.identity.session_commit import commit_auth
from app.identity.tokens import hash_token
from app.main import app
from app.models import Invitation
from tests.conftest import upgrade, alembic_config
from tests.helpers import USER_PASSWORD, ADMIN_EMAIL
from tests.test_recovery import backup, gate, fsync_fails
from tests.test_session_revocation_journal import break_write, failing_commit, browser
from tests.test_uploads import upload_api  # noqa: F401

EMAIL = "invitation.journal@example.org"
KIND = journal.INVITATION_CLOSURE


@pytest.fixture
def world(upload_api, monkeypatch):
    h, storage = upload_api
    aid = h.bootstrap_admin()
    admin, public = h.admin_browser(), h.browser()
    public.csrf()
    import httpx
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider blocked")))
    contexts = {}
    original = invitations.create_invitation
    def create(*args, **kwargs):
        created = original(*args, **kwargs)
        contexts[created._binding.invitation_id] = created  # known-commit backend result, not reconstructed from a request
        return created
    monkeypatch.setattr(invitations,"create_invitation",create)
    return SimpleNamespace(h=h, storage=storage, aid=aid, admin=admin,
                           cookies=dict(public.client.cookies), contexts=contexts)


def issue(w, email=EMAIL):
    r = w.admin.post("/admin/invitations", {"email": email})
    assert r.status_code == 201, r.text
    return SimpleNamespace(id=uuid.UUID(r.json()["invitation"]["id"]), token=w.h.last_link_token("invitation", email))


def accept(w, token, password=USER_PASSWORD):
    return browser(w.cookies).post("/auth/invitations/accept", {"token": token, "password": password})


def inspect(w, token):
    return browser(w.cookies).post("/auth/invitations/inspect", {"token": token})


def records():
    return [r for r in journal.read(get_settings().recovery.journal_path).records if r.kind == KIND]


def recover(w, engine=None, **kwargs):
    return recovery.recover(engine or w.h.setup_engine, w.storage, get_settings(), w.h.clock.now, **kwargs)


def compensate(w, old):
    """The current backend helper with the actual committed issuance context."""
    with Session(w.h.engine) as db:
        invitations._compensate_delivery(db,w.contexts[old.id],lambda:w.h.clock.now)


def batch_actor(w, db):
    context = w.admin.get("/auth/session").json()
    return invitations._actor(db, w.aid, uuid.UUID(context["session"]["id"]),
                              w.admin.client.cookies.get("ras_csrf"), lambda: w.h.clock.now)


def sample(w, iid=None, **changes):
    return dict(invitation_id=str(iid or w.h.scalar("SELECT id FROM invitations ORDER BY created_at LIMIT 1")),
                end_kind="revoked", reason_code="admin_revoked", ended_at=w.h.clock.now.isoformat()) | changes


def sql_mirror(db, body, **overrides):
    # Bypass Python validator to prove independent SQL INSERT guards.
    values = dict(i=body.get("invitation_id"), k=body.get("end_kind"), r=body.get("reason_code"),
                  at=body.get("ended_at")) | overrides
    seq=db.scalar(text("SELECT coalesce(max(seq),0)+1 FROM revocation_journal_records"))
    line = journal.canonical(dict(body=body, hash="f"*64, kind=KIND, prev="0"*64, seq=seq))
    db.execute(text("INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,"
                    "journal_confirmed,invitation_id,invitation_end_kind,invitation_reason_code,invitation_ended_at)"
                    " VALUES(:seq,'invitation_closure',repeat('f',64),:line,'close','closed',false,:i,:k,:r,:at)"),
               values | {"line": line,"seq":seq})


@pytest.mark.slow
@pytest.mark.parametrize("how", ["accept", "revoke", "replace", "delivery"])
def test_restore_twice_blocks_both_endpoints_without_recreating_account(world, admin_engine, app_env, monkeypatch, how):
    w = world
    old, valid = issue(w), issue(w, "valid.invitation@example.org")
    # Previous PR protections in this very same snapshot.
    uid = w.h.create_user("reset.invitation@example.org")
    session = w.h.user_browser("reset.invitation@example.org")
    cookies = dict(session.client.cookies)
    with Session(w.h.engine) as db:
        reset = password_reset.request_reset(db, email="reset.invitation@example.org", now=w.h.clock.now,
                                            lifetime=get_settings().auth.password_reset)
    with backup(w.h, admin_engine) as first, backup(w.h, admin_engine) as second:
        if how == "accept":
            response = accept(w, old.token)
            assert response.status_code == 201
            assert not any(c.startswith("ras_session=") for c in response.headers.get_list("set-cookie"))
        elif how == "revoke":
            assert w.admin.post(f"/admin/invitations/{old.id}/revoke").status_code == 204
        elif how == "replace":
            issue(w)
        else:
            # Link may have escaped BEFORE adapter reports failure; same compensation helper as route.
            compensate(w,old)
        assert browser(w.cookies).post("/auth/password-reset/confirm", {"token": reset.token,
                                      "new_password": "journal-invitation-New-123"}).status_code == 200
        for snap in (first, second):
            recovery.hold(snap.engine, get_settings(), w.h.clock.now)
            app_env(snap.url)
            assert inspect(w, old.token).status_code == accept(w, old.token).status_code == 503
            assert recover(w, snap.engine).gate == "open"
            for _ in range(2):
                assert inspect(w, old.token).json() == accept(w, old.token).json() == {"error": "invalid_token"}
                assert inspect(w, valid.token).status_code == 200
                assert recover(w, snap.engine).gate == "open"
            with snap.engine.connect() as conn:
                row = conn.execute(text("SELECT accepted_at,accepted_user_id,closed_by_recovery_at,expires_at"
                                        " FROM invitations WHERE id=:i"), {"i": old.id}).one()
                assert row.accepted_at is row.accepted_user_id is None
                assert row.closed_by_recovery_at and row.expires_at > w.h.clock.now
                assert conn.scalar(text("SELECT count(*) FROM users WHERE email=:e"), {"e": EMAIL}) == 0
            admin = w.h.admin_browser()
            listed = admin.get("/admin/invitations").json()["invitations"]
            assert next(i for i in listed if i["id"] == str(old.id))["status"] == "closed_by_recovery"
            assert browser(cookies).get("/auth/session").status_code == 401
            assert browser(w.cookies).post("/auth/password-reset/confirm", {"token": reset.token,
                                          "new_password": USER_PASSWORD}).json() == {"error": "invalid_token"}
            # Recovery close must not hold the pending email slot.
            assert admin.post("/admin/invitations", {"email": EMAIL}).status_code == 201
            if snap is second:
                assert accept(w, valid.token).status_code == 201
                assert accept(w, valid.token).status_code == 400
                assert admin.post("/admin/invitations", {"email": "valid.invitation@example.org"}).json() == {"error": "account_exists"}
            app_env(w.h.engine.url)


@pytest.mark.parametrize("accepted", [False, True])
def test_delivery_adapter_can_send_then_fail_and_accept_can_win_compensation(world, accepted):
    w = world
    captured = []
    class Adapter:
        enabled = True
        def send(self, message, *, now):
            token = message.link.split("#token=", 1)[1]
            captured.append(token)
            if accepted:
                assert accept(w, token).status_code == 201
            raise RuntimeError("adapter reported failure after delivery")
    app.dependency_overrides[get_delivery] = lambda: Adapter()
    try:
        r = w.admin.post("/admin/invitations", {"email": EMAIL})
    finally:
        app.dependency_overrides.pop(get_delivery, None)
    assert (r.status_code, r.json()) == (503, {"error": "delivery_failed"})
    assert inspect(w, captured[0]).status_code == accept(w, captured[0]).status_code == 400
    assert [r.body["reason_code"] for r in records()] == ["invitation_accepted" if accepted else "delivery_failed"]
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == int(accepted)


@pytest.mark.parametrize("field", ["invitation_id", "end_kind", "reason_code", "ended_at"])
@pytest.mark.parametrize("shape", ["missing", "null"])
def test_missing_null_body_rejected_before_append(world, field, shape):
    w = world
    issue(w)
    good = sample(w)
    before=get_settings().recovery.journal_path.read_bytes()
    body = good.copy()
    if shape == "missing":
        del body[field]
    else:
        body[field] = None
    with pytest.raises(journal.JournalError):
        journal.next_record(journal.read(get_settings().recovery.journal_path).head, KIND, body)
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, body, i=good["invitation_id"], k=good["end_kind"], r=good["reason_code"], at=good["ended_at"])
    assert get_settings().recovery.journal_path.read_bytes()==before


@pytest.mark.parametrize("changes", [dict(invitation_id="bad"), dict(end_kind="consumed"),
    dict(reason_code="invitation_accepted"), dict(reason_code="secret"), dict(raw_token="secret"),
    dict(token_hash="a"*64), dict(email=EMAIL), dict(ended_at="infinity"),
    dict(ended_at="2026-02-30T00:00:00Z"), dict(ended_at="2026-10-03T00:00:00"), dict(ended_at=123)])
def test_bad_body_sql_and_parser(world, changes):
    issue(world)
    body = sample(world, **changes)
    with pytest.raises(journal.JournalError):
        journal.next_record(journal.read(get_settings().recovery.journal_path).head, KIND, body)
    with pytest.raises(DBAPIError), Session(world.h.engine) as db:
        sql_mirror(db, body)


@pytest.mark.parametrize("column", ["i", "k", "r", "at"])
@pytest.mark.parametrize("null", [False, True])
def test_mirror_body_must_match_at_insert(world, column, null):
    issue(world)
    value = None if null else dict(i=str(uuid.uuid4()), k="accepted", r="delivery_failed",
                                  at=(world.h.clock.now + timedelta(seconds=1)).isoformat())[column]
    with pytest.raises(DBAPIError), Session(world.h.engine) as db:
        sql_mirror(db, sample(world), **{column: value})


@pytest.mark.parametrize("field", ["kind", "reason", "time", "id"])
def test_deferred_guard_matches_actual_closure(world, field):
    w = world
    old = issue(w)
    body = sample(w)
    if field == "kind":
        body.update(end_kind="accepted", reason_code="invitation_accepted")
    elif field == "reason":
        body["reason_code"] = "delivery_failed"
    elif field == "time":
        body["ended_at"] = (w.h.clock.now + timedelta(seconds=1)).isoformat()
    else:
        body["invitation_id"] = str(uuid.uuid4())
    with pytest.raises(DBAPIError), Session(w.h.setup_engine) as db:
        sql_mirror(db, body)
        db.execute(text("UPDATE invitations SET revoked_at=:t,closure_reason='admin_revoked' WHERE id=:i"),
                   {"i": old.id, "t": w.h.clock.now})
        db.commit()


def test_direct_sql_finality_and_append_only(world):
    w = world
    old = issue(w)
    with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
        conn.execute(text("UPDATE invitations SET revoked_at=:t WHERE id=:i"), {"i": old.id, "t": w.h.clock.now})
    assert accept(w, old.token).status_code == 201
    for change in ("accepted_at=NULL,accepted_user_id=NULL,closure_reason=NULL", "accepted_at=accepted_at+interval '1 second'",
                   "closure_reason='legacy_accepted'", "id=gen_random_uuid()", "email='other@example.org'",
                   "token_hash=repeat('a',64)", "expires_at=expires_at+interval '1 day'",
                   "accepted_user_id=NULL", "revoked_at=accepted_at"):
        with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
            conn.execute(text(f"UPDATE invitations SET {change} WHERE id=:i"), {"i": old.id})
    with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
        conn.execute(text("DELETE FROM invitations WHERE id=:i"), {"i": old.id})
    for sql in ("DELETE FROM revocation_journal_records WHERE invitation_id=:i",
                "UPDATE revocation_journal_records SET journal_confirmed=false WHERE invitation_id=:i",
                "UPDATE revocation_journal_records SET invitation_reason_code='legacy_accepted' WHERE invitation_id=:i"):
        with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
            conn.execute(text(sql), {"i": old.id})


@pytest.mark.parametrize("how", ["accept", "replace", "revoke", "compensate"])
@pytest.mark.parametrize("shape", ["none", "torn", "fsync"])
def test_append_faults_close_atomically_but_do_not_report_success(world, monkeypatch, how, shape):
    w = world
    old = issue(w)
    count = len(w.h.outbox().entries(now=w.h.clock.now))
    fix = fsync_fails(monkeypatch) if shape == "fsync" else break_write(monkeypatch, shape)
    if how == "accept":
        r = accept(w, old.token)
    elif how == "replace":
        r = w.admin.post("/admin/invitations", {"email": EMAIL})
    elif how == "revoke":
        r = w.admin.post(f"/admin/invitations/{old.id}/revoke")
    else:
        from app.api.errors import ApiError
        with pytest.raises(ApiError):
            compensate(w,old)
        r = SimpleNamespace(status_code=503)
    assert r.status_code == 503
    assert len(w.h.outbox().entries(now=w.h.clock.now)) == count
    assert w.h.scalar("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed") == (3 if how=="accept" else 1)
    assert w.h.scalar("SELECT accepted_at IS NOT NULL OR revoked_at IS NOT NULL FROM invitations WHERE id=:i", i=old.id)
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == int(how == "accept")
    assert gate(w.h.engine) != "open"
    fix()
    if shape != "fsync":
        with pytest.raises(recovery.RecoveryError, match="journal_behind_db"):
            recover(w)
    assert recover(w, confirm_db_current=shape != "fsync").gate == "open"
    assert inspect(w, old.token).status_code == accept(w, old.token).status_code == 400


@pytest.mark.parametrize("how", ["accept", "replace", "revoke"])
@pytest.mark.parametrize("lands", [False, True])
def test_commit_reply_loss_never_reports_success_or_delivery(world, monkeypatch, how, lands):
    w = world
    old = issue(w)
    count = len(w.h.outbox().entries(now=w.h.clock.now))
    fix = failing_commit(monkeypatch, lands)
    r = accept(w, old.token) if how == "accept" else w.admin.post(
        "/admin/invitations" if how == "replace" else f"/admin/invitations/{old.id}/revoke",
        {"email": EMAIL} if how == "replace" else None)
    assert (r.status_code, r.json()) == (503, {"error": "auth_outcome_uncertain"})
    fix()
    assert len(w.h.outbox().entries(now=w.h.clock.now)) == count
    if how=="accept" and not lands:
        assert gate(w.h.engine)=="db_behind_journal"  # no receipt-based inference or automatic replay
        recover(w)
    assert gate(w.h.engine) == "open"
    assert accept(w, old.token).status_code == 400
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == int(lands and how == "accept")
    assert w.h.scalar("SELECT closed_by_recovery_at IS NOT NULL FROM invitations WHERE id=:i", i=old.id) == (not lands)


def test_creation_only_unknown_commit_never_delivers(world, monkeypatch):
    w = world
    real = Session.commit
    def lost(db):
        if any(isinstance(r, Invitation) for r in db.identity_map.values()):
            real(db)
            raise RuntimeError("reply lost")
        real(db)
    monkeypatch.setattr(Session, "commit", lost)
    r = w.admin.post("/admin/invitations", {"email": EMAIL})
    assert (r.status_code, r.json()) == (503, {"error": "auth_outcome_uncertain"})
    assert w.h.outbox().entries(now=w.h.clock.now) == []


@pytest.mark.parametrize("shape", ["none", "torn", "prefix", "fsync"])
def test_multi_invitation_batch_prefix_with_rollback(world, monkeypatch, shape):
    w = world
    a, b = issue(w), issue(w, "batch.invitation@example.org")
    fix_write = fsync_fails(monkeypatch) if shape == "fsync" else break_write(monkeypatch, shape)
    fix_commit = failing_commit(monkeypatch, False)
    from app.api.errors import ApiError
    with pytest.raises(ApiError), Session(w.h.engine) as db:
        actor=batch_actor(w,db)  # User before email/invitation and journal
        for email in sorted([EMAIL, "batch.invitation@example.org"]):
            lock_email(db, email)
        from app.identity.invitation_endings import lock_invitations
        lock_invitations(db,[a.id,b.id])
        rows = db.query(Invitation).filter(Invitation.id.in_([a.id,b.id])).order_by(Invitation.id).all()
        for row in rows:
            end_invitation(db, row, kind="revoked", reason="admin_revoked", now=w.h.clock.now,actor=actor)
        ids = [r.id for r in rows]
        commit_auth(db)
    fix_write(); fix_commit()
    assert recover(w).gate == "open"
    ended = {uuid.UUID(r.body["invitation_id"]) for r in records()}
    assert ended == (set(ids) if shape == "fsync" else {ids[0]} if shape == "prefix" else set())
    assert w.h.scalar("SELECT count(*) FROM invitations WHERE closed_by_recovery_at IS NOT NULL") == len(ended)
    assert w.h.scalar("SELECT count(*) FROM users WHERE role='user'") == 0


def test_failed_replay_stays_gated(world, monkeypatch):
    w = world
    old = issue(w)
    fix = failing_commit(monkeypatch, False)
    real = recovery.replay_record
    monkeypatch.setattr(recovery, "replay_record", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert accept(w, old.token).json() == {"error": "auth_outcome_uncertain"}
    assert gate(w.h.engine) == "db_behind_journal"
    monkeypatch.setattr(recovery, "replay_record", real); fix()
    assert recover(w).gate == "open"
    assert accept(w, old.token).status_code == 400
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == 0


@pytest.mark.slow
@pytest.mark.parametrize("mirrored", [False, True])
@pytest.mark.parametrize("change", ["open", "kind", "reason", "time"])
def test_recover_postcondition_even_if_mirror_exists(world, admin_engine, app_env, mirrored, change):
    w = world
    old = issue(w)
    with backup(w.h, admin_engine) as snap:
        assert w.admin.post(f"/admin/invitations/{old.id}/revoke").status_code == 204
        if mirrored:
            assert recover(w, snap.engine).gate == "open"
        # A superuser bypass represents a corrupt snapshot; normal SQL is fenced.
        with snap.engine.begin() as conn:
            conn.execute(text("ALTER TABLE invitations DISABLE TRIGGER USER"))
            values = {"open": "closed_by_recovery_at=NULL,recovery_end_kind=NULL,closure_reason=NULL",
                      "kind": "revoked_at=NULL,closed_by_recovery_at=:t,recovery_end_kind='accepted',closure_reason='legacy_accepted'",
                      "reason": "revoked_at=NULL,closed_by_recovery_at=:t,recovery_end_kind='revoked',closure_reason='delivery_failed'",
                      "time": "revoked_at=NULL,closed_by_recovery_at=:later,recovery_end_kind='revoked',closure_reason='admin_revoked'"}[change]
            conn.execute(text(f"UPDATE invitations SET {values} WHERE id=:i"),
                         {"i": old.id, "t": w.h.clock.now, "later": w.h.clock.now + timedelta(seconds=1)})
            conn.execute(text("ALTER TABLE invitations ENABLE TRIGGER USER"))
        app_env(snap.url)
        if change == "open" and not mirrored:
            assert recover(w, snap.engine).gate == "open"  # expected replay of an open snapshot
        else:
            with pytest.raises(recovery.RecoveryError, match="invitation_not_ended|invitation_closure_conflict"):
                recover(w, snap.engine)
            assert gate(snap.engine) != "open"


@pytest.mark.slow
def test_absent_invitation_and_user_not_recreated(world, admin_engine, app_env):
    w = world
    with backup(w.h, admin_engine) as snap:
        old = issue(w)
        assert accept(w, old.token).status_code == 201
        app_env(snap.url)
        assert recover(w, snap.engine).replayed["recorded_invitation_absent"] == 1
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM invitations")) == 0
            assert conn.scalar(text("SELECT count(*) FROM users WHERE email=:e"), {"e": EMAIL}) == 0


def test_hashing_outside_locks_and_expiry_rechecked_after_hash(world, monkeypatch):
    w = world
    old = issue(w)
    actual = invitations.hasher_for_policy
    def factory(policy):
        hasher=actual(policy)
        class Slow:
            def hash(self,password):
                assert w.h.engine.pool.checkedout()==0
                result=hasher.hash(password)
                w.h.clock.advance(hours=72)
                return result
        return Slow()
    monkeypatch.setattr(invitations, "hasher_for_policy", factory)
    assert accept(w, old.token).json() == {"error": "invalid_token"}
    assert records() == []
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == 0


def test_configured_ttl_boundary_has_no_new_closure(world, monkeypatch):
    w = world
    monkeypatch.setenv("RAS_AUTH__INVITE", "PT1H")
    get_settings.cache_clear()
    old = issue(w)
    w.h.clock.advance(minutes=59, seconds=59)
    assert inspect(w, old.token).status_code == 200
    assert accept(w, old.token, "short").json()["error"] == "weak_password"
    w.h.clock.advance(seconds=1)
    assert inspect(w, old.token).json() == accept(w, old.token).json() == {"error": "invalid_token"}
    assert records() == []


def test_all_mirrors_false_and_flushed_before_one_append(world, monkeypatch):
    w = world
    a,b=issue(w),issue(w, "batch.invitation@example.org")
    actual = journal.append
    observed = []
    with Session(w.h.engine) as db:
        actor=batch_actor(w,db)
        for email in sorted([EMAIL, "batch.invitation@example.org"]):
            lock_email(db, email)
        from app.identity.invitation_endings import lock_invitations
        lock_invitations(db,[a.id,b.id])
        for row in db.query(Invitation).order_by(Invitation.id):
            end_invitation(db, row, kind="revoked", reason="admin_revoked", now=w.h.clock.now,actor=actor)
        def append(path, state, batch):
            flags = db.execute(text("SELECT journal_confirmed FROM revocation_journal_records WHERE invitation_id IS NOT NULL")).scalars().all()
            assert flags == [False, False]  # Same transaction, INSERT guards have already run.
            observed.append(len(batch))
            return actual(path, state, batch)
        monkeypatch.setattr(journal, "append", append)
        commit_auth(db)
    assert observed == [2]
    assert w.h.scalar("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed") == 0


def test_invalid_mirror_flush_rejects_before_any_journal_byte(world, monkeypatch):
    w = world
    old = issue(w)
    actual = recovery.invitation_body
    before = get_settings().recovery.journal_path.read_bytes()
    def wrong(*args, **kwargs):
        return actual(*args, **kwargs) | {"invitation_id":str(uuid.uuid4())}
    monkeypatch.setattr(recovery, "invitation_body", wrong)
    assert accept(w, old.token).json() == {"error": "auth_unavailable"}
    assert get_settings().recovery.journal_path.read_bytes() == before
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == 0
    assert inspect(w, old.token).status_code == 200


def test_absent_journal_identity_cannot_be_inserted_open(world):
    w = world
    iid = uuid.uuid4()
    state = journal.read(get_settings().recovery.journal_path)
    record = journal.next_record(state.head, KIND, sample(w, iid=iid))
    journal.append(get_settings().recovery.journal_path, state, [record])
    assert recovery.replay_record(w.h.setup_engine, record, w.h.clock.now) == ("recorded_invitation_absent", 0)
    with pytest.raises(DBAPIError), w.h.setup_engine.begin() as conn:
        conn.execute(text("INSERT INTO invitations(id,email,token_hash,invited_by_user_id,created_at,expires_at)"
                          " VALUES(:i,:e,repeat('a',64),:a,:t,:expires)"),
                     dict(i=iid,e=EMAIL,a=w.aid,t=w.h.clock.now,expires=w.h.clock.now+timedelta(hours=72)))


def test_journal_audit_logs_have_only_minimal_decision(world, caplog):
    w = world
    old = issue(w)
    assert accept(w, old.token).status_code == 201
    raw = get_settings().recovery.journal_path.read_text(encoding="ascii")
    audits = str(w.h.scalar("SELECT json_agg(row_to_json(a)) FROM audit_events a"))
    verifier = w.h.scalar("SELECT password_verifier FROM users WHERE email=:e", e=EMAIL)
    for secret in (old.token, hash_token(old.token), EMAIL, ADMIN_EMAIL, USER_PASSWORD, verifier):
        assert secret not in raw + audits + caplog.text
    assert all(set(r.body) == {"invitation_id", "end_kind", "reason_code", "ended_at"} for r in records())


@pytest.mark.parametrize("pair", [("accept", "revoke"), ("accept", "replace"), ("accept", "accept"),
                                  ("replace", "replace"), ("accept", "compensate")])
@pytest.mark.parametrize("reverse", [False, True])
def test_races_both_lock_orders_check_api_db_journal(world, monkeypatch, pair, reverse):
    import time
    from contextvars import ContextVar
    turn = ContextVar("invitation_race_turn", default=-1)
    w = world
    old = issue(w)
    admin_a, admin_b = w.h.admin_browser(), w.h.admin_browser()
    first_locked, second_started, release = Event(), Event(), Event()
    actual = invitations.lock_email
    def synchronized(db, email):
        db.execute(text("SET LOCAL lock_timeout='8s'"))
        db.execute(text("SET LOCAL statement_timeout='12s'"))
        if turn.get() == 0:
            actual(db, email)
            first_locked.set()
            assert release.wait(8), "race controller failed to release first lock"
        else:
            second_started.set()
            actual(db, email)
    monkeypatch.setattr(invitations, "lock_email", synchronized)
    user_lock=invitations.lock_users
    def synchronized_users(db,ids):
        if turn.get()==1:second_started.set()
        return user_lock(db,ids)
    monkeypatch.setattr(invitations,"lock_users",synchronized_users)
    # Same-Admin replacements may wait while principal resolution touches the
    # User, before the handler reaches its email lock. Observe the real API
    # engine as well as the handler, then require a PostgreSQL blocking edge.
    from app.db import get_engine
    from sqlalchemy import event
    api_engine=get_engine()
    def observe(conn,cursor,sql,params,context,many):
        if turn.get()==1:second_started.set()
    event.listen(api_engine,"before_cursor_execute",observe)
    sequence = list(reversed(pair)) if reverse else list(pair)
    results, errors = {}, []
    def run(index):
        turn.set(index)
        try:
            how = sequence[index]
            admin = (admin_a, admin_b)[index]
            if how == "accept":
                r = accept(w, old.token)
            elif how == "replace":
                r = admin.post("/admin/invitations", {"email": EMAIL})
            elif how == "revoke":
                r = admin.post(f"/admin/invitations/{old.id}/revoke")
            else:
                compensate(w,old)
                r = SimpleNamespace(status_code=204)
            results[index] = r
        except BaseException as exc:
            errors.append(exc)
    a = Thread(target=run, args=(0,), name="inv-first", daemon=True)
    b = Thread(target=run, args=(1,), name="inv-second", daemon=True)
    a.start()
    assert first_locked.wait(8)
    b.start()
    try:
        assert second_started.wait(8)
        until = time.monotonic() + 6
        while time.monotonic() < until:
            if w.h.scalar("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
                          " AND cardinality(pg_blocking_pids(pid))>0"):
                break
            time.sleep(.01)
        else:
            pytest.fail("second transaction did not block on the ordered User/email locks")
    finally:
        release.set()
        a.join(15); b.join(15)
        event.remove(api_engine,"before_cursor_execute",observe)
    assert not a.is_alive() and not b.is_alive(), "deadlock/timeout"
    assert errors == []
    first, second = sequence
    assert results[0].status_code == (201 if first in ("accept", "replace") else 204)
    expected = 400 if second == "accept" else 409 if first == "accept" and second == "replace" \
        else 404 if first == "accept" and second == "revoke" else 204 if second in ("revoke", "compensate") else 201
    assert results[1].status_code == expected
    reason = "invitation_accepted" if first == "accept" else "invitation_replaced" if first == "replace" \
        else "delivery_failed" if first == "compensate" else "admin_revoked"
    decisions = records()
    assert next(r for r in decisions if r.body["invitation_id"] == str(old.id)).body["reason_code"] == reason
    assert len(decisions) == (2 if pair == ("replace", "replace") else 1)
    assert w.h.scalar("SELECT count(*) FROM users WHERE email=:e", e=EMAIL) == int(first == "accept")
    assert w.h.scalar("SELECT count(*) FROM invitations WHERE email=:e AND accepted_at IS NULL AND revoked_at IS NULL"
                      " AND closed_by_recovery_at IS NULL", e=EMAIL) == int("replace" in pair and first != "accept")
    assert gate(w.h.engine) == "open"


def test_0031_history_backfill_hold_before_migrate_and_downgrade_fence(empty_db_url, journal_path, monkeypatch):
    from app import auth_safety
    from tests.conftest import HEAD_REVISION
    monkeypatch.setattr(auth_safety,"cutover",lambda db:"ready")  # recovery/hold/auth remain real
    from alembic import command
    from sqlalchemy import create_engine
    from app.identity.passwords import hash_password
    from app.clock import utcnow
    now = utcnow()
    upgrade(empty_db_url, "0031")
    eng = create_engine(empty_db_url, hide_parameters=True)
    settings = SimpleNamespace(recovery=SimpleNamespace(journal_path=journal_path))
    genesis = journal.genesis(uuid.uuid4(), now, "new_install")
    with eng.begin() as conn:
        # Only old columns; emulate the already-registered 0031 deployment.
        conn.execute(text("INSERT INTO recovery_registrations(id,journal_id,genesis_hash,mode,registered_at)"
                          " VALUES(1,:i,:h,'new_install',:t)"),
                     {"i": genesis.body["journal_id"], "h": genesis.hash, "t": now})
        conn.execute(text("INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed)"
                          " VALUES(1,'genesis',:h,:l,'genesis','new_install',true)"), {"h": genesis.hash, "l": genesis.line})
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('legacy.admin@example.org',:v,'admin') RETURNING id"),
                            {"v": hash_password(USER_PASSWORD)})
        uid = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('legacy.accepted@example.org',:v) RETURNING id"),
                          {"v": hash_password(USER_PASSWORD)})
        for state in ("accepted", "revoked", "open"):
            conn.execute(text("INSERT INTO invitations(email,token_hash,invited_by_user_id,created_at,expires_at,"
                              " accepted_at,accepted_user_id,revoked_at) VALUES(:e,:hash,:a,:t,:expire,:accepted,:u,:revoked)"),
                         dict(e=f"legacy.{state}@example.org", hash=hash_token(state), a=admin, t=now,
                              expire=now+timedelta(hours=72), accepted=now if state == "accepted" else None,
                              u=uid if state == "accepted" else None, revoked=now if state == "revoked" else None))
    journal.create(journal_path, [genesis])
    # Compatible pre-migration hold must never select new mirror columns.
    assert recovery.hold(eng, settings, now)
    upgrade(empty_db_url)
    from tests.conftest import db_env
    from app.config import get_settings
    for key, value in db_env(empty_db_url).items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    settings = get_settings()
    assert gate(eng) == "db_behind_journal"
    assert recovery.status(eng, settings)["unjournaled_invitations"] == 2
    recovery.replay_record(eng, journal.read(journal_path).head, now)
    assert gate(eng) == "invitations_not_journaled"
    # No revoked objects in this database; storage is never used by cleanup.
    report = recovery.recover(eng, None, settings, now)
    assert report.gate == "open" and report.invitations_imported == 2
    assert {r.body["reason_code"] for r in records()} == {"legacy_accepted", "legacy_revoked"}
    assert recovery.recover(eng, None, settings, now).invitations_imported == 0
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT count(*) FROM users")) == 2
        assert conn.scalar(text("SELECT accepted_user_id FROM invitations WHERE accepted_at IS NOT NULL")) == uid
    with pytest.raises(Exception, match="auth safety history prevents downgrade"):
        command.downgrade(alembic_config(empty_db_url), "0031")
    eng.dispose()
