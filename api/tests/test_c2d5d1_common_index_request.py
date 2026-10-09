"""One runtime index-request POST: real PG/roles/journal, MinIO originals via runtime publish.

Only business cutover is scoped. Owner-engine writes below are labelled
setup/corruption or legacy worker simulation, never the mutation under test.
The endpoint queues a durable job only: no storage, broker or provider.
"""
import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import journal, publication, recovery
from app.api import common_index_request as boundary, resource_writes
from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_engine
from app.ingest import worker
from app.storage import get_storage
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, wait_for  # noqa: F401
from tests.test_c2d5b_review_approval import Originals
from tests.test_c2d5c1_common_publish import ObservedStorage, pending, publish, replica, scalar, change
from tests.resource_fixtures import BODY, SHA, runtime_revoke

INDEXABLE = dict(audience='instance_users', original_download=True, fulltext_search=True,
                 external_processing=True, conditions=None)


@pytest.fixture
def indexes(writers, monkeypatch):
    from app.api.routes import publications, reviews
    from app.main import app
    b = writers
    b.approval_storage = Originals(b.own)
    app.dependency_overrides[reviews._storage] = lambda: b.approval_storage
    settings = get_settings().model_copy(deep=True)
    settings.storage.bucket = 'ras-index-' + uuid.uuid4().hex
    b.raw_storage = get_storage(settings)
    b.raw_storage.ensure_bucket()
    b.storage = ObservedStorage(b.raw_storage)
    app.dependency_overrides[publications._storage] = lambda: b.storage
    b.copy = pending(b, scope=INDEXABLE)
    assert publish(b).status_code == 200  # runtime publish; hash read happens here only
    b.opened = list(b.storage.opened)
    # No broker in this endpoint: any Celery dispatch fails the test.
    import celery
    from celery.app.task import Task
    def no_broker(*a, **kw): raise AssertionError('broker dispatch forbidden')
    monkeypatch.setattr(Task, 'apply_async', no_broker)
    monkeypatch.setattr(celery.Celery, 'send_task', no_broker)
    yield b
    assert get_engine().pool.checkedout() == 0 and not boundary._attempts
    for key, _ in b.raw_storage.orphan_keys(''):
        b.raw_storage.delete(key)
    b.raw_storage.client.delete_bucket(Bucket=b.raw_storage.bucket)


def index(b, *, item=None, data=None, who='admin', headers=None, cookies=True):
    issued = b.w.actor if who == 'admin' else b.owner_session
    b.client.cookies.clear()
    b.client.cookies.set('ras_csrf', issued.csrf_token)
    if cookies:
        b.client.cookies.set('ras_session', issued.token)
    h = {'Origin': 'http://web.test', 'X-CSRF-Token': issued.csrf_token} | (headers or {})
    return b.client.post(f'/admin/common-copies/{item or b.copy.item}/index',
        json=data if data is not None else {'confirm_external_processing': True},
        headers={k: v for k, v in h.items() if v is not None})


def direct(b, *, outcome=None, stopped=None, item=None):
    return asyncio.run(boundary.attempt(None, b.admin, str(item or b.copy.item), b.w.actor.csrf_token,
        b.clock, get_settings(), stopped or threading.Event(), outcome))


def blocked(engine):
    """Some backend waits on a row lock or the journal advisory lock (observed, not slept)."""
    import time
    for _ in range(150):
        with engine.connect() as c:
            if c.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
                             " AND wait_event IN ('transactionid','tuple','advisory')")):
                return True
        time.sleep(.03)
    return False


def jobs(b, item=None):
    with b.w.engine.connect() as c:
        return c.execute(text('SELECT * FROM ingestion_jobs WHERE item_id=:i ORDER BY created_at,id'),
                         {'i': item or b.copy.item}).mappings().all()


def audits(b, outcome='success'):
    with b.w.engine.connect() as c:
        return c.execute(text("SELECT actor_user_id,target_id,details FROM audit_events"
            " WHERE action='common_copy.index.start' AND outcome=:o ORDER BY id"), {'o': outcome}).all()


def live_job(b, **over):
    """Labelled setup: a live promotion job as the legacy worker/older API left it."""
    values = dict(state='received', actor=None, epoch=0, cancel=False, lease=None, attempts=0)
    values |= over
    with b.w.engine.begin() as c:
        return c.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,source_file_id,"
            "rights_epoch_at_start,idempotency_key,state,actor_user_id,cancel_requested,cancel_requested_at,"
            "lease_token,lease_until,attempt_count,created_at,updated_at) VALUES('promotion','common',:i,:v,:f,:e,:k,"
            ":s,:a,:c,CASE WHEN :c THEN CAST(:at AS timestamptz) END,:lt,:lu,:n,:at,:at) RETURNING id"),
            dict(i=b.copy.item, v=b.copy.version, f=b.copy.file, e=values['epoch'], k=f'setup:{uuid.uuid4()}',
                 s=values['state'], a=values['actor'], c=values['cancel'], at=NOW - timedelta(hours=1),
                 lt=values['lease'] and uuid.uuid4(), lu=values['lease'], n=values['attempts']))


def test_runtime_create_then_reuse_exact_binding_no_storage_provider_or_journal(indexes):
    b = indexes
    before = b.w.path.read_bytes()
    statements = []
    def sql(c, cursor, stmt, params, ctx, many): statements.append(stmt)
    event.listen(get_engine(), 'before_cursor_execute', sql)
    try:
        r = index(b)
    finally:
        event.remove(get_engine(), 'before_cursor_execute', sql)
    assert r.status_code == 200, r.text
    body = r.json()
    [job] = jobs(b)
    assert body['created'] is True and body['job'] == {'id': str(job['id']), 'state': 'received',
                                                      'cancel_requested': False}
    assert body['item']['id'] == str(b.copy.item) and body['item']['content_status'] == 'metadata_only'
    assert body['item']['index']['job']['id'] == str(job['id']) and body['item']['index']['provider_ready'] is False
    assert (job['origin'], job['scope_visibility'], job['scope_owner_user_id'], job['actor_user_id']) == (
        'promotion', 'common', None, b.w.uid)
    assert (job['item_id'], job['version_id'], job['source_file_id'], job['rights_epoch_at_start']) == (
        b.copy.item, b.copy.version, b.copy.file, 0)
    assert job['idempotency_key'] == f'promotion:{b.copy.file}:1' and job['source_label'] == 'admin_index'
    assert job['created_at'] == NOW and job['lease_token'] is None and job['attempt_count'] == 0
    assert [tuple(a) for a in audits(b)] == [(b.w.uid, str(b.copy.item),
        {'job_id': str(job['id']), 'source_file_id': str(b.copy.file)})]
    # Durable DB job only: no object read/write, no journal record, no provider data.
    assert b.storage.opened == b.opened and not b.storage.puts and not b.storage.deleted
    assert b.w.path.read_bytes() == before
    for table in ('chunks', 'sections', 'index_generations', 'embedding_calls'):
        assert scalar(b, f'SELECT count(*) FROM {table}') == 0
    assert b.copy.key not in r.text and 'PRIVATE-SYNTHETIC' not in r.text
    # Lock order: User -> item UPDATE -> resources -> DML/audit -> journal exclusive; nothing locked after it.
    exclusive = next(i for i, s in enumerate(statements) if 'SELECT pg_advisory_xact_lock(' in s)
    user_lock = max(i for i, s in enumerate(statements[:exclusive]) if 'ras_auth_lock_users' in s)
    item_lock = next(i for i, s in enumerate(statements) if 'FOR UPDATE' in s and 'FROM library_items' in s)
    rights_lock = next(i for i, s in enumerate(statements) if 'FOR SHARE OF access_rights' in s)
    jobs_lock = next(i for i, s in enumerate(statements) if 'FOR SHARE OF ingestion_jobs' in s)
    insert = next(i for i, s in enumerate(statements) if 'INSERT INTO ingestion_jobs' in s)
    audit = next(i for i, s in enumerate(statements) if 'INSERT INTO audit_events' in s)
    assert user_lock < item_lock < rights_lock < jobs_lock < insert < audit < exclusive
    shared = next(i for i, s in enumerate(statements[exclusive:], exclusive) if 'pg_advisory_xact_lock_shared' in s)
    response_locks = ('SELECT library_items.id', 'SELECT access_rights.id', 'SELECT paper_versions.id',
                      'SELECT source_files.id', 'SELECT ingestion_jobs.id')
    assert not any(('FOR UPDATE' in s or 'FOR SHARE' in s) for s in statements[exclusive:shared]
                   if not s.startswith(response_locks))
    # Repeated request: the same live job, untouched, no second success audit.
    again = index(b)
    assert again.status_code == 200 and again.json()['created'] is False
    assert again.json()['job']['id'] == str(job['id']) and jobs(b) == [job] and len(audits(b)) == 1
    assert scalar(b, 'SELECT checksum_sha256 FROM source_files WHERE id=:f', f=b.own.file) == SHA
    assert scalar(b, 'SELECT count(*) FROM ingestion_jobs WHERE item_id=:i', i=b.own.item) == 0


@pytest.mark.parametrize('confirm', [False, None, 1, 0, 1.0, 'true', '1', 'yes', [True], {}])
def test_literal_boolean_true_only(indexes, confirm):
    b = indexes
    r = index(b, data={'confirm_external_processing': confirm})
    assert r.status_code == 422, r.text
    assert jobs(b) == [] and audits(b) == []


@pytest.mark.parametrize('case', ['missing_confirm', 'extra', 'empty', 'missing', 'malformed', 'private', 'user',
                                  'anonymous', 'csrf', 'origin', 'no_origin'])
def test_input_and_acl_no_side_effect(indexes, case):
    b = indexes; kwargs = {}; expected = 404
    if case == 'missing_confirm': kwargs['data'] = {'other': True}; expected = 422
    elif case == 'extra': kwargs['data'] = {'confirm_external_processing': True, 'file': 'x'}; expected = 422
    elif case == 'empty': kwargs['data'] = {}; expected = 422
    elif case in ('missing', 'malformed', 'private'):
        kwargs['item'] = uuid.uuid4() if case == 'missing' else 'not-a-uuid' if case == 'malformed' else b.own.item
    elif case == 'user': kwargs['who'] = 'user'; expected = 403
    elif case == 'anonymous': kwargs['cookies'] = False; expected = 401
    elif case == 'csrf': kwargs['headers'] = {'X-CSRF-Token': 'bad'}; expected = 403
    elif case == 'origin': kwargs['headers'] = {'Origin': 'http://evil.test'}; expected = 403
    else: kwargs['headers'] = {'Origin': None}; expected = 403
    before = b.w.path.read_bytes()
    r = index(b, **kwargs)
    assert r.status_code == expected, r.text
    assert 'job' not in r.json() and jobs(b) == [] and jobs(b, b.own.item) == []
    assert audits(b) == [] and audits(b, 'denied') == [] and b.w.path.read_bytes() == before


@pytest.mark.parametrize('case,code,reason', [
    ('revoked', 'common_copy_revoked', 'revoked'),
    ('not_published', 'common_copy_not_published', 'not_published'),
    ('epoch', 'rights_not_current', 'rights_not_current'),
    ('rights_revoked', 'rights_not_current', 'rights_not_current'),
    ('publication_revoked', 'rights_not_current', 'rights_not_current'),
    ('checksum', 'rights_not_current', 'rights_not_current'),
    ('size', 'rights_not_current', 'rights_not_current'),
    ('no_fulltext', 'fulltext_not_permitted', 'fulltext_not_permitted'),
    ('no_provider', 'external_processing_not_permitted', 'external_processing_not_permitted'),
    ('conditions', 'blocked_by_conditions', 'blocked_by_conditions'),
    ('already_indexed', 'already_indexed', 'already_indexed'),
    ('needs_review', 'extraction_rejected', 'needs_review'),
    ('fulltext_rejected', 'extraction_rejected', 'fulltext_rejected'),
])
def test_ineligible_tuple_closed_no_job_and_code_only_audit(indexes, case, code, reason):
    b = indexes
    i = b.copy.item
    setup = {  # labelled corruption/setup on the owner engine, never the runtime mutation
        'not_published': ["DELETE FROM common_publications WHERE item_id=:i",
                          "UPDATE library_items SET publication_state='pending' WHERE id=:i"],
        'epoch': ['UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i'],
        'rights_revoked': ["UPDATE access_rights SET revoked_at=:at,revoked_by_user_id=reviewer_user_id,"
                           "revocation_reason_code='other',rights_epoch_at_revoke=rights_epoch_at_grant+1 WHERE item_id=:i"],
        'publication_revoked': ['UPDATE common_publications SET revoked_at=:at WHERE item_id=:i'],
        'checksum': ["UPDATE source_files SET checksum_sha256=repeat('a',64) WHERE item_id=:i"],
        'size': ['UPDATE source_files SET byte_size=byte_size+1 WHERE item_id=:i'],
        'no_fulltext': ['UPDATE access_rights SET permits_fulltext_search=false,permits_external_processing=false WHERE item_id=:i'],
        'no_provider': ['UPDATE access_rights SET permits_external_processing=false WHERE item_id=:i'],
        'conditions': ["UPDATE access_rights SET scope_conditions='synthetic condition' WHERE item_id=:i"],
        'already_indexed': ["UPDATE library_items SET content_status='indexed',active_version_id=:v WHERE id=:i"],
    }.get(case, [])
    for sql in setup:
        replica(b, sql, i=i, at=NOW, v=b.copy.version)
    if case == 'revoked':
        runtime_revoke(b.w, i, clock=b.clock)
    if case in ('needs_review', 'fulltext_rejected'):
        replica(b, "INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,source_file_id,"
            "rights_epoch_at_start,idempotency_key,state,finished_at) VALUES('promotion','common',:i,:v,:f,0,"
            "'setup-finished',:s,:at)", i=i, v=b.copy.version, f=b.copy.file, s=case, at=NOW)
    before_jobs, before = list(jobs(b)), b.w.path.read_bytes()
    r = index(b)
    assert (r.status_code, r.json()) == (409, {'error': code}), r.text
    assert jobs(b) == before_jobs and audits(b) == [] and b.w.path.read_bytes() == before
    # Best-effort denial audit on its own fresh writer proof: IDs/codes only.
    assert [tuple(a) for a in audits(b, 'denied')] == [(b.w.uid, str(i), {'reason_code': reason})]
    assert b.storage.opened == b.opened and jobs(b, b.own.item) == []


@pytest.mark.parametrize('case', ['claimed', 'other_actor', 'awaiting_index', 'cancel_requested', 'epoch',
                                  'finished'])
def test_existing_job_returned_untouched_never_reset_or_rebound(indexes, case):
    b = indexes
    over = {'claimed': dict(state='validating', lease=NOW + timedelta(minutes=5), attempts=1),
            'other_actor': dict(actor=b.user.id), 'awaiting_index': dict(state='awaiting_index', attempts=1),
            'cancel_requested': dict(cancel=True), 'epoch': dict(epoch=7), 'finished': dict()}[case]
    job = live_job(b, **over)
    if case == 'finished':
        replica(b, "UPDATE ingestion_jobs SET state='terminal_failed',finished_at=:at WHERE id=:j", j=job, at=NOW)
    before = list(jobs(b))
    r = index(b)
    if case in ('cancel_requested', 'epoch'):
        code = 'index_job_cancelling' if case == 'cancel_requested' else 'index_job_binding_changed'
        assert (r.status_code, r.json()) == (409, {'error': code})
        assert jobs(b) == before and audits(b) == []
        return
    assert r.status_code == 200, r.text
    if case == 'finished':  # a finished job stays finished; a new attempt is a new job
        assert r.json()['created'] is True and len(jobs(b)) == 2 and jobs(b)[0] == before[0]
        assert jobs(b)[1]['idempotency_key'] == f'promotion:{b.copy.file}:2'
        return
    assert r.json()['created'] is False and r.json()['job']['id'] == str(job)
    assert r.json()['job']['state'] == before[0]['state']
    assert jobs(b) == before and audits(b) == []


@pytest.mark.parametrize('admins', ['same', 'other'])
def test_two_concurrent_requests_one_job_one_success_audit(indexes, monkeypatch, admins):
    from app.authz import Principal
    b = indexes; entered, release = threading.Event(), threading.Event()
    second_admin, csrf = b.admin, b.w.actor.csrf_token
    if admins == 'other':
        # Labelled setup: a second Admin account, so only the item lock (not the
        # first Admin's User lock) can serialize the two acceptances.
        replica(b, "UPDATE users SET role='admin' WHERE id=:u", u=b.user.id)
        second_admin = Principal(b.user.id, 'admin', b.owner_session.session_id, b.owner.email, None)
        csrf = b.owner_session.csrf_token
    real = boundary.common_index.latest_job; once = []
    def pause(*a):  # after this acceptance decided to create a job
        result = real(*a)
        if not once:
            once.append(1); entered.set(); assert release.wait(15)
        return result
    monkeypatch.setattr(boundary.common_index, 'latest_job', pause)
    def other():
        return asyncio.run(boundary.attempt(None, second_admin, str(b.copy.item), csrf, b.clock, get_settings(),
                                            threading.Event()))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(direct, b); assert entered.wait(10)
        second = pool.submit(other)
        try: assert wait_for(b.w.engine, 'transactionid')
        finally: release.set()
        results = [first.result(20), second.result(20)]
    [job] = jobs(b)
    assert [r['created'] for r in results] == [True, False]
    assert {r['job']['id'] for r in results} == {str(job['id'])} and len(audits(b)) == 1
    assert job['actor_user_id'] == b.w.uid


@pytest.mark.parametrize('path', ['create', 'reuse'])
@pytest.mark.parametrize('phase', ['accept', 'response'])
@pytest.mark.parametrize('kind', ['idle', 'absolute'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_current_clock_after_final_predicate(indexes, monkeypatch, path, phase, kind, offset):
    b = indexes; armed = [False]
    if path == 'reuse':
        existing = live_job(b)
    settings = get_settings()
    edge = NOW + (settings.auth.session_idle if kind == 'idle' else settings.auth.session_absolute)
    if kind == 'absolute':
        b.after_dependency = lambda db, ctx: replica(b, 'UPDATE sessions SET last_activity_at=:at WHERE id=:s',
            at=edge - timedelta(minutes=1), s=ctx.session_id)
    real_live = resource_writes.session_live
    def live(*a, **kw):
        result = real_live(*a, **kw)
        if armed[0]: b.at = edge + timedelta(microseconds=offset)
        return result
    monkeypatch.setattr(resource_writes, 'session_live', live)
    if phase == 'accept':
        def arm(c, cur, stmt, params, ctx, many):
            # After DML flush; the journal, binding and actor SQL still follow.
            if 'SET CONSTRAINTS ALL IMMEDIATE' in stmt: armed[0] = True
        event.listen(get_engine(), 'after_cursor_execute', arm)
    else:
        real = boundary.respond
        def respond(*a): armed[0] = True; return real(*a)
        monkeypatch.setattr(boundary, 'respond', respond)
    try: r = index(b)
    finally:
        if phase == 'accept': event.remove(get_engine(), 'after_cursor_execute', arm)
    assert r.status_code == (200 if offset < 0 else 401), r.text
    if offset >= 0:
        assert 'job' not in r.json()
        expected = 1 if path == 'reuse' or phase == 'response' else 0
        assert len(jobs(b)) == expected and len(audits(b)) == int(path == 'create' and phase == 'response')
    if path == 'reuse':
        assert [j['id'] for j in jobs(b)] == [existing]


@pytest.mark.parametrize('path', ['create', 'reuse'])
@pytest.mark.parametrize('phase', ['dependency', 'response'])
@pytest.mark.parametrize('case', ['role', 'logout', 'reset', 'csrf', 'hold', 'disable',
                                  'held', 'quarantine', 'binding', 'generation', 'receipt', 'head'])
def test_current_actor_and_gate_not_dependency_snapshot(indexes, monkeypatch, path, phase, case):
    b = indexes
    if path == 'reuse':
        live_job(b)
    if phase == 'dependency':
        b.after_dependency = lambda db, ctx: change(b, case)
    else:
        real = boundary.respond
        def respond(*a): change(b, case); return real(*a)
        monkeypatch.setattr(boundary, 'respond', respond)
    r = index(b)
    assert r.status_code == (503 if case == 'hold' else 403 if case in ('role', 'csrf') else 401), r.text
    assert 'job' not in r.json()
    assert len(jobs(b)) == (1 if path == 'reuse' or phase == 'response' else 0)


@pytest.mark.parametrize('lock', ['user', 'item', 'rights', 'job', 'journal'])
@pytest.mark.parametrize('expired', [False, True])
def test_current_clock_after_real_row_or_journal_lock_wait(indexes, monkeypatch, lock, expired):
    from app.identity.predicates import lock_users
    b = indexes; entered, release = threading.Event(), threading.Event(); real = boundary.accept
    if lock == 'job':
        live_job(b)
    def accept(*a): entered.set(); assert release.wait(15); return real(*a)
    monkeypatch.setattr(boundary, 'accept', accept)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(index, b); assert entered.wait(10)
        with Session(b.w.runtime) as blocker:
            if lock == 'user': lock_users(blocker, [b.w.uid])
            elif lock == 'journal': blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'), {'k': recovery.JOURNAL_LOCK})
            elif lock == 'item': blocker.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'), {'i': b.copy.item})
            elif lock == 'job': blocker.execute(text('SELECT id FROM ingestion_jobs WHERE item_id=:i FOR UPDATE'), {'i': b.copy.item})
            else: blocker.execute(text('SELECT id FROM access_rights WHERE item_id=:i FOR UPDATE'), {'i': b.copy.item})
            release.set()
            assert wait_for(b.w.engine, 'advisory' if lock == 'journal' else 'transactionid')
            if expired: b.at = NOW + get_settings().auth.session_idle
            blocker.rollback()
        r = future.result(20)
    assert r.status_code == (401 if expired else 200), r.text
    assert len(jobs(b)) == int(lock == 'job' or not expired)
    assert len(audits(b)) == int(not expired and lock != 'job')


@pytest.mark.parametrize('first', ['index', 'revoke'])
def test_index_and_revoke_race_item_first_no_deadlock(indexes, monkeypatch, first):
    b = indexes; entered, release = threading.Event(), threading.Event()
    revoked = threading.Event()
    if first == 'index':
        real = boundary._binding
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(boundary, '_binding', hold)
        respond = boundary.respond
        def after(*a): assert revoked.wait(30); return respond(*a)
        monkeypatch.setattr(boundary, 'respond', after)
    else:
        real = publication._cancel_jobs
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(publication, '_cancel_jobs', hold)
    def run_index():
        try: return direct(b)
        except ApiError as e: return e.code
    def run_revoke(): return runtime_revoke(b.w, b.copy.item, clock=b.clock)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(run_index if first == 'index' else run_revoke); assert entered.wait(10)
        z = pool.submit(run_revoke if first == 'index' else run_index)
        try: assert blocked(b.w.engine)
        finally: release.set()
        zr = z.result(30) if first == 'index' else None
        revoked.set()
        ar = a.result(30)
        zr = zr if first == 'index' else z.result(30)
    index_result, revoke_result = (ar, zr) if first == 'index' else (zr, ar)
    assert revoke_result['item']['publication_state'] == 'revoked'
    if first == 'index':
        # Index won: the job exists, revoke cancelled it in its own transaction.
        [job] = jobs(b)
        assert index_result['created'] is True and revoke_result['cancelled_jobs'] == 1
        assert job['cancel_requested'] is True
        # Index response proof came after revoke: current DTO, not the precommit snapshot.
        assert index_result['item']['publication_state'] == 'revoked' and index_result['job']['cancel_requested']
        assert worker.process_job(job['id']) in ('cancelled', 'not_claimed', 'recovery_gate_closed')
    else:
        assert index_result == 'common_copy_revoked' and jobs(b) == []
    assert scalar(b, 'SELECT rights_epoch FROM library_items WHERE id=:i', i=b.copy.item) == 1


@pytest.mark.parametrize('first', ['auth', 'index'])
def test_session_writer_races_index_no_deadlock_new_response_proof(indexes, monkeypatch, first):
    from app.identity.sessions import revoke_session
    from app.identity.session_commit import commit_auth
    from app.models.enums import SessionRevokeReason
    b = indexes; entered, release, auth_done = threading.Event(), threading.Event(), threading.Event()
    if first == 'index':
        real = boundary._binding
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(boundary, '_binding', hold)
        respond = boundary.respond
        def after(*a): assert auth_done.wait(15); return respond(*a)
        monkeypatch.setattr(boundary, 'respond', after)
    else:
        real = boundary.accept
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(boundary, 'accept', hold)
    def auth():
        with Session(b.w.runtime) as db:
            revoke_session(db, session_id=b.w.actor.session_id, user_id=b.w.uid,
                           reason=SessionRevokeReason.USER_REVOKED, now=NOW)
            if first == 'auth':
                release.set(); assert wait_for(b.w.engine, 'transactionid')
            commit_auth(db)
        auth_done.set()
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(index, b); assert entered.wait(10)
        two = pool.submit(auth)
        if first == 'index':
            try: assert blocked(b.w.engine)
            finally: release.set()
        r = one.result(30); two.result(30)
    assert r.status_code == 401 and 'job' not in r.json(), r.text
    assert len(jobs(b)) == (1 if first == 'index' else 0)


@pytest.mark.parametrize('first', ['worker', 'index'])
def test_worker_item_job_checkpoint_order_and_claim_skip(indexes, monkeypatch, first):
    b = indexes; job = live_job(b); entered, release = threading.Event(), threading.Event()
    if first == 'index':
        real = boundary._binding
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(boundary, '_binding', hold)
    def legacy_worker():
        # Legacy worker checkpoint protocol only; not a converted runtime worker.
        with Session(b.w.engine) as d:
            d.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'), {'i': b.copy.item})
            d.execute(text('SELECT id FROM ingestion_jobs WHERE id=:j FOR UPDATE'), {'j': job})
            if first == 'worker': entered.set(); assert release.wait(15)
            d.rollback()
        with Session(b.w.runtime) as d:
            return worker.claim_job(d, job, get_settings(), NOW)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(legacy_worker) if first == 'worker' else pool.submit(direct, b)
        assert entered.wait(10)
        z = pool.submit(direct, b) if first == 'worker' else pool.submit(legacy_worker)
        try: assert wait_for(b.w.engine, 'transactionid')
        finally: release.set()
        ar, zr = a.result(20), z.result(20)
    index_result, token = (zr, ar) if first == 'worker' else (ar, zr)
    assert index_result['created'] is False and index_result['job']['id'] == str(job)
    if first == 'index':  # the waiting worker checkpoint resumes after index released item/job
        assert token is not None and jobs(b)[0]['state'] == 'validating'
    assert len(jobs(b)) == 1


@pytest.mark.parametrize('case', ['advance', 'revoke', 'cleanup'])
def test_response_reflects_current_job_after_known_commit(indexes, monkeypatch, case):
    b = indexes; real = boundary.respond
    def respond(principal, item_id, csrf, clock, settings, outcome):
        if case == 'advance':
            with Session(b.w.engine) as d:  # legacy worker claim, labelled
                assert worker.claim_job(d, outcome.job_id, get_settings(), NOW)
        else:
            runtime_revoke(b.w, b.copy.item, clock=b.clock)
            if case == 'cleanup':
                done = publication.cleanup_revoked(b.w.engine, b.raw_storage, NOW + timedelta(days=2), 10)
                assert sum(done.values()) == 1
        return real(principal, item_id, csrf, clock, settings, outcome)
    monkeypatch.setattr(boundary, 'respond', respond)
    r = index(b)
    assert r.status_code == 200, r.text
    body = r.json(); [job] = jobs(b)
    assert body['created'] is True and body['job']['id'] == str(job['id'])
    assert body['job']['state'] == job['state'] and body['job']['cancel_requested'] == job['cancel_requested']
    if case == 'advance':
        assert job['state'] == 'validating' and body['item']['publication_state'] == 'listed'
    else:
        assert body['item']['publication_state'] == 'revoked' and job['cancel_requested']
        assert (body['item']['title'] is None) == (case == 'cleanup')
        assert (body['item']['rights']['evidence_note'] is None) == (case == 'cleanup')
        assert (body['item']['tombstone']['purged_at'] is not None) == (case == 'cleanup')


def test_late_formatter_denial_rechecks_current_actor(indexes, monkeypatch):
    from app.api.routes import publications
    b = indexes
    def deny(*a):
        change(b, 'role'); raise ApiError(404, 'not_found')
    monkeypatch.setattr(publications, '_copy_out', deny)
    r = index(b)
    assert (r.status_code, r.json()) == (403, {'error': 'forbidden'}) and len(jobs(b)) == 1


@pytest.mark.parametrize('landed', [False, True])
@pytest.mark.parametrize('teardown', ['none', 'rollback', 'close', 'both'])
def test_commit_exception_uncertain_no_select_retry_or_ids(indexes, monkeypatch, landed, teardown):
    b = indexes; calls = []
    class Lost(Session):
        def commit(self):
            if landed: super().commit()
            else: super().rollback()
            self.lost = True
            raise RuntimeError('lost commit acknowledgement')
        def rollback(self):
            if getattr(self, 'lost', False) and teardown in ('rollback', 'both'): raise RuntimeError('rollback')
            return super().rollback()
        def close(self):
            if getattr(self, 'lost', False) and teardown in ('close', 'both'): raise RuntimeError('close')
            return super().close()
    monkeypatch.setattr(boundary, 'Session', Lost)
    monkeypatch.setattr(boundary, 'respond', lambda *a: calls.append('response'))
    monkeypatch.setattr(boundary, 'refusal_audit', lambda *a: calls.append('audit'))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='index_start_outcome_uncertain') as raised:
        direct(b, outcome=outcome)
    assert raised.value.status_code == 503
    assert outcome.commit_started and not outcome.committed and not outcome.rollback_confirmed and calls == []
    assert len(jobs(b)) == int(landed) and len(audits(b)) == int(landed)
    assert get_engine().pool.checkedout() == 0


def test_commit_exception_http_is_controlled_json(indexes, monkeypatch):
    b = indexes
    class Lost(Session):
        def commit(self):
            super().commit(); raise RuntimeError('lost commit acknowledgement')
    monkeypatch.setattr(boundary, 'Session', Lost)
    r = index(b)
    assert (r.status_code, r.json()) == (503, {'error': 'index_start_outcome_uncertain'})
    assert len(jobs(b)) == 1  # landed, never deleted or retried


def teardown_session(fault, nth=1):
    made = []
    class Fault(Session):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw); made.append(self); self.fail = len(made) == nth
        def rollback(self):
            if self.fail and fault in ('rollback_before', 'both_before'): raise RuntimeError('rollback before')
            super().rollback()
            if self.fail and fault == 'rollback_after': raise RuntimeError('rollback after')
        def close(self):
            if self.fail and fault in ('close_before', 'both_before'): raise RuntimeError('close before')
            super().close()
            if self.fail and fault == 'close_after': raise RuntimeError('close after')
    return Fault


@pytest.mark.parametrize('fault', ['rollback_before', 'close_before', 'both_before', 'rollback_after', 'close_after'])
@pytest.mark.parametrize('phase', ['accept', 'response'])
def test_known_commit_teardown_failure_keeps_job_no_dto_no_leak(indexes, monkeypatch, fault, phase):
    b = indexes
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=1 if phase == 'accept' else 2))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='index_start_response_unavailable'): direct(b, outcome=outcome)
    assert outcome.committed and len(jobs(b)) == 1 and len(audits(b)) == 1
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('fault', ['rollback_before', 'close_before', 'both_before'])
def test_denial_teardown_failure_controlled(indexes, monkeypatch, fault):
    b = indexes
    replica(b, "UPDATE access_rights SET scope_conditions='synthetic' WHERE item_id=:i", i=b.copy.item)
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=1))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError) as raised: direct(b, outcome=outcome)
    assert raised.value.status_code == 409 and not outcome.commit_started and jobs(b) == []
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('phase', ['job_insert', 'audit', 'audit_fk', 'binding', 'journal_lock'])
def test_fault_before_commit_rolls_back_no_job(indexes, monkeypatch, phase):
    from dataclasses import replace
    b = indexes; before = b.w.path.read_bytes()
    target = {'job_insert': 'INSERT INTO ingestion_jobs', 'audit': 'INSERT INTO audit_events',
              'journal_lock': 'SELECT pg_advisory_xact_lock('}.get(phase)
    def fail(c, cursor, statement, params, ctx, many):
        if target and target in statement: raise RuntimeError('synthetic SQL fault')
    if phase == 'audit_fk':
        real = boundary.record_event
        def invalid(db, **kw): return real(db, **(kw | {'actor_user_id': uuid.uuid4()}))
        monkeypatch.setattr(boundary, 'record_event', invalid)
    if phase == 'binding':  # the job row read under the journal differs from the flushed snapshot
        real_row, seen = boundary._job_row, []
        def row(db, job_id):
            seen.append(job_id); found = real_row(db, job_id)
            return found if len(seen) == 1 else None
        monkeypatch.setattr(boundary, '_job_row', row)
    event.listen(get_engine(), 'before_cursor_execute', fail)
    try: r = index(b)
    finally: event.remove(get_engine(), 'before_cursor_execute', fail)
    expected = {'binding': (409, 'index_binding_changed')}.get(phase, (503, 'index_start_unavailable'))
    if phase == 'journal_lock': expected = (503, 'service_recovering')
    assert (r.status_code, r.json()) == (expected[0], {'error': expected[1]}), r.text
    assert jobs(b) == [] and audits(b) == [] and b.w.path.read_bytes() == before


def test_integrity_conflict_outside_locked_dedup_is_controlled(indexes, monkeypatch):
    b = indexes; job = live_job(b)
    # Adversary: the dedup read misses the live job, so only the partial unique index stops a second one.
    monkeypatch.setattr(boundary.common_index, 'live_job', lambda db, file_id: None)
    r = index(b)
    assert (r.status_code, r.json()) == (503, {'error': 'index_start_unavailable'})
    assert [j['id'] for j in jobs(b)] == [job] and audits(b) == []


def test_broker_unavailable_job_still_durable_for_discovery(indexes, monkeypatch):
    b = indexes
    import redis
    def down(*a, **kw): raise redis.exceptions.ConnectionError('synthetic broker outage')
    monkeypatch.setattr(redis.Redis, 'execute_command', down)
    r = index(b)
    assert r.status_code == 200 and r.json()['created'] is True
    with Session(b.w.runtime) as db:  # legacy discovery query on the runtime role, no provider settings
        assert uuid.UUID(r.json()['job']['id']) in worker.due_job_ids(db, get_settings(), NOW)


@pytest.mark.parametrize('phase', ['before_commit', 'response'])
@pytest.mark.parametrize('cancel', ['attempt', 'receive', 'disconnect'])
def test_cancel_keeps_thread_owned_until_settle(indexes, monkeypatch, phase, cancel):
    b = indexes; entered, release = threading.Event(), threading.Event(); outcome = boundary.Outcome()
    cut = [False]
    if phase == 'before_commit':
        real = boundary._binding
        def hold(*a): result = real(*a); entered.set(); assert release.wait(15); return result
        monkeypatch.setattr(boundary, '_binding', hold)
    else:  # inside the response proof transaction
        real = boundary.common_publish._lock_item
        def hold(*a, **kw): result = real(*a, **kw); entered.set(); assert release.wait(15); return result
        monkeypatch.setattr(boundary.common_publish, '_lock_item', hold)
    class Request:
        headers = {'x-csrf-token': b.w.actor.csrf_token}
        async def is_disconnected(self): return cut[0]
    async def run():
        stop = threading.Event()
        if cancel == 'receive':
            task = asyncio.create_task(boundary.receive(Request(), b.admin, str(b.copy.item), b.clock, get_settings()))
        else:
            task = asyncio.create_task(boundary.attempt(Request(), b.admin, str(b.copy.item), b.w.actor.csrf_token,
                                                        b.clock, get_settings(), stop, outcome))
        assert await asyncio.to_thread(entered.wait, 10)
        if cancel in ('attempt', 'receive'):
            task.cancel(); await asyncio.sleep(0)
        else:
            cut[0] = True; stop.set()
        # The executor thread still owns its transaction until it settles.
        assert get_engine().pool.checkedout() == 1
        release.set()
        if cancel in ('attempt', 'receive'):
            with pytest.raises(asyncio.CancelledError): await task
            await asyncio.gather(*list(boundary._attempts), return_exceptions=True)
        else:
            with pytest.raises(ApiError): await task
    asyncio.run(run())
    assert len(jobs(b)) == int(phase == 'response')
    assert get_engine().pool.checkedout() == 0 and not boundary._attempts


def test_pending_http_unpatched_gate_no_mutation(installation):
    from fastapi.testclient import TestClient
    from app.main import app
    before = installation.path.read_bytes()
    with TestClient(app) as c:
        r = c.post(f'/admin/common-copies/{uuid.uuid4()}/index', json={'confirm_external_processing': True},
                   headers={'Origin': 'http://web.test'})
        assert r.status_code == 503 and r.json() == {'error': 'service_recovering'}
        assert c.get('/healthz').status_code == 200 and c.get('/readyz').status_code == 503
    assert installation.path.read_bytes() == before
    with installation.engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM ingestion_jobs')) == 0
