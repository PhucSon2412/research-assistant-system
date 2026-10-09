"""Publish through real runtime PostgreSQL, MinIO, identity and journal.

Only the business cutover fixture is scoped. Replica writes are explicitly
labelled corruption/setup; revoke uses runtime, cleanup remains system code.
"""
import asyncio
import hashlib
import io
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import auth_safety, journal, publication, recovery
from app.api import common_publish as boundary, resource_writes
from app.api.errors import ApiError
from app.api.routes import publications, reviews
from app.config import get_settings
from app.db import get_engine
from app.identity.login import login
from app.identity.session_commit import commit_auth
from app.identity.sessions import revoke_session
from app.models.enums import RevocationReason, SessionRevokeReason
from app.storage import get_storage
from tests.resource_fixtures import BODY, SHA
from tests.test_auth_safety_foundation import NOW, SECRET
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world, issue, accept, PASSWORD  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, opened, wait_for  # noqa: F401
from tests.test_c2d5b_review_approval import Originals, approve, body as approval_body


class ObservedStorage:
    def __init__(self, raw):
        self.raw = raw
        self.opened, self.puts, self.deleted = [], [], []
        self.hook = None

    def open(self, key):
        self.opened.append(key)
        if self.hook:
            self.hook(key)
        return self.raw.open(key)

    def put(self, *args):
        self.puts.append(args[0])
        return self.raw.put(*args)

    def delete(self, key):
        self.deleted.append(key)
        return self.raw.delete(key)


def pending(b, data=BODY, *, scope=None):
    """Resource setup only: private file fixture; actual runtime approval/copy."""
    checksum = hashlib.sha256(data).hexdigest()
    if data != BODY:
        from tests.resource_fixtures import source
        b.own = source(b.w, b.user.id, 'LARGE-SYNTHETIC-ORIGINAL', data=data)
        b.body = b.body.model_copy(update=dict(item_id=b.own.item, version_id=b.own.version,
                                             source_file_id=b.own.file))
    b.approval_storage.objects[b.own.key] = data
    opened(b)
    data_ = approval_body(file_checksum_sha256=checksum)
    if scope is not None:
        data_['scope'] = scope
    r = approve(b, data_)
    assert r.status_code == 200, r.text
    item = uuid.UUID(r.json()['request']['pending_publication_id'])
    with b.w.engine.connect() as c:
        row = c.execute(text('SELECT f.id,f.version_id,f.storage_key,a.id FROM source_files f'
            ' JOIN access_rights a ON a.source_file_id=f.id WHERE f.item_id=:i'), {'i': item}).one()
    b.raw_storage.put(row[2], io.BytesIO(data), 'text/markdown')
    return SimpleNamespace(item=item, file=row[0], version=row[1], key=row[2], rights=row[3], checksum=checksum)


@pytest.fixture
def publishes(writers, monkeypatch):
    b = writers
    b.approval_storage = Originals(b.own)
    b.storage = b.approval_storage
    from app.main import app
    app.dependency_overrides[reviews._storage] = lambda: b.approval_storage
    settings = get_settings().model_copy(deep=True)
    settings.storage.bucket = 'ras-publish-' + uuid.uuid4().hex
    b.raw_storage = get_storage(settings)
    b.raw_storage.ensure_bucket()
    b.storage = ObservedStorage(b.raw_storage)
    app.dependency_overrides[publications._storage] = lambda: b.storage
    # approve() uses the route override above, while publish uses real MinIO.
    b.copy = pending(b)
    yield b
    assert boundary.slots.active == 0 and get_engine().pool.checkedout() == 0
    for key, _ in b.raw_storage.orphan_keys(''):
        b.raw_storage.delete(key)
    b.raw_storage.client.delete_bucket(Bucket=b.raw_storage.bucket)


def publish(b, *, item=None, data=None, who='admin', headers=None):
    issued = b.w.actor if who == 'admin' else b.owner_session
    b.client.cookies.set('ras_session', issued.token)
    b.client.cookies.set('ras_csrf', issued.csrf_token)
    h = {'Origin': 'http://web.test', 'X-CSRF-Token': issued.csrf_token} | (headers or {})
    return b.client.post(f'/admin/common-copies/{item or b.copy.item}/publish',
        json=data if data is not None else {'confirm_publish': True, 'file_checksum_sha256': b.copy.checksum},
        headers={k: v for k, v in h.items() if v is not None})


def replica(b, sql, **params):
    # Privileged corruption/setup, never a runtime grant or production writer.
    with b.w.engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text(sql), params)


def scalar(b, sql, **params):
    with b.w.engine.connect() as c:
        return c.scalar(text(sql), params)


def listed(b):
    return scalar(b, 'SELECT count(*) FROM common_publications')


def direct(b, *, stopped=None, outcome=None, checksum=None, item=None):
    return asyncio.run(boundary.attempt(None, b.admin, str(item or b.copy.item), checksum or b.copy.checksum,
        b.w.actor.csrf_token, b.clock, get_settings(), b.storage, stopped or threading.Event(), outcome))


def revoke(b, *, copy=None):
    c = copy or b.copy
    from tests.resource_fixtures import runtime_revoke
    return runtime_revoke(b.w, c.item, clock=b.clock)


def test_runtime_success_metadata_only_fresh_dto_no_provider_or_object_mutation(publishes, monkeypatch):
    b = publishes
    before = b.w.path.read_bytes()
    statements = []
    def sql(conn, cursor, statement, params, ctx, many):
        statements.append(statement)
    def storage(key):
        assert get_engine().pool.checkedout() == 0
        with b.w.runtime.connect() as c:
            assert c.scalar(text('SELECT current_user')) == 'c1_runtime'
    b.storage.hook = storage
    event.listen(get_engine(), 'before_cursor_execute', sql)
    try:
        r = publish(b)
    finally:
        event.remove(get_engine(), 'before_cursor_execute', sql)
    assert r.status_code == 200, r.text
    dto = r.json()['item']
    assert (dto['id'], dto['publication_state'], dto['content_status'], dto['fulltext_available']) == (
        str(b.copy.item), 'listed', 'metadata_only', False)
    assert dto['file']['checksum_sha256'] == SHA and dto['publication']['verified_byte_size'] == len(BODY)
    assert dto['rights']['scope'] == dict(audience='instance_users', original_download=True,
        fulltext_search=True, external_processing=False, conditions=None)
    assert b.storage.opened == [b.copy.key] and not b.storage.puts and not b.storage.deleted
    assert b.w.path.read_bytes() == before and listed(b) == 1
    for table in ('ingestion_jobs', 'chunks', 'sections', 'index_generations', 'embedding_calls'):
        assert scalar(b, f'SELECT count(*) FROM {table}') == 0
    assert b.copy.key not in r.text and b.own.key not in r.text and 'PRIVATE-SYNTHETIC' not in r.text
    write = next(i for i, s in enumerate(statements) if "publication_state='listed'" in s)
    exclusive = next(i for i, s in enumerate(statements[write:], write) if 'SELECT pg_advisory_xact_lock(' in s)
    assert any('INSERT INTO common_publications' in s for s in statements[write:exclusive])
    assert any('INSERT INTO audit_events' in s for s in statements[write:exclusive])
    assert any('FOR UPDATE' in s for s in statements[:write])
    assert any('pg_advisory_xact_lock_shared' in s for s in statements[exclusive:])
    assert publish(b).json() == {'error': 'already_published'}


@pytest.mark.parametrize('confirm', [False, None, 1, 'true', '1', 0])
def test_explicit_boolean_confirmation_only(publishes, confirm):
    b = publishes
    r = publish(b, data={'confirm_publish': confirm, 'file_checksum_sha256': SHA})
    assert r.status_code == 422 and listed(b) == 0 and b.storage.opened == []


@pytest.mark.parametrize('case', ['missing_confirm', 'checksum', 'missing', 'malformed', 'private',
                                  'owner', 'other_user', 'anonymous', 'csrf', 'origin'])
def test_input_acl_no_storage_or_mutation(publishes, case):
    b = publishes
    expected = 404
    kwargs = {}
    if case == 'missing_confirm':
        kwargs['data'] = {'file_checksum_sha256': SHA}; expected = 422
    elif case == 'checksum':
        kwargs['data'] = {'confirm_publish': True, 'file_checksum_sha256': 'a' * 64}; expected = 409
    elif case in ('missing', 'malformed', 'private'):
        kwargs['item'] = uuid.uuid4() if case == 'missing' else 'not-a-uuid' if case == 'malformed' else b.own.item
    elif case == 'owner':
        kwargs['who'] = 'owner'; expected = 403
    elif case == 'other_user':
        user = accept(b.w, issue(b.w, email='reader-b@example.test'))
        with Session(b.w.runtime) as db:
            other = login(db, email=user.email, password=PASSWORD, now=NOW,
                          settings=get_settings(), clock=b.clock)[1]
        b.owner_session = other; kwargs['who'] = 'owner'; expected = 403
    elif case == 'anonymous':
        b.client.cookies.clear()
        r = b.client.post(f'/admin/common-copies/{b.copy.item}/publish',
            json={'confirm_publish': True, 'file_checksum_sha256': SHA}, headers={'Origin': 'http://web.test'})
        assert r.status_code in (401, 403) and not b.storage.opened and listed(b) == 0
        return
    else:
        kwargs['headers'] = {'X-CSRF-Token': 'bad'} if case == 'csrf' else {'Origin': 'https://evil.test'}
        expected = 403
    r = publish(b, **kwargs)
    assert r.status_code == expected, r.text
    assert listed(b) == 0 and b.storage.opened == []


@pytest.mark.parametrize('case', ['epoch', 'checksum', 'file', 'version', 'rights_count', 'tombstone',
                                  'revoked', 'status', 'active_generation', 'origin', 'review'])
def test_resource_binding_corruption_refused(publishes, case):
    b = publishes
    i, f, a = b.copy.item, b.copy.file, b.copy.rights
    sqls = {
        'epoch': ('UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i', {}),
        'checksum': ("UPDATE source_files SET checksum_sha256=repeat('a',64) WHERE id=:f", {}),
        'file': ('UPDATE source_files SET item_id=:other WHERE id=:f', {'other': b.own.item}),
        'version': ('UPDATE source_files SET version_id=:other WHERE id=:f', {'other': b.own.version}),
        'rights_count': ('INSERT INTO access_rights SELECT (jsonb_populate_record(NULL::access_rights,'
            " to_jsonb(a)||jsonb_build_object('id',gen_random_uuid(),'origin_review_request_id',gen_random_uuid()))).*"
            ' FROM access_rights a WHERE id=:a', {}),
        'status': ("UPDATE library_items SET content_status='indexed',active_version_id=:v WHERE id=:i", {}),
        'active_generation': ('UPDATE library_items SET active_index_generation_id=gen_random_uuid() WHERE id=:i', {}),
        'origin': ("UPDATE source_files SET file_source='upload' WHERE id=:f", {}),
        'review': ("UPDATE review_requests SET status='closed_by_recovery',closed_by_user_id=NULL WHERE id=:r", {}),
    }
    if case in ('revoked', 'tombstone'):
        revoke(b)
        if case == 'tombstone':
            replica(b, "UPDATE library_items SET publication_state='pending' WHERE id=:i", i=i)
        expected = 'common_copy_revoked'
    else:
        sql, params = sqls[case]
        replica(b, sql, i=i, f=f, a=a, r=b.rid, v=b.copy.version, **params)
        expected = 'rights_not_publishable'
    r = publish(b)
    assert (r.status_code, r.json()) == (409, {'error': expected})
    assert not b.storage.opened and listed(b) == 0


@pytest.mark.parametrize('flags', [dict(download=False, search=False, provider=False, conditions=None),
                                  dict(download=True, search=True, provider=False, conditions='attribution'),
                                  dict(download=False, search=True, provider=True, conditions=None)])
def test_metadata_publication_does_not_require_or_widen_content_permissions(publishes, flags):
    b = publishes
    c = pending(b, BODY, scope=dict(audience='instance_users', original_download=flags['download'],
        fulltext_search=flags['search'], external_processing=flags['provider'], conditions=flags['conditions']))
    b.copy = c
    r = publish(b)
    assert r.status_code == 200, r.text
    out = r.json()['item']
    assert out['rights']['scope'] == dict(audience='instance_users', original_download=flags['download'],
        fulltext_search=flags['search'], external_processing=flags['provider'], conditions=flags['conditions'])
    assert out['index']['search_open'] is False and out['index']['rag_open'] is False
    assert out['original_download'] == ('not_permitted' if not flags['download'] else 'blocked_by_conditions')


def change(b, case):
    if case == 'logout':
        with Session(b.w.runtime) as db:
            revoke_session(db, session_id=b.w.actor.session_id, user_id=b.w.uid,
                           reason=SessionRevokeReason.LOGOUT, now=NOW)
            commit_auth(db)
    elif case == 'hold':
        recovery.hold(b.w.engine, get_settings(), NOW)
    elif case == 'disable':
        from app.identity.predicates import lock_users
        from app.identity.session_commit import AUTH_PENDING
        with Session(b.w.runtime) as db:
            lock_users(db, [b.w.uid])
            db.info[AUTH_PENDING] = [auth_safety.Change(b.w.uid, 'status', 'admin_disable', NOW, decision='disabled')]
            commit_auth(db)
    elif case == 'reset':
        from app.identity.password_reset import request_reset, confirm_reset
        with Session(b.w.runtime) as db:
            token = request_reset(db, email='admin@example.test', now=NOW,
                lifetime=get_settings().auth.password_reset, clock=b.clock)
        assert token is not None
        with Session(b.w.runtime) as db:
            confirm_reset(db, raw_token=token.token, new_password='New-test-password-789!', now=NOW,
                          policy=get_settings().password, clock=b.clock)
    else:
        from tests.test_c2d5b_review_approval import change_actor
        change_actor(b, case)


@pytest.mark.parametrize('phase', ['dependency', 'hash', 'response'])
@pytest.mark.parametrize('case', ['role', 'revoke', 'logout', 'disable', 'reset', 'csrf', 'hold',
                                  'held', 'quarantine', 'binding', 'generation', 'receipt', 'head'])
def test_current_actor_head_receipt_csrf_and_gate_at_every_boundary(publishes, monkeypatch, phase, case):
    b = publishes
    if phase == 'dependency':
        b.after_dependency = lambda db, ctx: change(b, case)
    elif phase == 'hash':
        b.storage.hook = lambda key: change(b, case)
    else:
        real = boundary.respond
        def respond(*a, **kw):
            change(b, case)
            return real(*a, **kw)
        monkeypatch.setattr(boundary, 'respond', respond)
    r = publish(b)
    expected = 503 if case == 'hold' else 403 if case in ('role', 'csrf') else 401
    assert r.status_code == expected, r.text
    assert 'item' not in r.json() and listed(b) == (phase == 'response')
    if r.status_code == 401:
        assert 'Max-Age=0' in r.headers.get('set-cookie', '')
    else:
        assert 'set-cookie' not in r.headers
    assert b.storage.puts == b.storage.deleted == []


@pytest.mark.parametrize('phase', ['proof', 'accept', 'respond'])
@pytest.mark.parametrize('kind', ['idle', 'absolute'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_ttl_sampled_after_last_resource_and_predicate_sql(publishes, monkeypatch, phase, kind, offset):
    b = publishes
    if kind == 'absolute':
        expires = NOW + timedelta(minutes=3)
        replica(b, 'UPDATE sessions SET absolute_expires_at=:at WHERE id=:s', at=expires, s=b.w.actor.session_id)
    else:
        expires = NOW + get_settings().auth.session_idle
    mode, armed = {'phase': None}, {'value': False}
    for name in ('proof', 'accept', 'respond'):
        real = getattr(boundary, name)
        def wrap(*a, _name=name, _real=real, **kw):
            mode['phase'] = _name
            armed['value'] = False
            return _real(*a, **kw)
        monkeypatch.setattr(boundary, name, wrap)
    real_target, real_dto, real_live = boundary._target, publications._copy_out, resource_writes.session_live
    def target(*a, **kw):
        result = real_target(*a, **kw)
        if mode['phase'] in ('proof', 'accept'):
            armed['value'] = True
        return result
    def dto(*a, **kw):
        result = real_dto(*a, **kw)
        armed['value'] = True
        return result
    def live(*a, **kw):
        result = real_live(*a, **kw)
        if mode['phase'] == phase and armed['value']:
            b.at = expires + timedelta(seconds=offset)
        return result
    monkeypatch.setattr(boundary, '_target', target)
    monkeypatch.setattr(publications, '_copy_out', dto)
    monkeypatch.setattr(resource_writes, 'session_live', live)
    r = publish(b)
    assert r.status_code == (200 if offset < 0 else 401), r.text
    assert listed(b) == (offset < 0 or phase == 'respond')
    if offset >= 0:
        assert 'item' not in r.json() and 'Max-Age=0' in r.headers['set-cookie']


@pytest.mark.parametrize('case', ['missing', 'truncated', 'large', 'same_size', 'open', 'read', 'close'])
def test_storage_integrity_stream_and_close_failures_do_not_publish(publishes, monkeypatch, case):
    b = publishes
    if case == 'missing':
        b.raw_storage.delete(b.copy.key)
    elif case in ('truncated', 'large', 'same_size'):
        data = BODY[:-1] if case == 'truncated' else BODY + b'x' if case == 'large' else b'x' * len(BODY)
        b.raw_storage.put(b.copy.key, io.BytesIO(data), 'text/markdown')
    else:
        real_open = b.storage.open
        def opened(key):
            if case == 'open':
                raise OSError('injected internal storage failure')
            body = real_open(key)
            class Fault:
                def iter_chunks(self, **kw):
                    if case == 'read':
                        raise OSError('injected read failure')
                    yield from body.iter_chunks(**kw)
                def close(self):
                    body.close()
                    if case == 'close':
                        raise OSError('injected close failure')
            return Fault()
        monkeypatch.setattr(b.storage, 'open', opened)
    r = publish(b)
    assert (r.status_code, r.json()) == ((409, {'error': 'common_file_integrity_failed'}) if case in (
        'missing', 'truncated', 'large', 'same_size') else (503, {'error': 'publication_storage_unavailable'}))
    assert listed(b) == 0 and b.storage.puts == b.storage.deleted == []


@pytest.mark.parametrize('failure', ['missing', 'same_size'])
@pytest.mark.parametrize('case', ['role', 'logout', 'reset', 'csrf', 'hold', 'expiry', 'common_revoke'])
def test_storage_denial_requires_fresh_caller_gate_and_resource_proof(publishes, failure, case):
    b = publishes
    def changed(key):
        if case == 'expiry':
            b.at = NOW + get_settings().auth.session_idle
        elif case == 'common_revoke':
            revoke(b)
        else:
            change(b, case)
        if failure == 'missing':
            b.raw_storage.delete(key)  # labelled storage corruption adversary
        else:
            b.raw_storage.put(key, io.BytesIO(b'x' * len(BODY)), 'text/markdown')
    b.storage.hook = changed
    r = publish(b)
    expected = 503 if case == 'hold' else 409 if case == 'common_revoke' else (
        403 if case in ('role', 'csrf') else 401)
    assert r.status_code == expected and r.json().get('error') != 'common_file_integrity_failed', r.text
    if case == 'common_revoke':
        assert r.json() == {'error': 'common_copy_revoked'}
    if expected == 401:
        assert 'Max-Age=0' in r.headers.get('set-cookie', '')
    assert listed(b) == 0 and 'item' not in r.json() and b.storage.puts == b.storage.deleted == []


def test_30_mib_real_minio_hash_bounded_chunks_and_memory(publishes, monkeypatch):
    import tracemalloc
    b = publishes
    block = b'synthetic PDF body block!\n'
    large = block * (30 * 1024 * 1024 // len(block) + 1)
    large = large[:30 * 1024 * 1024]
    b.copy = pending(b, large)
    del large
    tracemalloc.start()
    try:
        r = publish(b)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.status_code == 200, r.text
    assert r.json()['item']['publication']['verified_byte_size'] == 30 * 1024 * 1024
    assert peak < 12 * 1024 * 1024 and b.storage.opened == [b.copy.key]


@pytest.mark.parametrize('case', ['key', 'size', 'checksum', 'epoch', 'rights', 'flags'])
def test_frozen_binding_change_during_hash_refuses_old_verified_bytes(publishes, case):
    b = publishes
    def change(key):
        sql = {
            'key': "UPDATE source_files SET storage_key=storage_key||'-changed' WHERE id=:f",
            'size': 'UPDATE source_files SET byte_size=byte_size+1 WHERE id=:f',
            'checksum': "UPDATE source_files SET checksum_sha256=repeat('a',64) WHERE id=:f",
            'epoch': 'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',
            'rights': 'UPDATE access_rights SET version_id=:v WHERE id=:a',
            'flags': 'UPDATE access_rights SET permits_original_download=false WHERE id=:a',
        }[case]
        replica(b, sql, f=b.copy.file, i=b.copy.item, a=b.copy.rights, v=b.own.version)
    b.storage.hook = change
    r = publish(b)
    assert (r.status_code, r.json()) == (409, {'error': 'rights_not_publishable'})
    assert listed(b) == 0 and b.storage.opened == [b.copy.key]


@pytest.mark.parametrize('adversary', ['revoke', 'auth'])
def test_hash_has_no_connection_or_db_locks_and_writers_commit(publishes, adversary):
    b = publishes
    entered, release = threading.Event(), threading.Event()
    def hold(key):
        assert get_engine().pool.checkedout() == 0
        entered.set()
        assert release.wait(20)
    b.storage.hook = hold
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(publish, b)
        try:
            assert entered.wait(15)
            if adversary == 'revoke':
                revoke(b)
            else:
                change(b, 'revoke')
            assert get_engine().pool.checkedout() == 0
        finally:
            release.set()
        r = future.result(30)
    assert (r.status_code, r.json()) == ((409, {'error': 'common_copy_revoked'}) if adversary == 'revoke'
                                       else (401, {'error': 'session_expired'}))
    assert listed(b) == 0 and b.storage.puts == b.storage.deleted == []


@pytest.mark.parametrize('phase', ['proof', 'malformed', 'denial', 'accept', 'response'])
@pytest.mark.parametrize('fault', ['rollback', 'close', 'both', 'both_before_release'])
def test_teardown_faults_before_and_after_release_never_leak_or_return_dto(publishes, monkeypatch, phase, fault):
    from tests.test_c2d5b_review_approval import teardown_session
    b = publishes
    nth = 1 if phase in ('proof', 'malformed', 'denial') else 2 if phase == 'accept' else 3
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=nth))
    kwargs = {}
    if phase == 'malformed':
        kwargs['item'] = 'not-a-uuid'
    elif phase == 'denial':
        kwargs['data'] = {'confirm_publish': True, 'file_checksum_sha256': 'a' * 64}
    elif phase == 'accept':
        def fail(*a, **kw):
            raise ApiError(409, 'rights_not_publishable')
        monkeypatch.setattr(boundary, '_binding', fail)
    r = publish(b, **kwargs)
    assert r.status_code == (409 if phase in ('denial', 'accept') else 503), r.text
    assert r.json() == {'error': 'checksum_confirmation_mismatch' if phase == 'denial' else
        'rights_not_publishable' if phase == 'accept' else 'publication_response_unavailable' if phase == 'response'
        else 'publication_unavailable'}
    assert listed(b) == (phase == 'response')
    if phase in ('proof', 'malformed', 'denial'):
        assert b.storage.opened == []
    assert b.raw_storage.size(b.copy.key) == len(BODY)
    assert get_engine().pool.checkedout() == 0 and b.storage.puts == b.storage.deleted == []


@pytest.mark.parametrize('landed', [False, True])
@pytest.mark.parametrize('fault', ['none', 'rollback', 'close', 'both'])
@pytest.mark.parametrize('error', ['runtime', 'api'])
def test_commit_exception_preserves_independent_uncertain_outcome_without_select_or_retry(
        publishes, monkeypatch, landed, fault, error):
    b = publishes
    commits, responses = [], []
    class Lost(Session):
        def commit(self):
            commits.append(1)
            if landed:
                super().commit()
            else:
                super().rollback()
            self.started = True
            raise ApiError(401, 'session_expired') if error == 'api' else RuntimeError('lost commit reply')
        def rollback(self):
            if getattr(self, 'started', False) and fault in ('rollback', 'both'):
                raise RuntimeError('rollback before release')
            return super().rollback()
        def close(self):
            if getattr(self, 'started', False) and fault in ('close', 'both'):
                raise RuntimeError('close before release')
            return super().close()
    monkeypatch.setattr(boundary, 'Session', Lost)
    monkeypatch.setattr(boundary, 'respond', lambda *a, **kw: responses.append(1))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError) as caught:
        direct(b, outcome=outcome)
    assert (caught.value.status_code, caught.value.code) == (503, 'publication_outcome_uncertain')
    assert outcome.commit_started and not outcome.committed and not outcome.rollback_confirmed
    assert len(commits) == 1 and responses == [] and listed(b) == int(landed)
    assert b.raw_storage.size(b.copy.key) == len(BODY) and b.storage.deleted == b.storage.puts == []
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('fault', ['response', 'rollback', 'close', 'both'])
def test_known_commit_with_response_or_teardown_failure_retains_publication(publishes, monkeypatch, fault):
    b = publishes
    if fault == 'response':
        def fail(*a, **kw):
            raise RuntimeError('injected response fault')
        monkeypatch.setattr(boundary, 'respond', fail)
    else:
        class Teardown(Session):
            def commit(self):
                super().commit()
                self.done = True
            def rollback(self):
                if getattr(self, 'done', False) and fault in ('rollback', 'both'):
                    raise RuntimeError('rollback before release')
                return super().rollback()
            def close(self):
                if getattr(self, 'done', False) and fault in ('close', 'both'):
                    raise RuntimeError('close before release')
                return super().close()
        monkeypatch.setattr(boundary, 'Session', Teardown)
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='publication_response_unavailable'):
        direct(b, outcome=outcome)
    assert outcome.commit_started and outcome.committed and not outcome.rollback_confirmed
    assert listed(b) == 1 and b.raw_storage.size(b.copy.key) == len(BODY)
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('case', ['deadline', 'attempt_cancel', 'receive_cancel', 'disconnect'])
def test_deadline_and_cancel_keep_slot_until_storage_thread_settles_no_acceptance(publishes, monkeypatch, case):
    b = publishes
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def blocking(key):
        assert get_engine().pool.checkedout() == 0
        entered.set()
        assert release.wait(15)
    b.storage.hook = blocking
    real_open = b.storage.open
    def opened(key):
        stream = real_open(key)
        real_close = stream.close
        def close():
            real_close(); closed.set()
        stream.close = close
        return stream
    monkeypatch.setattr(b.storage, 'open', opened)
    if case == 'deadline':
        monkeypatch.setattr(get_settings().files, 'publication_verify_deadline_seconds', .05)
    class Request:
        headers = {'x-csrf-token': b.w.actor.csrf_token}
        async def is_disconnected(self):
            return case == 'disconnect' and entered.is_set()
    async def run():
        stopped = threading.Event()
        if case == 'receive_cancel':
            task = asyncio.create_task(boundary.receive(Request(), b.admin, str(b.copy.item), SHA,
                b.clock, get_settings(), b.storage))
        else:
            task = asyncio.create_task(boundary.attempt(Request(), b.admin, str(b.copy.item), SHA,
                b.w.actor.csrf_token, b.clock, get_settings(), b.storage, stopped))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            if 'cancel' in case:
                task.cancel()
            await asyncio.sleep(.1)
            assert boundary.slots.active == 1 and not closed.is_set() and listed(b) == 0
        finally:
            release.set()
        if 'cancel' in case:
            with pytest.raises(asyncio.CancelledError):
                await task
            for pending_ in list(boundary._attempts):
                await asyncio.gather(pending_, return_exceptions=True)
        else:
            with pytest.raises(ApiError) as caught:
                await task
            assert caught.value.code == ('publication_verify_timeout' if case == 'deadline' else 'publication_unavailable')
    asyncio.run(run())
    assert closed.is_set() and listed(b) == 0 and boundary.slots.active == 0
    assert b.raw_storage.size(b.copy.key) == len(BODY)


def test_slot_cap_no_additional_storage_read_while_busy(publishes, monkeypatch):
    b = publishes
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(get_settings().files, 'publication_verify_max_concurrent', 1)
    def hold(key):
        entered.set(); assert release.wait(15)
    b.storage.hook = hold
    with ThreadPoolExecutor(1) as pool:
        f = pool.submit(direct, b)
        try:
            assert entered.wait(10)
            with pytest.raises(ApiError, match='publication_busy'):
                direct(b)
            assert boundary.slots.active == 1 and b.storage.opened == [b.copy.key]
        finally:
            release.set()
        assert f.result(20)['item']['publication_state'] == 'listed'


@pytest.mark.parametrize('kind', ['same_item', 'duplicate_checksum'])
def test_concurrent_publish_unique_winner_with_runtime_admins(publishes, monkeypatch, kind):
    from app.authz import Principal
    b = publishes
    other = accept(b.w, issue(b.w, email='second-admin@example.test'))
    # Test-only role assignment; account/receipts/login/session are runtime.
    replica(b, "UPDATE users SET role='admin' WHERE id=:u", u=other.id)
    with Session(b.w.runtime) as db:
        actor = login(db, email=other.email, password=PASSWORD, now=NOW,
                      settings=get_settings(), clock=b.clock)[1]
    principal = Principal(other.id, 'admin', actor.session_id, other.email, None)
    second = b.copy if kind == 'same_item' else pending(b)
    barrier = threading.Barrier(2)
    b.storage.hook = lambda key: barrier.wait(15)
    if kind == 'duplicate_checksum':
        # Both prechecks can miss a concurrent publication. Exercise the REAL
        # partial unique index, not a fabricated duplicate response.
        monkeypatch.setattr(boundary, 'live_duplicate', lambda *a: None)
    def run(p, c, csrf):
        try:
            return asyncio.run(boundary.attempt(None, p, str(c.item), c.checksum, csrf,
                b.clock, get_settings(), b.storage, threading.Event()))['item']['publication_state']
        except ApiError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda args: run(*args), [(b.admin, b.copy, b.w.actor.csrf_token),
                                                        (principal, second, actor.csrf_token)]))
    assert sorted(results) == sorted(['listed', 'already_published' if kind == 'same_item' else
                                     'duplicate_published_checksum'])
    assert listed(b) == 1 and get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('first', ['revoke', 'publish'])
def test_publish_revoke_real_lock_wait_both_orders_and_current_history_response(publishes, monkeypatch, first):
    b = publishes
    entered, release = threading.Event(), threading.Event()
    if first == 'revoke':
        real = publication._cancel_jobs
        def hold(*a, **kw):
            entered.set(); assert release.wait(15)
            return real(*a, **kw)
        monkeypatch.setattr(publication, '_cancel_jobs', hold)
    else:
        real = boundary._binding
        def hold(*a, **kw):
            entered.set(); assert release.wait(15)
            return real(*a, **kw)
        monkeypatch.setattr(boundary, '_binding', hold)
    with ThreadPoolExecutor(2) as pool:
        f = pool.submit(revoke if first == 'revoke' else direct, b)
        try:
            assert entered.wait(10)
            g = pool.submit(direct if first == 'revoke' else revoke, b)
            assert wait_for(b.w.engine, 'transactionid')
        finally:
            release.set()
        if first == 'revoke':
            f.result(20)
            with pytest.raises(ApiError, match='common_copy_revoked'):
                g.result(20)
            assert listed(b) == 0
        else:
            dto = f.result(20)['item']
            g.result(20)
            assert dto['publication_state'] in ('listed', 'revoked') and listed(b) == 1
    assert scalar(b, "SELECT publication_state FROM library_items WHERE id=:i", i=b.copy.item) == 'revoked'
    assert scalar(b, 'SELECT count(*) FROM common_publications WHERE revoked_at IS NULL') == 0


@pytest.mark.parametrize('cleanup', [False, True])
def test_response_after_revoke_cleanup_uses_new_history_proof_no_stale_dto(publishes, monkeypatch, cleanup):
    b = publishes
    real = boundary.respond
    def respond(*a, **kw):
        revoke(b)
        if cleanup:
            publication.cleanup_revoked(b.w.engine, b.raw_storage, NOW + timedelta(days=2), 10)
        return real(*a, **kw)
    monkeypatch.setattr(boundary, 'respond', respond)
    r = publish(b)
    assert r.status_code == 200, r.text
    out = r.json()['item']
    assert out['publication_state'] == 'revoked' and out['published'] is False
    assert out['publication']['revoked_at'] is not None and out['original_download'] == 'revoked'
    if cleanup:
        assert out['title'] is None and out['file']['filename'] is None and out['rights']['evidence_note'] is None
    # Ordinary routes still reject; history permission never grants content.
    assert b.client.get(f'/library/items/{b.copy.item}').status_code == 404
    assert b.client.get(f'/files/{b.copy.file}/original').status_code == 404


@pytest.mark.parametrize('snapshot', ['pending', 'listed'])
def test_snapshot_restore_with_current_journal_never_replays_missing_positive_publication(
        publishes, admin_engine, app_env, tmp_path, monkeypatch, snapshot):
    from tests.test_recovery import backup
    from tests.conftest import upgrade
    b = publishes
    if snapshot == 'listed':
        assert publish(b).status_code == 200
    with backup(SimpleNamespace(engine=b.w.engine), admin_engine) as snap:
        if snapshot == 'pending':
            assert publish(b).status_code == 200
        revoke(b)  # C2d-5c2 runtime Admin/session/journal boundary
        current = tmp_path / 'current-journal.jsonl'
        current.write_bytes(b.w.path.read_bytes())
        monkeypatch.setenv('RAS_RECOVERY__JOURNAL_PATH', str(current))
        get_settings.cache_clear()
        settings = get_settings()
        recovery.hold(snap.engine, settings, NOW)
        upgrade(snap.url)
        recovery.recover(snap.engine, b.raw_storage, settings, NOW)
        assert recovery.evaluate_integrity(settings, snap.engine).open
        with snap.engine.connect() as c:
            assert c.scalar(text('SELECT publication_state FROM library_items WHERE id=:i'),
                            {'i': b.copy.item}) == 'revoked'
            assert c.scalar(text('SELECT count(*) FROM common_publications')) == int(snapshot == 'listed')
            assert c.scalar(text('SELECT checksum_sha256 FROM source_files WHERE id=:f'), {'f': b.own.file}) == SHA
        app_env(snap.url.set(username=b.w.runtime.url.username, password=b.w.runtime.url.password))
        b.client.cookies.set('ras_session', b.w.actor.token)
        assert b.client.get(f'/library/items/{b.copy.item}').status_code == 404
        assert b.client.get(f'/files/{b.copy.file}/original').status_code == 404
        assert b.copy.item.hex not in b.client.get('/library/items?scope=common').text.replace('-', '')
        report = recovery.recover(snap.engine, b.raw_storage, settings, NOW)
        assert report.reconciliation_complete and not report.safety_ready
    assert b.approval_storage.objects[b.own.key] == BODY


def test_pending_http_real_gate_without_override_no_storage_or_publish(installation, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    calls = []
    class Never:
        def open(self, key):
            calls.append(key); raise AssertionError('pending must never read storage')
    app.dependency_overrides[publications._storage] = lambda: Never()
    try:
        before = installation.path.read_bytes()
        with TestClient(app) as client:
            r = client.post(f'/admin/common-copies/{uuid.uuid4()}/publish',
                json={'confirm_publish': True, 'file_checksum_sha256': SHA},
                headers={'Origin': 'http://web.test'})
            assert r.status_code == 503 and r.json() == {'error': 'service_recovering'}
            assert client.get('/healthz').status_code == 200
            assert client.get('/readyz').status_code == 503
        assert not calls and installation.path.read_bytes() == before
        with installation.runtime.connect() as c:
            assert c.scalar(text('SELECT count(*) FROM common_publications')) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize('field', ['publication_verify_max_concurrent', 'publication_verify_deadline_seconds'])
@pytest.mark.parametrize('value', [0, -1, float('inf'), 'bad'])
def test_verification_config_limits_fail_closed(field, value):
    from pydantic import ValidationError
    from app.config import FileLimits
    with pytest.raises(ValidationError):
        FileLimits(**{field: value})


def test_verification_config_defaults_and_upper_bounds():
    from pydantic import ValidationError
    from app.config import FileLimits
    assert FileLimits().publication_verify_max_concurrent == 2
    assert FileLimits().publication_verify_deadline_seconds == 90
    with pytest.raises(ValidationError):
        FileLimits(publication_verify_max_concurrent=65)
    with pytest.raises(ValidationError):
        FileLimits(publication_verify_deadline_seconds=601)


@pytest.mark.parametrize('lock', ['user', 'item', 'journal'])
@pytest.mark.parametrize('expired', [False, True])
def test_acceptance_lock_wait_uses_current_clock_not_proof_time(publishes, monkeypatch, lock, expired):
    b = publishes
    entered, release = threading.Event(), threading.Event()
    real = boundary.accept
    def accept_(*a, **kw):
        entered.set(); assert release.wait(15)
        return real(*a, **kw)
    monkeypatch.setattr(boundary, 'accept', accept_)
    with ThreadPoolExecutor(1) as pool:
        f = pool.submit(direct, b)
        try:
            assert entered.wait(10)
            with b.w.engine.connect() as c:
                tx = c.begin()
                if lock == 'user':
                    c.execute(text('SELECT id FROM users WHERE id=:i FOR NO KEY UPDATE'), {'i': b.w.uid})
                elif lock == 'item':
                    c.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'), {'i': b.copy.item})
                else:
                    c.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': recovery.JOURNAL_LOCK})
                release.set()
                assert wait_for(b.w.engine, 'advisory' if lock == 'journal' else 'transactionid')
                b.at = NOW + get_settings().auth.session_idle + timedelta(seconds=0 if expired else -1)
                tx.commit()
            if expired:
                with pytest.raises(ApiError, match='session_expired'):
                    f.result(20)
            else:
                assert f.result(20)['item']['publication_state'] == 'listed'
        finally:
            release.set()
    assert listed(b) == int(not expired)


def test_storage_close_before_release_fault_is_controlled_and_releases_socket(publishes, monkeypatch):
    b = publishes
    real = b.storage.open
    released, calls = [], []
    def opened(key):
        stream = real(key)
        original = stream.close
        def close():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('injected close before release')
            original(); released.append(1)
        stream.close = close
        return stream
    monkeypatch.setattr(b.storage, 'open', opened)
    r = publish(b)
    assert (r.status_code, r.json()) == (503, {'error': 'publication_storage_unavailable'})
    assert calls == [1, 1] and released == [1] and listed(b) == 0


def test_audit_denial_failure_keeps_original_business_denial(publishes, monkeypatch):
    b = publishes
    def fail(*a, **kw):
        raise RuntimeError('injected audit failure')
    monkeypatch.setattr(boundary, 'record_event', fail)
    r = publish(b, data={'confirm_publish': True, 'file_checksum_sha256': 'a' * 64})
    assert (r.status_code, r.json()) == (409, {'error': 'checksum_confirmation_mismatch'})
    assert listed(b) == 0 and b.storage.opened == []


def test_stop_after_final_binding_before_commit_rolls_back_without_mutation(publishes, monkeypatch):
    b = publishes
    stopped, outcome = threading.Event(), boundary.Outcome()
    real = boundary._binding
    def stop(*a, **kw):
        result = real(*a, **kw)
        stopped.set()
        return result
    monkeypatch.setattr(boundary, '_binding', stop)
    with pytest.raises(ApiError, match='publication_unavailable'):
        direct(b, stopped=stopped, outcome=outcome)
    assert not outcome.commit_started and outcome.rollback_confirmed and listed(b) == 0
    assert b.raw_storage.size(b.copy.key) == len(BODY)


def test_overwrite_after_last_hash_exposes_documented_storage_gap(publishes, monkeypatch):
    """An adversarial overwrite after verification cannot be detected by DB proof."""
    b = publishes
    real = boundary.accept
    def overwrite(*a, **kw):
        assert get_engine().pool.checkedout() == 0
        b.raw_storage.put(b.copy.key, io.BytesIO(b'x' * len(BODY)), 'text/markdown')
        return real(*a, **kw)
    monkeypatch.setattr(boundary, 'accept', overwrite)
    assert publish(b).status_code == 200 and listed(b) == 1
    stream = b.raw_storage.open(b.copy.key)
    try:
        assert hashlib.sha256(stream.read()).hexdigest() != SHA
    finally:
        stream.close()
    assert b.storage.opened == [b.copy.key] and b.storage.puts == b.storage.deleted == []
    from app.main import app
    from app.api.routes import uploads
    app.dependency_overrides[uploads._storage] = lambda: b.raw_storage
    response = b.client.get(f'/files/{b.copy.file}/original')
    assert (response.status_code, response.json()) == (409, {'error': 'common_file_integrity_failed'})
