"""Final caller reads with runtime LOGIN, real receipts and independent journal.

Shared business setup uses the real C4 contract. Privileged corruptions below are test setup,
never runtime grants; dependency wrappers always authenticate/touch for real.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery
from app.api import deps
from app.api.errors import ApiError
from app.api.routes import admin as admin_routes
from app.config import get_settings
from app.delivery import DevOutbox, DisabledDelivery, MessageKind, OutgoingMessage, get_delivery
from app.identity.final_caller import final_caller
from app.identity.predicates import lock_users
from app.identity.session_commit import AUTH_PENDING, commit_auth
from app.identity.sessions import revoke_session
from app.models import User, UserSession
from app.models.enums import SessionRevokeReason
from app.main import app
from tests.helpers import DEV_ENV
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as world, issue, accept, PASSWORD, EMAIL
from tests.test_auth_safety_foundation import NOW, event

PATHS = ['/auth/session', '/admin/users', '/admin/dev-outbox']
LINK = 'http://web.test/#token=final-private-token-marker'


@pytest.fixture
def browser(world, monkeypatch):
    w = world
    for key, value in DEV_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    b = SimpleNamespace(w=w, at=NOW, after_dependency=lambda db, ctx: None)
    b.clock = lambda: b.at
    b.outbox = DevOutbox(5)
    b.outbox.send(OutgoingMessage(MessageKind.INVITATION, EMAIL, LINK,
                                 NOW + timedelta(hours=1)), now=NOW)
    b.reads = []
    entries = b.outbox.entries
    def counted(*, now):
        b.reads.append(now)
        return entries(now=now)
    monkeypatch.setattr(b.outbox, 'entries', counted)
    def context(request: Request, db=Depends(deps.get_db), settings=Depends(get_settings)):
        ctx = deps.get_session_context(request, db=db, now=b.clock(), settings=settings, clock=b.clock)
        assert not db.in_transaction()
        b.after_dependency(db, ctx)
        return ctx
    app.dependency_overrides[deps.get_session_context] = context
    app.dependency_overrides[deps.get_clock] = lambda: b.clock
    app.dependency_overrides[deps.get_now] = b.clock
    app.dependency_overrides[get_delivery] = lambda: b.outbox
    try:
        with TestClient(app, base_url='http://web.test') as client:
            client.cookies.set('ras_session', w.actor.token)
            b.client = client
            yield b
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def close(w):
    with Session(w.runtime) as db:
        revoke_session(db, session_id=w.actor.session_id, user_id=w.uid,
                       reason=SessionRevokeReason.USER_REVOKED, now=NOW)
        commit_auth(db)


def mutate(b, change):
    w = b.w
    if change == 'revoke':
        close(w)
    elif change == 'disable':
        with Session(w.runtime) as db:
            lock_users(db, [w.uid])
            db.info[AUTH_PENDING] = [auth_safety.Change(w.uid, 'status', 'admin_disable', NOW, decision='disabled')]
            commit_auth(db)
    elif change in ('hold', 'ahead', 'unconfirmed', 'latest_credential', 'latest_status'):
        if change == 'hold':
            recovery.hold(w.engine, w.settings, NOW)
            return
        kind = journal.ACCOUNT_STATUS_DECISION if change == 'latest_status' else journal.CREDENTIAL_TRANSITION
        with Session(w.runtime) as db:
            lock_users(db, [w.uid])
            recovery._lock_journal(db)
            state = journal.read(w.path)
            record = journal.next_record(state.head, kind, event(kind, w.uid))
            if change != 'ahead':
                db.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"), {'line': record.line})
            journal.append(w.path, state, [record])
            if change.startswith('latest_'):
                db.execute(text('SELECT ras_auth_confirm(:seq)'), {'seq': record.seq})
            db.commit()
    else:
        sql = {
            'quarantine': "UPDATE users SET credential_state='quarantined' WHERE id=:uid",
            'held': "UPDATE users SET status_safety='held' WHERE id=:uid",
            'generation': 'UPDATE sessions SET credential_generation=gen_random_uuid() WHERE id=:sid',
            'role': "UPDATE users SET role='user' WHERE id=:uid",
            'receipt_credential': "DELETE FROM auth_change_receipts WHERE user_id=:uid AND domain='credential'",
            'receipt_status': "DELETE FROM auth_change_receipts WHERE user_id=:uid AND domain='status'",
            'wrong_receipt': "UPDATE auth_change_receipts SET reason_code='verified_recovery' WHERE user_id=:uid AND domain='credential'",
            'head': 'UPDATE auth_safety_heads SET credential_generation=gen_random_uuid() WHERE user_id=:uid',
        }[change]
        with w.engine.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            conn.execute(text(sql), {'uid': w.uid, 'sid': w.actor.session_id})


@pytest.mark.parametrize('path', PATHS)
def test_valid_admin_shape_and_allowlist(browser, path, caplog):
    b = browser
    response = b.client.get(path)
    assert response.status_code == 200, response.text
    data = response.json()
    if path == '/auth/session':
        assert set(data) == {'user', 'session'}
        assert set(data['user']) == {'id', 'email', 'display_name', 'role'}
        assert set(data['session']) == {'id', 'idle_expires_at', 'absolute_expires_at'}
    elif path == '/admin/users':
        assert set(data['users'][0]) == {'id', 'email', 'display_name', 'role', 'status', 'created_at'}
    else:
        assert data['messages'][0]['link'] == LINK
        with Session(b.w.runtime) as db:
            audit = ' '.join(db.execute(text('SELECT row_to_json(a)::text FROM audit_events a')).scalars())
        assert LINK not in audit + caplog.text + b.w.path.read_text()
    assert 'password_verifier' not in response.text and 'csrf_token_hash' not in response.text


@pytest.mark.parametrize('path', PATHS)
def test_real_user_session_and_admin_denial(browser, path):
    from app.identity.login import login
    b = browser
    accept(b.w, issue(b.w))
    with Session(b.w.runtime) as db:
        issued = login(db, email=EMAIL, password=PASSWORD, now=NOW,
                       settings=b.w.settings, clock=b.clock)[1]
    b.client.cookies.set('ras_session', issued.token)
    response = b.client.get(path)
    assert response.status_code == (200 if path == '/auth/session' else 403)
    assert not b.reads
    assert LINK not in response.text


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('change', ['revoke', 'disable', 'generation', 'quarantine', 'held',
    'receipt_credential', 'receipt_status', 'wrong_receipt', 'head', 'latest_credential',
    'latest_status', 'hold', 'ahead', 'unconfirmed'])
def test_changes_after_dependency_block_payload(browser, path, change):
    b = browser
    b.after_dependency = lambda db, ctx: mutate(b, change)
    response = b.client.get(path)
    expected = 503 if change in ('hold', 'ahead', 'unconfirmed', 'receipt_credential', 'receipt_status', 'wrong_receipt', 'head', 'latest_credential', 'latest_status') else 401
    assert response.status_code == expected, response.text
    assert response.json() == {'error': 'service_recovering' if expected == 503 else 'session_expired'}
    if expected == 401:
        assert 'Max-Age=0' in response.headers['set-cookie']
    assert not b.reads and LINK not in response.text and 'users' not in response.json()


@pytest.mark.parametrize('path', PATHS)
def test_exact_caller_owner_binding(browser, path):
    b = browser
    other = accept(b.w, issue(b.w))
    def rebind(db, ctx):
        # A live session for the OTHER proven owner must not satisfy this caller.
        with b.w.engine.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            conn.execute(text('UPDATE sessions SET user_id=:uid, credential_generation=:gen WHERE id=:sid'),
                         {'uid': other.id, 'gen': other.credential_generation, 'sid': ctx.session_id})
    b.after_dependency = rebind
    response = b.client.get(path)
    assert response.status_code == 401, response.text
    assert not b.reads


@pytest.mark.parametrize('path', ['/admin/users', '/admin/dev-outbox'])
def test_stale_admin_role_denied(browser, path):
    b = browser
    b.after_dependency = lambda db, ctx: mutate(b, 'role')
    response = b.client.get(path)
    assert response.status_code == 403 and response.json() == {'error': 'forbidden'}
    assert not b.reads


def test_profile_expiry_reload_and_no_final_touch(browser):
    b = browser
    expected = NOW + timedelta(minutes=1)
    def update(db, ctx):
        # Populate stale identities on the actual route connection deliberately.
        b.stale_user = db.get(User, ctx.user_id)
        b.stale_session = db.get(UserSession, ctx.session_id)
        with b.w.engine.begin() as conn:
            conn.execute(text("UPDATE users SET email='new@example.test',display_name='new profile',role='user' WHERE id=:uid"), {'uid': ctx.user_id})
            conn.execute(text('UPDATE sessions SET last_activity_at=:at,absolute_expires_at=:end WHERE id=:sid'),
                         {'at': expected, 'end': NOW+timedelta(hours=7), 'sid': ctx.session_id})
        b.at = expected + timedelta(minutes=1)
    b.after_dependency = update
    response = b.client.get('/auth/session')
    assert response.status_code == 200, response.text
    assert response.json()['user'] == {'id': str(b.w.uid), 'email': 'new@example.test', 'display_name': 'new profile', 'role': 'user'}
    assert response.json()['session']['idle_expires_at'] == (expected+timedelta(minutes=30)).isoformat()
    assert response.json()['session']['absolute_expires_at'] == (NOW+timedelta(hours=7)).isoformat()
    # C4 verifies fresh User bindings; session activity still must not be touched.
    assert b.stale_user.email == 'new@example.test'
    assert b.stale_session.last_activity_at == NOW
    with Session(b.w.runtime) as db:
        assert db.get(UserSession, b.w.actor.session_id).last_activity_at == expected


def test_current_session_idle_expiry_capped_by_absolute(browser):
    b = browser
    def update(db, ctx):
        b.at = NOW + timedelta(hours=7, minutes=59)
        with b.w.engine.begin() as conn:
            conn.execute(text('UPDATE sessions SET last_activity_at=:at WHERE id=:sid'),
                         {'at': b.at, 'sid': ctx.session_id})
    b.after_dependency = update
    response = b.client.get('/auth/session')
    assert response.status_code == 200, response.text
    session = response.json()['session']
    assert session['idle_expires_at'] == session['absolute_expires_at'] == (NOW+timedelta(hours=8)).isoformat()


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('kind,seconds,expected', [('idle',1799,200), ('idle',1800,401), ('absolute',28799,200), ('absolute',28800,401)])
def test_fresh_clock_boundaries_after_dependency(browser, path, kind, seconds, expected):
    b = browser
    def advance(db, ctx):
        with b.w.engine.begin() as conn:
            if kind == 'absolute':
                conn.execute(text('UPDATE sessions SET last_activity_at=:at WHERE id=:sid'),
                             {'at': NOW+timedelta(hours=7,minutes=59), 'sid': ctx.session_id})
            b.activity = conn.scalar(text('SELECT last_activity_at FROM sessions WHERE id=:sid'), {'sid': ctx.session_id})
        b.at = NOW + timedelta(seconds=seconds)
    b.after_dependency = advance
    response = b.client.get(path)
    assert response.status_code == expected, response.text
    with Session(b.w.runtime) as db:
        assert db.get(UserSession, b.w.actor.session_id).last_activity_at == b.activity


def test_admin_users_clock_after_query_and_held_targets_visible(browser, monkeypatch):
    b = browser
    other = accept(b.w, issue(b.w))
    with b.w.engine.begin() as conn:
        conn.execute(text("UPDATE users SET credential_state='quarantined',status_safety='held' WHERE id=:uid"), {'uid': other.id})
    real = admin_routes.users.list_users
    def query(db):
        rows = real(db)
        b.at = NOW + timedelta(minutes=30)
        return rows
    monkeypatch.setattr(admin_routes.users, 'list_users', query)
    assert b.client.get('/admin/users').status_code == 401
    monkeypatch.setattr(admin_routes.users, 'list_users', real)
    b.at = NOW
    response = b.client.get('/admin/users')
    assert response.status_code == 200
    assert {u['id'] for u in response.json()['users']} == {str(other.id), str(b.w.uid)}


@pytest.mark.parametrize('change', ['revoke', 'role', 'hold', 'ahead', 'unconfirmed', 'idle'])
def test_outbox_after_audit_commit_rechecks(browser, monkeypatch, change):
    b = browser
    real = admin_routes.commit_activity
    def commit(db):
        real(db)
        assert not db.in_transaction()
        if change == 'idle': b.at = NOW + timedelta(minutes=30)
        else: mutate(b, change)
    monkeypatch.setattr(admin_routes, 'commit_activity', commit)
    response = b.client.get('/admin/dev-outbox')
    assert response.status_code == (403 if change == 'role' else 503 if change in ('hold','ahead','unconfirmed','receipt_credential','receipt_status','wrong_receipt','head','latest_credential','latest_status') else 401), response.text
    assert not b.reads and LINK not in response.text
    with Session(b.w.runtime) as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='dev_outbox.read'")) == 1


@pytest.mark.parametrize('landed', [False, True])
def test_outbox_commit_unknown_never_returns_messages(browser, monkeypatch, landed):
    b = browser
    def driver():
        with Session(b.w.runtime) as db:
            commit = db.commit
            def reply():
                audit = db.scalar(text("SELECT count(*) FROM audit_events WHERE action='dev_outbox.read'"))
                if not audit: return commit()  # dependency activity commit remains real
                if landed: commit()
                else: db.rollback()
                raise OSError('synthetic unknown commit reply')
            monkeypatch.setattr(db, 'commit', reply)
            yield db
    app.dependency_overrides[deps.get_db] = driver
    response = b.client.get('/admin/dev-outbox')
    assert response.status_code == 503 and response.json() == {'error': 'auth_outcome_uncertain'}
    assert not b.reads and LINK not in response.text
    with Session(b.w.runtime) as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='dev_outbox.read'")) == int(landed)


@pytest.mark.parametrize('phase', ['commit', 'snapshot'])
def test_outbox_ttl_uses_fresh_clock(browser, monkeypatch, phase):
    b = browser
    b.outbox.clear()
    b.outbox.send(OutgoingMessage(MessageKind.INVITATION, EMAIL, LINK, NOW+timedelta(seconds=1)), now=NOW)
    if phase == 'commit':
        real = admin_routes.commit_activity
        def commit(db): real(db); b.at += timedelta(seconds=1)
        monkeypatch.setattr(admin_routes, 'commit_activity', commit)
    else:
        real = b.outbox.entries
        def entries(*, now):
            result = real(now=now); b.at += timedelta(seconds=1); return result
        monkeypatch.setattr(b.outbox, 'entries', entries)
    response = b.client.get('/admin/dev-outbox')
    assert response.status_code == 200 and response.json() == {'messages': []}


def test_outbox_auth_expiry_after_snapshot(browser, monkeypatch):
    b = browser
    real = b.outbox.entries
    def entries(*, now):
        result = real(now=now); b.at = NOW+timedelta(minutes=30); return result
    monkeypatch.setattr(b.outbox, 'entries', entries)
    response = b.client.get('/admin/dev-outbox')
    assert response.status_code == 401 and LINK not in response.text


@pytest.mark.parametrize('mode', ['disabled', 'production'])
def test_outbox_mode_still_404(browser, mode):
    b = browser
    if mode == 'disabled': app.dependency_overrides[get_delivery] = lambda: DisabledDelivery()
    else:
        settings = get_settings().model_copy(update={'environment': 'production'})
        app.dependency_overrides[get_settings] = lambda: settings
    assert b.client.get('/admin/dev-outbox').status_code == 404
    assert not b.reads


def guard(w, db, clock=None):
    return final_caller(db, session_id=w.actor.session_id, user_id=w.uid,
                        clock=clock or w.clock, idle=w.settings.auth.session_idle, admin=True)


def wait_for(w, event):
    deadline = time.monotonic()+10
    with w.engine.connect() as observer:
        while time.monotonic() < deadline:
            found = observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND usename='c1_runtime' AND wait_event=:e)"), {'e': event})
            observer.rollback()
            if found: return
            threading.Event().wait(.005)
    raise AssertionError('database wait not observed')


def test_clock_after_observed_final_journal_wait(world):
    w = world; at = [NOW]; entered = threading.Event()
    def reader():
        with Session(w.runtime) as db:
            entered.set()
            return guard(w, db, lambda: at[0])
    with w.runtime.connect() as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': recovery.JOURNAL_LOCK})
        future = pool.submit(reader)
        try:
            assert entered.wait(10); wait_for(w, 'advisory')
            at[0] = NOW+timedelta(minutes=30)
        finally: blocker.rollback()
        with pytest.raises(ApiError, match='session_expired'): future.result(15)


def test_final_shared_lock_same_transaction_resource_order_and_response_boundary(world):
    w = world; entered = threading.Event()
    item_id = uuid.uuid4()
    with w.engine.begin() as conn:
        conn.execute(text("INSERT INTO library_items(id,visibility,owner_user_id,title) VALUES(:id,'private',:uid,'synthetic resource')"),
                     {'id': item_id, 'uid': w.uid})
    with Session(w.runtime) as db, ThreadPoolExecutor(1) as pool:
        # A business row stands for an already acquired resource lock.
        db.execute(text('SELECT id FROM library_items WHERE id=:id FOR SHARE'), {'id': item_id})
        pid = db.scalar(text('SELECT pg_backend_pid()'))
        caller = guard(w, db)
        assert caller.user_id == w.uid and db.in_transaction()
        assert db.scalar(text('SELECT pg_backend_pid()')) == pid
        assert db.scalar(text("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND mode='ShareLock' AND granted")) == 1
        # No User/session row lock: another writer can lock them BEFORE journal.
        def writer():
            with Session(w.runtime) as writer_db:
                revoke_session(writer_db, session_id=w.actor.session_id, user_id=w.uid,
                               reason=SessionRevokeReason.USER_REVOKED, now=NOW)
                entered.set()
                commit_auth(writer_db)
        future = pool.submit(writer)
        try:
            assert entered.wait(10); wait_for(w, 'advisory')
            assert not future.done()
        finally: db.rollback()
        future.result(15)
        # An authorized DTO may already have left this boundary; new proof fails.
        assert caller.role == 'admin'
        with pytest.raises(ApiError, match='session_expired'): guard(w, db)
    assert w.runtime.pool.checkedout() == 0


@pytest.mark.parametrize('path', PATHS + ['/healthz', '/readyz'])
def test_real_pending_http_without_cutover_patch(installation, path):
    w = installation
    with Session(w.runtime) as db:
        assert auth_safety.cutover(db) == 'auth_safety_pending'
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == (200 if path == '/healthz' else 503)


@pytest.mark.parametrize('path', PATHS)
def test_http_revocation_wins_barrier_before_final(browser, path):
    b = browser; entered = threading.Event(); release = threading.Event()
    def pause(db, ctx):
        entered.set()
        assert release.wait(15)
    b.after_dependency = pause
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(b.client.get, path)
        try:
            assert entered.wait(15)
            close(b.w)
        finally: release.set()
        response = future.result(20)
    assert response.status_code == 401 and LINK not in response.text
    assert not b.reads


def test_revocation_commits_while_reader_waits_final_lock(world):
    w = world; entered = threading.Event()
    def reader():
        with Session(w.runtime) as db:
            entered.set()
            return guard(w, db)
    with Session(w.runtime) as writer, ThreadPoolExecutor(1) as pool:
        revoke_session(writer, session_id=w.actor.session_id, user_id=w.uid,
                       reason=SessionRevokeReason.USER_REVOKED, now=NOW)
        writer.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': recovery.JOURNAL_LOCK})
        future = pool.submit(reader)
        try:
            assert entered.wait(10); wait_for(w, 'advisory')
            commit_auth(writer)
        finally: writer.rollback()
        with pytest.raises(ApiError, match='session_expired'): future.result(15)


def test_outbox_audit_flush_failure_controlled(browser, monkeypatch):
    b = browser
    real = admin_routes.record_event
    def broken(db, **kw):
        real(db, **kw)
        def flush(*args, **kwargs): raise OSError('synthetic audit flush failure')
        monkeypatch.setattr(db, 'flush', flush)
    monkeypatch.setattr(admin_routes, 'record_event', broken)
    response = b.client.get('/admin/dev-outbox')
    assert response.status_code == 503 and response.json() == {'error': 'auth_unavailable'}
    assert not b.reads and LINK not in response.text
