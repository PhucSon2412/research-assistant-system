"""Post-SQL time boundaries with real runtime auth, journal and predicates.

Hooks identify the response phase; latency advances time only AFTER real SQL.
The business fixtures scope cutover only. No receipt/head/ACL is mocked.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.api import resource_reads, review_publication_reads
from app.api.errors import ApiError
from app.api.routes import admin, auth
from app.db import get_engine
from app.identity import final_caller as callers
from app.models import User, UserSession
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as world, issue, accept
from tests.test_c2d1_final_caller import browser, mutate
from tests.test_c2d2a_resource_reads import resources
from tests.test_c2d2b_review_publication_reads import reads
from tests.test_c2d3_resource_writes import writes, request, business, changed, audits


def expiry(b, kind):
    """Nondefault idle, or absolute earlier than idle. Keep dependency live."""
    from app.config import get_settings
    import os
    os.environ['RAS_AUTH__SESSION_IDLE'] = 'PT7M'
    get_settings.cache_clear()
    assert get_settings().auth.session_idle == timedelta(seconds=420)
    end = NOW + timedelta(seconds=420 if kind == 'idle' else 240)
    with b.w.engine.begin() as conn:
        conn.execute(text('UPDATE sessions SET absolute_expires_at=:end WHERE id=:s'),
                     {'end': NOW + timedelta(hours=8) if kind == 'idle' else end,
                      's': b.w.actor.session_id})
    return end


@pytest.fixture(autouse=True)
def restore_idle_env(monkeypatch):
    # expiry uses process env because Settings is also constructed by HTTP DI.
    monkeypatch.setenv('RAS_AUTH__SESSION_IDLE', 'PT30M')


def slow_final(monkeypatch, b, boundary, end):
    """Arm only the proof after resource requery, never the entry proof."""
    phase = SimpleNamespace(initial_proof=False, final=False, sql_done=False)
    real_caller, real_live = boundary.final_caller, callers.session_live

    def caller(db, **kw):
        phase.final = phase.initial_proof
        result = real_caller(db, **kw)
        phase.initial_proof = True
        return result

    def live(db, *args, **kw):
        result = real_live(db, *args, **kw)
        if phase.final:
            assert result  # the real predicate was live before simulated latency
            phase.sql_done = True
            b.at = end
        return result

    monkeypatch.setattr(boundary, 'final_caller', caller)
    monkeypatch.setattr(callers, 'session_live', live)
    return phase


def denied(response):
    assert response.status_code == 401, f'final SQL completed at expiry but GET returned {response.status_code}'
    assert response.json() == {'error': 'session_expired'}
    assert 'Max-Age=0' in response.headers['set-cookie']
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('kind', ['idle', 'absolute'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_collections_final_sql_expiry(browser, monkeypatch, kind, offset):
    b = browser
    end = expiry(b, kind)
    with b.w.engine.begin() as conn:
        conn.execute(text("INSERT INTO collections(owner_user_id,name) VALUES(:u,'clock fixture')"), {'u': b.w.uid})
    assert b.client.get('/collections').status_code == 200
    phase = slow_final(monkeypatch, b, resource_reads, end + timedelta(microseconds=offset))
    response = b.client.get('/collections')
    assert phase.sql_done
    if offset < 0:
        assert response.status_code == 200
    else:
        denied(response)


@pytest.mark.parametrize('kind', ['idle', 'absolute'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
@pytest.mark.parametrize('phase', ['profile', 'predicate'])
def test_direct_proof_post_sql_clock_no_touch_commit_or_new_locks(browser, monkeypatch, kind, offset, phase):
    b = browser
    end = expiry(b, kind)
    idle = timedelta(seconds=420)
    seen = []
    with Session(b.w.runtime) as db:
        stale_user = db.get(User, b.w.uid)
        stale_session = db.get(UserSession, b.w.actor.session_id)
        with b.w.engine.begin() as conn:
            conn.execute(text("UPDATE users SET display_name='fresh clock profile' WHERE id=:u"), {'u': b.w.uid})
        activity = stale_session.last_activity_at
        tx = db.get_transaction()
        def after_sql(conn, cursor, statement, parameters, context, many):
            seen.append(statement)
            target = ('ras_auth_session_live' in statement if phase == 'predicate'
                      else 'users.email' in statement and 'sessions.last_activity_at' in statement)
            if target:
                b.at = end + timedelta(microseconds=offset)
        event.listen(b.w.runtime, 'after_cursor_execute', after_sql)
        def no_commit():
            raise AssertionError('read helper committed')
        monkeypatch.setattr(db, 'commit', no_commit)
        try:
            if offset >= 0:
                with pytest.raises(ApiError, match='session_expired') as error:
                    callers.final_caller(db, session_id=b.w.actor.session_id, user_id=b.w.uid,
                                         clock=b.clock, idle=idle)
                assert error.value.clear_session
            else:
                proof = callers.final_caller(db, session_id=b.w.actor.session_id, user_id=b.w.uid,
                                             clock=b.clock, idle=idle)
                assert proof.checked_at == b.at == end + timedelta(microseconds=offset)
                assert proof.idle_expires_at == end
                assert proof.display_name == 'fresh clock profile'
                # C4 verifies current User bindings and refreshes loaded ORM rows.
                assert stale_user.display_name == proof.display_name
            assert db.get_transaction() is tx and tx.is_active
            assert not db.new and not db.dirty and not db.deleted
            assert not any('FOR UPDATE' in s.upper() or 'FOR SHARE' in s.upper() or 'UPDATE SESSIONS' in s.upper() for s in seen)
        finally:
            event.remove(b.w.runtime, 'after_cursor_execute', after_sql)
    with b.w.runtime.connect() as conn:
        assert conn.scalar(text('SELECT last_activity_at FROM sessions WHERE id=:s'), {'s': b.w.actor.session_id}) == activity
    assert b.w.runtime.pool.checkedout() == 0


@pytest.mark.parametrize('path', ['/auth/session', '/admin/users'])
@pytest.mark.parametrize('kind', ['idle', 'absolute'])
def test_direct_http_final_predicate(browser, monkeypatch, path, kind):
    b = browser
    end = expiry(b, kind)
    real = callers.session_live
    def live(db, *a, **kw):
        result = real(db, *a, **kw)
        b.at = end
        return result
    monkeypatch.setattr(callers, 'session_live', live)
    denied(b.client.get(path))


@pytest.mark.parametrize('key', ['list', 'count', 'detail', 'collections', 'topics'])
def test_resource_http_final_predicate(resources, monkeypatch, key):
    b = resources
    end = expiry(b, 'idle')
    phase = slow_final(monkeypatch, b, resource_reads, end)
    denied(b.client.get(b.path(key)))
    assert phase.sql_done


@pytest.mark.parametrize('key', ['own_list', 'own_get', 'admin_list', 'admin_get', 'pending_list', 'pending_get', 'copy_list', 'copy_get', 'notice'])
def test_review_common_http_final_predicate(reads, monkeypatch, key):
    b = reads
    end = expiry(b, 'absolute')
    phase = slow_final(monkeypatch, b, review_publication_reads, end)
    denied(b.client.get(b.path(key)))
    assert phase.sql_done


@pytest.mark.parametrize('key', ['list', 'members', 'papers'])
def test_empty_resource_page_predicate_latency(resources, monkeypatch, key):
    b = resources
    end = expiry(b, 'idle')
    path = b.path(key) + '?offset=1000'
    baseline = b.client.get(path)
    assert baseline.status_code == 200
    assert baseline.json()['items'] == []
    phase = slow_final(monkeypatch, b, resource_reads, end)
    denied(b.client.get(path))
    assert phase.sql_done


@pytest.mark.parametrize('key', ['admin_list', 'pending_list', 'copy_list'])
def test_empty_review_common_page_predicate_latency(reads, monkeypatch, key):
    b = reads
    end = expiry(b, 'absolute')
    path = b.path(key) + '?offset=1000'
    baseline = b.client.get(path)
    assert baseline.status_code == 200
    assert baseline.json()['requests' if key == 'admin_list' else 'items'] == []
    phase = slow_final(monkeypatch, b, review_publication_reads, end)
    denied(b.client.get(path))
    assert phase.sql_done


@pytest.mark.parametrize('kind', ['idle', 'absolute'])
def test_outbox_post_known_audit_commit_final_predicate(browser, monkeypatch, kind):
    b = browser
    end = expiry(b, kind)
    committed = []
    real_commit, real_live = admin.commit_activity, callers.session_live
    def commit(db):
        tx = db.get_transaction()
        result = real_commit(db)
        committed.append(tx)
        return result
    def live(db, *a, **kw):
        result = real_live(db, *a, **kw)
        if committed:
            assert db.get_transaction() is not committed[0]
            b.at = end
        return result
    monkeypatch.setattr(admin, 'commit_activity', commit)
    monkeypatch.setattr(callers, 'session_live', live)
    denied(b.client.get('/admin/dev-outbox'))
    assert len(committed) == 1 and not committed[0].is_active and not b.reads
    with b.w.runtime.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM audit_events WHERE action='dev_outbox.read'")) == 1


@pytest.mark.parametrize('offset', [0, 1])
def test_outbox_link_ttl_after_snapshot_predicate(browser, monkeypatch, offset):
    b = browser
    # Keep session live for link expiry at one hour; do not change its activity in helper.
    from app.config import get_settings
    monkeypatch.setenv('RAS_AUTH__SESSION_IDLE', 'PT2H')
    get_settings.cache_clear()
    real_entries, real_live = b.outbox.entries, callers.session_live
    stage = SimpleNamespace(snapshot=False, sql=False)
    def entries(**kw):
        result = real_entries(**kw)
        stage.snapshot = True
        return result
    def live(db, *a, **kw):
        result = real_live(db, *a, **kw)
        if stage.snapshot:
            stage.sql = True
            b.at = NOW + timedelta(hours=1, microseconds=offset)
        return result
    monkeypatch.setattr(b.outbox, 'entries', entries)
    monkeypatch.setattr(callers, 'session_live', live)
    response = b.client.get('/admin/dev-outbox')
    assert stage.sql and response.status_code == 200
    assert response.json() == {'messages': []}


@pytest.mark.parametrize('key', ['item', 'create', 'rename', 'add'])
@pytest.mark.parametrize('kind', ['idle', 'absolute'])
def test_writer_response_final_predicate_after_known_commit(writes, monkeypatch, key, kind):
    b = writes
    end = expiry(b, kind)
    before, audit = business(b), audits(b)
    phase = slow_final(monkeypatch, b, resource_reads, end)
    denied(request(b, key))
    assert phase.sql_done
    changed(b, key, before)
    assert audits(b) == audit + (1 if key == 'item' else 0)


@pytest.mark.parametrize('change', ['revoke', 'disable', 'quarantine', 'held', 'generation', 'receipt_credential', 'receipt_status', 'head'])
def test_direct_integrity_bindings_still_closed(browser, change):
    b = browser
    mutate(b, change)
    code = 'service_recovering' if change in ('receipt_credential', 'receipt_status', 'head') else 'session_expired'
    with Session(b.w.runtime) as db, pytest.raises(ApiError, match=code):
        callers.final_caller(db, session_id=b.w.actor.session_id, user_id=b.w.uid,
                             clock=b.clock, idle=timedelta(minutes=30))


def test_direct_wrong_exact_user_binding(browser):
    b = browser
    other = accept(b.w, issue(b.w))
    with Session(b.w.runtime) as db, pytest.raises(ApiError, match='session_expired'):
        callers.final_caller(db, session_id=b.w.actor.session_id, user_id=other.id,
                             clock=b.clock, idle=timedelta(minutes=30))


@pytest.mark.parametrize('kind', ['idle', 'absolute'])
def test_sessions_listing_own_final_predicate_latency(browser, monkeypatch, kind):
    b = browser
    end = expiry(b, kind)
    real = callers.session_live
    def live(db, *a, **kw):
        result = real(db, *a, **kw)
        b.at = end
        return result
    monkeypatch.setattr(auth, 'session_live', live, raising=False)  # old route for pre-fix evidence
    monkeypatch.setattr(callers, 'session_live', live)
    denied(b.client.get('/auth/sessions'))
