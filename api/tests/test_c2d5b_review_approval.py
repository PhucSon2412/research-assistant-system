"""C2d-5b runtime approve/copy: proofs, unlocked copy, coupled journal, outcomes.

Accounts/session/head/receipt come from real identity (bootstrap, invitation,
login, runtime role). Shared business setup uses the real C4 contract; resource corruption,
storage faults and restore setup use the separate test owner or fakes.
"""
import asyncio
import hashlib
import io
import threading
import time
import tracemalloc
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from botocore.response import StreamingBody
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import journal, recovery
from app.api import review_approval as boundary, review_writes, resource_writes
from app.api.errors import ApiError
from app.api.routes import reviews
from app.authz import Principal
from app.config import get_settings
from app.db import get_engine
from app.identity.login import login
from app.identity.predicates import lock_users
from app.identity.session_commit import commit_auth
from app.identity.sessions import revoke_session
from app.models import LibraryItem, ReviewRequest
from app.models.enums import SessionRevokeReason
from tests.resource_fixtures import BODY, SHA, MemoryOriginals, source
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import EMAIL, PASSWORD, accept, issue, world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import opened, request as review_post, wait_for, writers  # noqa: F401

PRIVATE = ('PRIVATE-SYNTHETIC-TITLE', 'PRIVATE-SOURCE-DECLARATION', 'PRIVATE-RIGHTS-DECLARATION', 'synthetic/')
ADMIN_TEXT = ('ADMIN-CHECKED-SOURCE', 'ADMIN-CHECKED-EVIDENCE', 'https://license.example.test/l',
              'ADMIN-COMMON-TITLE')
THIN = {'id', 'status', 'submitted_at', 'closed_at', 'close_reason_code', 'review_available', 'publication',
        'pending_note', 'pending_publication_id'}


class Originals(MemoryOriginals):
    """In-memory S3 stand-in with hooks; records every key opened/put/deleted."""

    def __init__(self, ids, objects=None):
        super().__init__(ids)
        if objects:
            self.objects.update(objects)
        self.private = ids.key
        self.opened, self.puts, self.deleted, self.hooks = [], [], [], {}

    def _hook(self, name, key):
        hook = self.hooks.get(name)
        if hook:
            hook(key)

    def open(self, key):
        self.opened.append(key)
        self._hook('open', key)
        if key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        return StreamingBody(io.BytesIO(self.objects[key]), len(self.objects[key]))

    def put(self, key, file, mime):
        self.puts.append(key)
        file.seek(0)
        self.objects[key] = file.read()
        self._hook('put', key)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    def copies(self):
        return {k: v for k, v in self.objects.items() if k.startswith('originals/')}


@pytest.fixture
def approvals(writers):
    b = writers
    b.storage = Originals(b.own)
    from app.main import app
    app.dependency_overrides[reviews._storage] = lambda: b.storage
    opened(b)
    return b


def body(**over):
    scope = {'audience': 'instance_users', 'original_download': True, 'fulltext_search': True,
             'external_processing': False, 'conditions': None}
    return {'confirm_verified': True, 'file_checksum_sha256': SHA, 'title': 'ADMIN-COMMON-TITLE',
            'verified_source': 'ADMIN-CHECKED-SOURCE', 'verified_source_url': 'https://source.example.test/p',
            'license_url': 'https://license.example.test/l', 'evidence_note': 'ADMIN-CHECKED-EVIDENCE',
            'checked_on': NOW.date().isoformat(), 'scope': scope} | over


def approve(b, data=None, *, rid=None, who='admin', headers=None):
    issued = b.w.actor if who == 'admin' else b.owner_session
    b.client.cookies.set('ras_session', issued.token)
    b.client.cookies.set('ras_csrf', issued.csrf_token)
    h = {'Origin': 'http://web.test', 'X-CSRF-Token': issued.csrf_token} | (headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    return b.client.post(f'/admin/review-requests/{rid or b.rid}/approve', json=body() if data is None else data,
                         headers=h)


def state(b):
    with b.w.engine.connect() as c:
        return SimpleNamespace(
            requests=c.execute(text('SELECT id,status,closed_by_user_id,close_reason_code FROM review_requests'
                                    ' ORDER BY submitted_at,id')).all(),
            common=c.execute(text("SELECT id,publication_state,owner_user_id,content_status,rights_epoch,title"
                                  " FROM library_items WHERE visibility='common'")).all(),
            files=c.execute(text("SELECT f.id,f.storage_key,f.checksum_sha256,f.byte_size,f.file_source FROM"
                                 " source_files f JOIN library_items i ON i.id=f.item_id WHERE"
                                 " i.visibility='common'")).all(),
            rights=c.execute(text('SELECT * FROM access_rights')).mappings().all(),
            audit=c.execute(text("SELECT outcome,target_id,details FROM audit_events WHERE"
                                 " action='review.request.approve' ORDER BY id")).all(),
            mirrors=c.execute(text("SELECT review_request_id,journal_confirmed FROM revocation_journal_records"
                                   " WHERE kind='review_closure' ORDER BY seq")).all())


def private_rows(b):
    with b.w.engine.connect() as c:
        return [c.execute(text(sql), {'i': b.own.item}).all() for sql in (
            'SELECT * FROM library_items WHERE id=:i', 'SELECT * FROM paper_versions WHERE item_id=:i',
            'SELECT * FROM source_files WHERE item_id=:i', 'SELECT * FROM collection_items WHERE item_id=:i')]


def nothing(b, s=None):
    s = s or state(b)
    return not s.common and not s.files and not s.rights and all(r[1] != 'approved' for r in s.requests)


def gate(b):
    return recovery.evaluate_integrity(get_settings(), b.w.runtime)


def old_url(b):
    return f'/admin/review-requests/{b.rid}/files/{b.own.file}/original'


def as_admin(b):
    b.client.cookies.set('ras_session', b.w.actor.token)
    b.client.cookies.set('ras_csrf', b.w.actor.csrf_token)


def replica(b, sql, **params):
    with b.w.engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text(sql), params)


# --- contract / ACL ----------------------------------------------------------------

def test_runtime_success_thin_response_coupled_rows_journal_and_lock_order(approvals, monkeypatch, caplog):
    b = approvals
    before_private = private_rows(b)
    before = journal.read(b.w.path)
    statements, txs = [], {}

    def sql(conn, cursor, statement, params, ctx, many):
        statements.append(statement)

    real_accept, real_respond = boundary.accept, boundary.respond

    def accept(*a, **kw):
        out = real_accept(*a, **kw)
        txs['accept'] = threading.get_ident()
        return out

    def respond(*a, **kw):
        statements.append('-- respond --')
        return real_respond(*a, **kw)

    monkeypatch.setattr(boundary, 'accept', accept)
    monkeypatch.setattr(boundary, 'respond', respond)
    event.listen(get_engine(), 'before_cursor_execute', sql)
    try:
        r = approve(b)
    finally:
        event.remove(get_engine(), 'before_cursor_execute', sql)
    assert r.status_code == 200, r.text
    data = r.json()['request']
    assert set(data) == THIN
    assert (data['status'], data['close_reason_code'], data['review_available'], data['publication']) == (
        'approved', 'approved_by_admin', False, False)
    s = state(b)
    assert len(s.common) == 1 and data['pending_publication_id'] == str(s.common[0][0])
    assert s.common[0][1:] == ('pending', None, 'metadata_only', 0, 'ADMIN-COMMON-TITLE')
    (fid, key, checksum, size, kind), = s.files
    assert key != b.own.key and key.startswith('originals/') and (checksum, size, kind) == (
        SHA, len(BODY), 'review_approval')
    assert b.storage.objects[key] == BODY and b.storage.objects[b.own.key] == BODY
    assert b.storage.puts == [key] and b.storage.deleted == []
    rights, = s.rights
    assert (rights['origin_review_request_id'], rights['source_file_id'], rights['reviewer_user_id']) == (
        b.rid, fid, b.w.uid)
    assert (rights['verified_source'], rights['evidence_note'], rights['rights_epoch_at_grant']) == (
        'ADMIN-CHECKED-SOURCE', 'ADMIN-CHECKED-EVIDENCE', 0)
    assert 'PRIVATE-SOURCE-DECLARATION' not in str(dict(rights))
    assert s.requests[0][1:] == ('approved', b.w.uid, 'approved_by_admin')
    assert s.mirrors == [(b.rid, True)]
    after = journal.read(b.w.path)
    assert len(after.records) == len(before.records) + 1
    assert after.head.body == {'request_id': str(b.rid), 'item_id': str(b.own.item),
        'version_id': str(b.own.version), 'source_file_id': str(b.own.file), 'status': 'approved',
        'reason_code': 'approved_by_admin', 'closed_at': NOW.isoformat()}
    (outcome, target, details), = s.audit
    assert (outcome, target) == ('success', str(b.rid))
    assert details == {'common_item_id': data['pending_publication_id'], 'source_file_id': str(fid),
                       'access_rights_id': str(rights['id'])}
    leaks = str(s.audit) + b.w.path.read_text() + caplog.text + r.text
    assert not any(m in leaks for m in PRIVATE + ADMIN_TEXT + (SHA, key, PASSWORD))
    assert private_rows(b) == before_private
    assert gate(b).open
    as_admin(b)
    assert b.client.get(old_url(b)).status_code == 404
    detail = b.client.get(f'/admin/review-requests/{b.rid}').json()['request']
    assert detail['status'] == 'approved' and detail['review_available'] is False
    assert detail['pending_publication_id'] == data['pending_publication_id']
    # Lock order of acceptance: User, request UPDATE, private item SHARE, DML, then journal exclusive.
    insert = next(i for i, s_ in enumerate(statements) if 'INSERT INTO library_items' in s_)
    # The dependency may touch activity earlier; acceptance starts at its own User lock.
    start = max(i for i, s_ in enumerate(statements[:insert]) if 'ras_auth_lock_users' in s_)
    journal_at = next(i for i, s_ in enumerate(statements[start:], start) if 'pg_advisory_xact_lock(' in s_)
    head = statements[start:journal_at]
    request_lock = next(i for i, s_ in enumerate(head) if 'FROM review_requests' in s_ and 'FOR UPDATE' in s_)
    item_lock = next(i for i, s_ in enumerate(head) if 'FROM library_items' in s_ and 'FOR SHARE' in s_)
    assert request_lock < item_lock < next(i for i, s_ in enumerate(head) if 'INSERT INTO library_items' in s_)
    assert any('INSERT INTO audit_events' in s_ for s_ in head)
    respond_at = statements.index('-- respond --')
    assert not any('FOR UPDATE' in s_ or 'FOR NO KEY UPDATE' in s_ or 'ras_auth_lock_users' in s_
                   for s_ in statements[journal_at:respond_at])
    assert any('ras_auth_review_approval_mirror' in s_ for s_ in statements[journal_at:respond_at])
    # Response: request SHARE, common item SHARE, then journal shared; never User or private item.
    tail = statements[respond_at:]
    assert 'review_requests' in next(s_ for s_ in tail if 'FOR SHARE' in s_)
    assert not any('ras_auth_lock_users' in s_ or 'FOR UPDATE' in s_ for s_ in tail)
    assert any('pg_advisory_xact_lock_shared' in s_ for s_ in tail)
    assert get_engine().pool.checkedout() == 0 and boundary.slots.active == 0


def test_two_users_identical_bytes_copy_only_the_exact_reviewed_file(approvals):
    b = approvals
    other = source(b.w, b.w.uid, 'OTHER-IDENTICAL-BYTES')  # same bytes/checksum, another owner and key
    b.storage.objects[other.key] = BODY
    before = private_rows(b)
    r = approve(b)
    assert r.status_code == 200, r.text
    assert [k for k in b.storage.opened if not k.startswith('originals/')] == [b.own.key]
    assert other.key not in b.storage.opened and b.storage.objects[other.key] == BODY
    with b.w.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM library_items WHERE id=:i AND visibility='private'"),
                        {'i': other.item}) == 1
    assert private_rows(b) == before
    # A second request for the same private file is a new grant, never a dedup to the first copy.
    b.rid = None
    opened(b)
    r = approve(b)
    assert r.status_code == 200, r.text
    s = state(b)
    assert len(s.common) == 2 and len({f[1] for f in s.files}) == 2 and len(b.storage.copies()) == 2


@pytest.mark.parametrize('value', (False, None, 1, 'true', 'yes', 0, [True], {'v': True}, 'missing'))
def test_confirm_verified_must_be_explicit_json_true(approvals, value):
    b = approvals
    data = body()
    if value == 'missing':
        del data['confirm_verified']
    else:
        data['confirm_verified'] = value
    r = approve(b, data)
    assert r.status_code == 422, r.text
    assert nothing(b) and b.storage.opened == [] and b.storage.puts == []


@pytest.mark.parametrize('extra', ({'declared_rights': 'PRIVATE-RIGHTS-DECLARATION'}, {'declared_source': 'x'},
                                   {'unexpected': 'secret'}, {'storage_key': 'originals/x'}))
def test_declaration_or_extra_fields_rejected(approvals, extra):
    b = approvals
    r = approve(b, body() | extra)
    assert r.status_code == 422 and 'PRIVATE' not in r.text and nothing(b)


@pytest.mark.parametrize('over,code', (
    ({'license_url': None, 'evidence_note': '   '}, 'rights_evidence_required'),
    ({'scope': {'audience': 'instance_users', 'original_download': False, 'fulltext_search': False,
                'external_processing': True, 'conditions': None}}, 'rights_scope_inconsistent'),
    ({'checked_on': (NOW.date() + timedelta(days=2)).isoformat()}, 'checked_on_in_future')))
def test_admin_evidence_scope_and_check_date(approvals, over, code):
    b = approvals
    r = approve(b, body(**over))
    assert (r.status_code, r.json()) == (422, {'error': code})
    assert nothing(b) and b.storage.opened == []


def test_check_date_one_day_slack_and_license_or_evidence_enough(approvals):
    b = approvals
    r = approve(b, body(checked_on=(NOW.date() + timedelta(days=1)).isoformat(), license_url=None))
    assert r.status_code == 200, r.text
    rights = state(b).rights[0]
    assert rights['license_url'] is None and rights['evidence_note'] == 'ADMIN-CHECKED-EVIDENCE'


@pytest.mark.parametrize('over', ({'license_url': None, 'evidence_note': None},
                                  {'checked_on': (NOW.date() + timedelta(days=3)).isoformat()}))
def test_late_input_denial_never_skips_final_caller(approvals, over):
    b = approvals
    b.after_dependency = lambda db, ctx: setattr(b, 'at', NOW + get_settings().auth.session_idle)
    r = approve(b, body(**over))
    assert r.status_code == 401 and 'Max-Age=0' in r.headers['set-cookie']
    assert nothing(b)


def test_request_state_denials_record_nothing_and_never_read_storage(approvals):
    b = approvals
    first = b.rid
    assert approve(b, body(file_checksum_sha256='0' * 64)).json() == {'error': 'checksum_confirmation_mismatch'}
    assert approve(b, rid=str(uuid.uuid4())).json() == {'error': 'not_found'}
    assert approve(b, rid='not-a-uuid').json() == {'error': 'not_found'}
    assert approve(b, who='owner').status_code == 403
    assert approve(b, headers={'Origin': None}).status_code == 403
    assert approve(b, headers={'X-CSRF-Token': None}).status_code == 403
    assert approve(b, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    replica(b, 'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i', i=b.own.item)
    assert approve(b).json() == {'error': 'review_grant_inactive'}
    replica(b, 'UPDATE library_items SET rights_epoch=0 WHERE id=:i', i=b.own.item)
    assert review_post(b, 'withdraw').status_code == 200
    assert approve(b).json() == {'error': 'review_request_closed'}
    b.rid = None
    opened(b)
    assert review_post(b, 'reject').status_code == 200
    assert approve(b).json() == {'error': 'review_request_closed'}
    assert nothing(b) and b.storage.opened == [] and b.storage.puts == []
    audit = state(b).audit
    assert [d['reason_code'] for o, t, d in audit] == ['checksum_confirmation_mismatch', 'grant_inactive',
                                                        'request_closed', 'request_closed']
    assert all(o == 'denied' for o, t, d in audit) and audit[0][1] == str(first)
    assert gate(b).open and get_engine().pool.checkedout() == 0 and boundary.slots.active == 0


def test_repeat_and_later_closes_change_nothing(approvals):
    b = approvals
    assert approve(b).status_code == 200
    copies = b.storage.copies()
    assert approve(b).json() == {'error': 'review_request_closed'}
    assert review_post(b, 'reject').json() == {'error': 'review_request_closed'}
    assert review_post(b, 'withdraw').json() == {'error': 'review_request_closed'}
    assert b.storage.copies() == copies and len(state(b).rights) == 1
    assert len([r for r in journal.read(b.w.path).records if r.kind == journal.REVIEW_CLOSURE]) == 1


def test_pending_copy_closed_on_every_normal_route_even_for_admin(approvals):
    b = approvals
    r = approve(b)
    common = r.json()['request']['pending_publication_id']
    fid = str(state(b).files[0][0])
    for who in ('admin', 'owner'):
        issued = b.w.actor if who == 'admin' else b.owner_session
        b.client.cookies.set('ras_session', issued.token)
        b.client.cookies.set('ras_csrf', issued.csrf_token)
        h = {'Origin': 'http://web.test', 'X-CSRF-Token': issued.csrf_token}
        for scope in ('all', 'common', 'mine'):
            assert common not in b.client.get(f'/library/items?scope={scope}&limit=100').text, (who, scope)
        for path in (f'/library/items/{common}', f'/library/items/{common}/chunks', f'/files/{fid}/original',
                     f'/library/items/{common}/references'):
            assert b.client.get(path).status_code == 404, (who, path)
        box = b.client.post('/collections', json={'name': f'{who} box'}, headers=h)
        assert box.status_code == 201, box.text
        added = b.client.post(f"/collections/{box.json()['collection']['id']}/items", json={'item_id': common},
                              headers=h)
        assert added.status_code == 404, (who, added.text)
    # The owner learns the decision, never the copy, its rights or the Admin's evidence.
    b.client.cookies.set('ras_session', b.owner_session.token)
    mine = b.client.get(f'/review-requests/{b.rid}').json()['request']
    assert mine['status'] == 'approved' and common not in str(mine) and 'ADMIN-CHECKED' not in str(mine)


# --- caller / clock -----------------------------------------------------------------

ACTOR_CASES = ('role', 'revoke', 'held', 'quarantine', 'binding', 'generation', 'receipt', 'head', 'csrf')


def change_actor(b, case):
    uid, sid = b.w.uid, b.w.actor.session_id
    if case == 'revoke':
        with Session(b.w.runtime) as d:
            revoke_session(d, session_id=sid, user_id=uid, reason=SessionRevokeReason.USER_REVOKED, now=NOW)
            commit_auth(d)
        return
    sql = {'role': "UPDATE users SET role='user' WHERE id=:u",
           'held': "UPDATE users SET status_safety='held' WHERE id=:u",
           'quarantine': "UPDATE users SET credential_state='quarantined' WHERE id=:u",
           'binding': 'UPDATE sessions SET user_id=:other WHERE id=:s',
           'generation': 'UPDATE sessions SET credential_generation=gen_random_uuid() WHERE id=:s',
           'receipt': "DELETE FROM auth_change_receipts WHERE user_id=:u AND domain='credential'",
           'head': 'UPDATE auth_safety_heads SET credential_generation=gen_random_uuid() WHERE user_id=:u',
           'csrf': "UPDATE sessions SET csrf_token_hash=repeat('0',64) WHERE id=:s"}[case]
    replica(b, sql, u=uid, s=sid, other=b.user.id)


def expected_denial(case):
    return 403 if case in ('role', 'csrf') else 401


@pytest.mark.parametrize('phase', ('dependency', 'source_read', 'after_put', 'before_accept'))
@pytest.mark.parametrize('case', ACTOR_CASES)
def test_actor_change_before_commit_records_nothing_and_drops_only_new_key(approvals, monkeypatch, case, phase):
    b = approvals
    once = []

    def fire(*_):
        if not once:
            once.append(1)
            change_actor(b, case)

    if phase == 'dependency':
        b.after_dependency = lambda db, ctx: fire()
    elif phase == 'source_read':
        b.storage.hooks['open'] = lambda key: fire() if key == b.own.key else None
    elif phase == 'after_put':
        b.storage.hooks['put'] = fire
    else:
        real = boundary.accept
        monkeypatch.setattr(boundary, 'accept', lambda *a, **kw: (fire(), real(*a, **kw))[1])
    r = approve(b)
    assert r.status_code == expected_denial(case), r.text
    if r.status_code == 401:
        assert 'Max-Age=0' in r.headers['set-cookie']
    assert nothing(b) and b.storage.copies() == {} and b.storage.objects[b.own.key] == BODY
    assert b.storage.deleted == b.storage.puts  # only this attempt's own key, after its threads settled
    assert not [a for a in state(b).audit if a[0] == 'success']
    assert get_engine().pool.checkedout() == 0 and boundary.slots.active == 0


@pytest.mark.parametrize('case', ACTOR_CASES)
def test_actor_change_after_known_commit_no_dto_rows_and_copy_kept(approvals, monkeypatch, case):
    b = approvals
    real = boundary.respond

    def respond(*a, **kw):
        change_actor(b, case)
        return real(*a, **kw)

    monkeypatch.setattr(boundary, 'respond', respond)
    r = approve(b)
    assert r.status_code == expected_denial(case), r.text
    assert 'pending_publication_id' not in r.text
    s = state(b)
    assert s.requests[0][1] == 'approved' and len(s.common) == 1 and len(b.storage.copies()) == 1
    assert b.storage.deleted == []


@pytest.mark.parametrize('phase', ('acceptance', 'response'))
@pytest.mark.parametrize('kind', ('idle', 'absolute'))
@pytest.mark.parametrize('offset', (-1, 0, 1))
def test_ttl_boundary_uses_clock_after_last_sql(approvals, monkeypatch, phase, kind, offset):
    b = approvals
    settings = get_settings()
    if kind == 'absolute':
        def near_absolute(db, ctx):
            with b.w.engine.begin() as c:
                c.execute(text('UPDATE sessions SET last_activity_at=:at WHERE id=:s'),
                          {'s': ctx.session_id, 'at': NOW + settings.auth.session_absolute - timedelta(minutes=1)})
        b.after_dependency = near_absolute
    armed = [False]
    edge = NOW + (settings.auth.session_idle if kind == 'idle' else settings.auth.session_absolute)
    if phase == 'acceptance':
        real_gate = boundary._gate

        def arm(db, s, pending=None):
            out = real_gate(db, s, pending)
            armed[0] = armed[0] or pending is not None
            return out
        monkeypatch.setattr(boundary, '_gate', arm)
    else:
        real_respond = boundary.respond
        monkeypatch.setattr(boundary, 'respond', lambda *a, **kw: (armed.__setitem__(0, True), real_respond(*a, **kw))[1])
    real_live = resource_writes.session_live

    def live(*a, **kw):
        out = real_live(*a, **kw)  # the predicate SQL itself crosses the boundary
        if armed[0]:
            b.at = edge + timedelta(microseconds=offset)
        return out

    monkeypatch.setattr(resource_writes, 'session_live', live)
    r = approve(b)
    if offset < 0:
        assert r.status_code == 200, r.text
        return
    assert r.status_code == 401, r.text
    s = state(b)
    if phase == 'acceptance':
        assert nothing(b, s) and b.storage.copies() == {} and gate(b).open
    else:
        assert s.requests[0][1] == 'approved' and len(b.storage.copies()) == 1


@pytest.mark.parametrize('lock', ('user', 'request', 'private_item', 'journal'))
@pytest.mark.parametrize('expired', (False, True))
def test_lock_wait_then_current_clock(approvals, monkeypatch, lock, expired):
    b = approvals
    entered, release = threading.Event(), threading.Event()
    real = boundary.accept

    def accept(*a, **kw):
        entered.set()
        assert release.wait(15)
        return real(*a, **kw)

    monkeypatch.setattr(boundary, 'accept', accept)
    with ThreadPoolExecutor(1) as pool:
        f = pool.submit(approve, b)
        assert entered.wait(15)
        with Session(b.w.runtime) as blocker:
            if lock == 'user':
                lock_users(blocker, [b.w.uid])
            elif lock == 'request':
                blocker.execute(select(ReviewRequest.id).where(ReviewRequest.id == b.rid).with_for_update(read=True))
            elif lock == 'private_item':
                blocker.execute(select(LibraryItem.id).where(LibraryItem.id == b.own.item).with_for_update())
            else:
                blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'), {'k': recovery.JOURNAL_LOCK})
            release.set()
            assert wait_for(b.w.engine, 'advisory' if lock == 'journal' else 'transactionid')
            b.at = NOW + get_settings().auth.session_idle - timedelta(microseconds=0 if expired else 1)
            blocker.rollback()
        r = f.result(30)
    assert r.status_code == (401 if expired else 200), r.text
    if expired:
        assert nothing(b) and b.storage.copies() == {}
    assert get_engine().pool.checkedout() == 0


# --- storage / attempt ownership ---------------------------------------------------

def failure_reasons(b):
    return [d['reason_code'] for o, t, d in state(b).audit if o == 'failure']


def test_missing_source_object_records_nothing_and_never_puts(approvals):
    b = approvals
    del b.storage.objects[b.own.key]
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_storage_unavailable'})
    assert nothing(b) and b.storage.puts == [] and failure_reasons(b) == ['source_unavailable']
    assert state(b).requests[0][1] == 'open'


@pytest.mark.parametrize('bad', ('same_size', 'longer', 'shorter'))
def test_wrong_source_bytes_is_integrity_failure_and_private_original_kept(approvals, bad):
    b = approvals
    wrong = {'same_size': BODY[:-1] + b'!', 'longer': BODY + b'x' * 10, 'shorter': BODY[:-3]}[bad]
    b.storage.objects[b.own.key] = wrong
    r = approve(b)
    assert (r.status_code, r.json()) == (409, {'error': 'review_file_integrity_failed'})
    assert nothing(b) and b.storage.puts == [] and failure_reasons(b) == ['checksum_mismatch']
    assert b.storage.objects[b.own.key] == wrong  # never deleted or repaired by approval
    b.storage.objects[b.own.key] = BODY
    assert approve(b).status_code == 200


@pytest.mark.parametrize('fault,reason', (('put_after_store', 'copy_failed'), ('put_before_store', 'copy_failed'),
                                          ('readback_mismatch', 'copy_mismatch'), ('readback_raises', 'copy_failed'),
                                          ('readback_short', 'copy_mismatch')))
def test_copy_faults_delete_only_the_attempt_key(approvals, monkeypatch, fault, reason):
    b = approvals
    real_put, real_open = b.storage.put, b.storage.open

    def put(key, file, mime):
        if fault == 'put_before_store':
            b.storage.puts.append(key)
            raise ConnectionError('synthetic S3 put failure')
        real_put(key, file, mime)
        if fault == 'put_after_store':
            raise ConnectionError('synthetic S3 failure after a partial object')

    def open_(key):
        if key.startswith('originals/') and fault.startswith('readback'):
            if fault == 'readback_raises':
                raise ConnectionError('synthetic readback failure')
            data = BODY[:-1] + b'?' if fault == 'readback_mismatch' else BODY[:-1]
            return StreamingBody(io.BytesIO(data), len(data))
        return real_open(key)

    monkeypatch.setattr(b.storage, 'put', put)
    monkeypatch.setattr(b.storage, 'open', open_)
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_storage_unavailable'})
    assert nothing(b) and b.storage.copies() == {} and b.storage.deleted == b.storage.puts
    assert b.storage.objects[b.own.key] == BODY and failure_reasons(b) == [reason]
    assert gate(b).open and boundary.slots.active == 0


class FaultySpool:
    def __init__(self, fault):
        import tempfile
        self.file, self.fault, self.closed = tempfile.TemporaryFile(), fault, False

    def write(self, data):
        if self.fault == 'write':
            raise OSError('synthetic temp write failure')
        return self.file.write(data[:-1] if self.fault == 'short' and len(data) > 1 else data)

    def close(self):
        self.closed = True
        self.file.close()
        if self.fault == 'close':
            raise OSError('synthetic temp close failure')

    def __getattr__(self, name):
        return getattr(self.file, name)


@pytest.mark.parametrize('fault', ('write', 'short', 'close'))
def test_spool_faults_are_local_storage_errors_and_always_release(approvals, monkeypatch, fault):
    b = approvals
    spools = []

    def open_spool():
        spools.append(FaultySpool(fault))
        return spools[-1]

    monkeypatch.setattr(boundary, 'open_spool', open_spool)
    r = approve(b)
    if fault == 'close':
        assert r.status_code == 200, r.text  # a temp close fault never replaces the known outcome
    else:
        assert (r.status_code, r.json()) == (503, {'error': 'approval_storage_unavailable'})
        assert nothing(b) and b.storage.puts == [] and failure_reasons(b) == ['source_unavailable']
    assert spools and all(s.closed for s in spools) and boundary.slots.active == 0


def test_30_mib_stream_is_copied_and_verified_without_buffering_in_memory():
    size, block = 30 * 1024 * 1024, 64 * 1024
    pattern = bytes(range(256)) * (block // 256)

    def chunks():
        for _ in range(size // block):
            yield pattern

    digest = hashlib.sha256()
    for c in chunks():
        digest.update(c)

    class Stream:
        def iter_chunks(self, chunk_size):
            return chunks()

        def close(self):
            pass

    class Storage:
        stored = None

        def open(self, key):
            return Stream()

        def put(self, key, file, mime):
            file.seek(0)
            h, n = hashlib.sha256(), 0
            while data := file.read(block):
                h.update(data)
                n += len(data)
            self.stored = (n, h.hexdigest())

    target = boundary.Target(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 'synthetic/src',
                             size, 'application/pdf', None, None, digest.hexdigest(), 0)
    storage, stopped = Storage(), threading.Event()
    spool = boundary.open_spool()
    tracemalloc.start()
    try:
        assert boundary.read_source(storage, target, spool, stopped)
        assert boundary.store_copy(storage, 'originals/' + 'a' * 32, target, spool, stopped)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        spool.close()
    assert storage.stored == (size, digest.hexdigest())
    assert peak < 4 * 1024 * 1024, peak  # streamed in bounded chunks, never the whole 30 MiB


def test_copy_pool_exhaustion_is_busy_and_slot_returns(approvals, monkeypatch):
    b = approvals
    monkeypatch.setenv('RAS_FILES__APPROVAL_COPY_MAX_CONCURRENT', '1')
    get_settings.cache_clear()
    first_rid = b.rid
    other = source(b.w, b.user.id, 'SECOND-PRIVATE')
    b.storage.objects[other.key] = BODY
    b.body = b.body.model_copy(update={'item_id': other.item, 'version_id': other.version,
                                       'source_file_id': other.file})
    b.rid = None
    second_rid = opened(b)
    entered, release = threading.Event(), threading.Event()

    def hold(key):
        if key == b.own.key:
            entered.set()
            assert release.wait(15)

    b.storage.hooks['open'] = hold
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(approve, b, rid=first_rid)
        assert entered.wait(15)
        assert boundary.slots.active == 1
        from app.main import app
        with TestClient(app, base_url='http://web.test') as other_client:
            other_client.cookies.set('ras_session', b.w.actor.token)
            other_client.cookies.set('ras_csrf', b.w.actor.csrf_token)
            busy = other_client.post(f'/admin/review-requests/{second_rid}/approve', json=body(),
                                     headers={'Origin': 'http://web.test', 'X-CSRF-Token': b.w.actor.csrf_token})
        assert (busy.status_code, busy.json()) == (503, {'error': 'approval_busy'})
        release.set()
        assert first.result(30).status_code == 200
    assert boundary.slots.active == 0
    assert other.key not in b.storage.opened  # the busy attempt never touched storage


@pytest.mark.parametrize('where', ('source_read', 'put'))
def test_copy_deadline_stops_observing_but_owns_resources_until_thread_settles(approvals, monkeypatch, where):
    b = approvals
    monkeypatch.setenv('RAS_FILES__APPROVAL_COPY_DEADLINE_SECONDS', '0.5')
    get_settings.cache_clear()
    settled = []

    def slow(key):
        if (where == 'source_read' and key == b.own.key) or where == 'put':
            time.sleep(2)  # a blocked SDK call: cancellation cannot kill this thread
            settled.append(time.monotonic())

    b.storage.hooks['open' if where == 'source_read' else 'put'] = slow
    started = time.monotonic()
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_copy_timeout'})
    assert settled and settled[0] - started >= 1.9 and time.monotonic() - started >= 2
    assert nothing(b) and b.storage.copies() == {} and b.storage.deleted == b.storage.puts
    assert failure_reasons(b) == ['copy_timeout'] and boundary.slots.active == 0
    assert gate(b).open and get_engine().pool.checkedout() == 0


class FakeRequest:
    def __init__(self, csrf, disconnect_after=None):
        self.headers = {'x-csrf-token': csrf}
        self.calls, self.disconnect_after = 0, disconnect_after

    async def is_disconnected(self):
        self.calls += 1
        return self.disconnect_after is not None and self.calls > self.disconnect_after


def direct(b):
    admin = Principal(b.w.uid, 'admin', b.w.actor.session_id, 'admin@example.test', None)
    return admin, reviews.ApproveInput(**body())


@pytest.mark.parametrize('after', (0, 1))
def test_client_disconnect_before_put_or_acceptance_stops_without_business_write(approvals, after):
    b = approvals
    admin, data = direct(b)
    req = FakeRequest(b.w.actor.csrf_token, disconnect_after=after)
    with pytest.raises(ApiError) as caught:
        asyncio.run(boundary.attempt(req, admin, str(b.rid), data, b.w.actor.csrf_token, b.clock, get_settings(),
                                     b.storage, threading.Event()))
    assert caught.value.code == 'approval_unavailable'
    assert nothing(b) and b.storage.copies() == {} and len(b.storage.puts) == after
    assert b.storage.deleted == b.storage.puts and boundary.slots.active == 0


async def _cancel_all_during(b, entered, release, *, cancel_attempts=True):
    admin, data = direct(b)
    task = asyncio.create_task(boundary.receive(FakeRequest(b.w.actor.csrf_token), admin, str(b.rid), data,
                                                b.clock, get_settings(), b.storage))
    while not entered.is_set():
        await asyncio.sleep(0.02)
    attempts = list(boundary._attempts)
    task.cancel()
    if cancel_attempts:
        for t in attempts:
            t.cancel()  # shutdown cancels every task, including the attempt itself
    await asyncio.sleep(0.2)
    assert boundary.slots.active == 1  # still owned while the thread runs
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*attempts, return_exceptions=True)
    assert not boundary._attempts


@pytest.mark.parametrize('cancel_attempts', (False, True))
def test_cancel_during_source_read_settles_then_releases_without_write(approvals, cancel_attempts):
    b = approvals
    entered, release = threading.Event(), threading.Event()

    def hold(key):
        if key == b.own.key:
            entered.set()
            assert release.wait(15)

    b.storage.hooks['open'] = hold
    asyncio.run(_cancel_all_during(b, entered, release, cancel_attempts=cancel_attempts))
    assert nothing(b) and b.storage.puts == [] and boundary.slots.active == 0
    assert get_engine().pool.checkedout() == 0


def test_cancel_all_before_journal_rolls_back_and_drops_key(approvals, monkeypatch):
    b = approvals
    entered, release = threading.Event(), threading.Event()
    real = boundary._gate

    def gate_(db, s, pending=None):
        if pending is not None:
            entered.set()
            assert release.wait(15)
        return real(db, s, pending)

    monkeypatch.setattr(boundary, '_gate', gate_)
    asyncio.run(_cancel_all_during(b, entered, release))
    # The stop was known before the mirror: no journal line, no business write.
    assert nothing(b) and b.storage.copies() == {} and b.storage.deleted == b.storage.puts
    assert gate(b).open and boundary.slots.active == 0


def test_cancel_all_during_commit_lets_commit_settle_and_keeps_copy(approvals, monkeypatch):
    b = approvals
    entered, release = threading.Event(), threading.Event()

    class Blocking(Session):
        def commit(self):
            entered.set()
            assert release.wait(15)
            return super().commit()

    monkeypatch.setattr(boundary, 'Session', Blocking)
    asyncio.run(_cancel_all_during(b, entered, release))
    s = state(b)
    assert s.requests[0][1] == 'approved' and len(s.common) == 1 and s.mirrors == [(b.rid, True)]
    assert len(b.storage.copies()) == 1 and b.storage.deleted == [] and boundary.slots.active == 0
    assert gate(b).open


# --- SQL routines / pending gate -----------------------------------------------------

MIRROR = text('SELECT ras_auth_review_approval_mirror(:l)')
CONFIRM = text('SELECT ras_auth_review_approval_confirm(:l)')


def admin_principal(b):
    return Principal(b.w.uid, 'admin', b.w.actor.session_id, 'admin@example.test', None)


def stage(b, db, *, variant=None, other_admin=None):
    """The acceptance DML of one approval on this runtime transaction, without journal."""
    from app.audit import record_event
    from app.models import AccessRights, PaperVersion, SourceFile
    from app.models.enums import AuditActorType, AuditOutcome
    lock_users(db, [b.w.uid])
    target = boundary._target(db, admin_principal(b), b.rid)
    rr = db.scalar(select(ReviewRequest).where(ReviewRequest.id == b.rid).with_for_update())
    key = 'originals/' + uuid.uuid4().hex
    if variant == 'old_common':
        item_id = b.old_common.item
        version_id, file_id, key = b.old_common.version, b.old_common.file, b.old_common.key
    else:
        nested = db.begin_nested() if variant == 'savepoint' else None
        item = LibraryItem(visibility='common', owner_user_id=None, publication_state='pending', title='T',
                           rights_epoch=0, created_by_user_id=b.w.uid, created_at=NOW, updated_at=NOW)
        db.add(item)
        db.flush()
        version = PaperVersion(item_id=item.id, received_at=NOW, created_at=NOW)
        db.add(version)
        db.flush()
        common = SourceFile(item_id=item.id, version_id=version.id, storage_key=key,
                            verified_mime='application/pdf' if variant == 'mime' else 'text/markdown',
                            byte_size=len(BODY) + (1 if variant == 'size' else 0), checksum_sha256=SHA,
                            file_source='upload' if variant == 'file_source' else 'review_approval', created_at=NOW)
        db.add(common)
        db.flush()
        if variant == 'extra_version':
            db.add(PaperVersion(item_id=item.id, received_at=NOW, created_at=NOW))
        if variant == 'extra_file':
            db.add(SourceFile(item_id=item.id, version_id=version.id, storage_key=key + 'x', verified_mime='text/markdown',
                              byte_size=len(BODY), checksum_sha256=SHA, file_source='review_approval'))
        if nested is not None:
            db.flush()
            nested.commit()
        item_id, version_id, file_id = item.id, version.id, common.id
    reviewer = other_admin if variant == 'reviewer' else b.w.uid
    rights = AccessRights(item_id=item_id, version_id=version_id, source_file_id=file_id, file_checksum_sha256=SHA,
        origin_kind='review_request', origin_review_request_id=rr.id, verified_source='checked',
        evidence_note='evidence', checked_on=NOW.date(), reviewer_user_id=reviewer,
        recorded_at=NOW - timedelta(seconds=1) if variant == 'recorded_at' else NOW, scope_audience='instance_users',
        permits_original_download=False, permits_fulltext_search=False, permits_external_processing=False,
        rights_epoch_at_grant=0)
    db.add(rights)
    rr.status, rr.closed_at, rr.closed_by_user_id, rr.close_reason_code = 'approved', NOW, b.w.uid, 'approved_by_admin'
    db.flush()
    record_event(db, action='review.request.approve', outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.USER,
                 actor_user_id=b.w.uid, target_type='review_request', target_id=str(rr.id), details={})
    if variant == 'epoch':
        db.execute(text('UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i'), {'i': b.own.item})
    if variant == 'stray_pending':  # an unrelated deferred violation in the same transaction
        db.add(LibraryItem(visibility='common', publication_state='pending', title='stray'))
    db.flush()
    return boundary.Approval(target, b.w.uid, NOW, item_id, version_id, file_id, rights.id, key)


def line_for(b, approval, **body_over):
    st = journal.read(b.w.path)
    e = {'body': approval.body() | body_over, 'kind': 'review_closure', 'seq': st.head.seq + 1, 'prev': st.head.hash,
         'hash': 'a' * 64}
    return journal.canonical(e)


def test_runtime_routines_mirror_false_then_confirm_once(approvals):
    b = approvals
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        st = boundary._gate(db, get_settings(), approval)
        record = journal.next_record(st.head, journal.REVIEW_CLOSURE, approval.body())
        with pytest.raises(DBAPIError):  # nothing to confirm before the mirror
            with db.begin_nested():
                db.execute(CONFIRM, {'l': record.line})
        db.execute(MIRROR, {'l': record.line})
        assert db.scalar(text('SELECT journal_confirmed FROM revocation_journal_records WHERE review_request_id=:r'),
                         {'r': b.rid}) is False
        with pytest.raises(DBAPIError):  # an approval line is never accepted twice
            with db.begin_nested():
                db.execute(MIRROR, {'l': record.line})
        journal.append(b.w.path, st, [record])
        db.execute(CONFIRM, {'l': record.line})
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.execute(CONFIRM, {'l': record.line})
        db.commit()
    with Session(b.w.runtime) as db, pytest.raises(DBAPIError):
        db.execute(CONFIRM, {'l': record.line})  # another transaction
    assert gate(b).open and state(b).requests[0][1] == 'approved'


@pytest.mark.parametrize('field', journal.CLOSURE_FIELDS)
@pytest.mark.parametrize('shape', ('missing', 'null', 'number', 'mismatch'))
def test_sql_independent_body_shape_and_value(approvals, field, shape):
    b = approvals
    before = b.w.path.read_bytes()
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        body_ = approval.body()
        if shape == 'missing':
            del body_[field]
        elif shape == 'null':
            body_[field] = None
        elif shape == 'number':
            body_[field] = 17
        else:
            body_[field] = (str(uuid.uuid4()) if field.endswith('_id') else 'withdrawn' if field == 'status'
                            else 'withdrawn_by_requester' if field == 'reason_code'
                            else (NOW + timedelta(seconds=1)).isoformat())
        st = journal.read(b.w.path)
        line = journal.canonical({'body': body_, 'kind': 'review_closure', 'seq': st.head.seq + 1, 'prev': st.head.hash,
                                  'hash': 'a' * 64})
        with pytest.raises(DBAPIError):
            db.execute(MIRROR, {'l': line})
        db.rollback()
    assert b.w.path.read_bytes() == before and state(b).requests[0][1] == 'open'


@pytest.mark.parametrize('shape', ('body_extra', 'envelope_extra', 'kind', 'seq_null', 'seq_string', 'seq_zero',
                                   'hash_null', 'hash_upper', 'prev_number', 'body_null', 'not_json_object'))
def test_sql_envelope_rejected(approvals, shape):
    b = approvals
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        st = journal.read(b.w.path)
        e = {'body': approval.body(), 'kind': 'review_closure', 'seq': st.head.seq + 1, 'prev': st.head.hash,
             'hash': 'a' * 64}
        if shape == 'body_extra':
            e['body']['common_item_id'] = str(approval.common_item_id)
        elif shape == 'envelope_extra':
            e['extra'] = True
        elif shape == 'kind':
            e['kind'] = 'session_revocation'
        elif shape in ('seq_null', 'seq_string', 'seq_zero'):
            e['seq'] = {'seq_null': None, 'seq_string': '9', 'seq_zero': 0}[shape]
        elif shape == 'hash_null':
            e['hash'] = None
        elif shape == 'hash_upper':
            e['hash'] = 'A' * 64
        elif shape == 'prev_number':
            e['prev'] = 1
        elif shape == 'body_null':
            e['body'] = None
        line = '[1]' if shape == 'not_json_object' else journal.canonical(e)
        with pytest.raises(DBAPIError):
            db.execute(MIRROR, {'l': line})
        db.rollback()
    assert state(b).requests[0][1] == 'open'


@pytest.mark.parametrize('when', ('infinity', '2026-10-07T24:00:00Z', '2026-02-30T12:00:00Z', '2026-10-07T12:00:00',
                                  '2026-10-07T12:00:60Z', '2026-10-07 12:00:00Z'))
def test_sql_rejects_invalid_time(approvals, when):
    b = approvals
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        with pytest.raises(DBAPIError):
            db.execute(MIRROR, {'l': line_for(b, approval, closed_at=when)})
        db.rollback()


@pytest.mark.parametrize('variant', ('savepoint', 'old_common', 'reviewer', 'recorded_at', 'extra_version',
                                     'extra_file', 'size', 'mime', 'file_source', 'epoch'))
def test_sql_and_python_bind_exact_tuple_new_common_rows_and_actor(approvals, variant):
    b = approvals
    other_admin = None
    if variant == 'reviewer':
        other_admin = accept(b.w, issue(b.w, email='second-admin@example.test')).id
        replica(b, "UPDATE users SET role='admin' WHERE id=:u", u=other_admin)
    if variant == 'old_common':
        # A pending copy committed earlier (privileged fixture): its rows are not this transaction's.
        with b.w.engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            item = c.scalar(text("INSERT INTO library_items(visibility,publication_state,title,created_by_user_id)"
                                 " VALUES('common','pending','old',:u) RETURNING id"), {'u': b.w.uid})
            version = c.scalar(text('INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id'), {'i': item})
            key = 'originals/' + uuid.uuid4().hex
            file = c.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                                 "checksum_sha256,file_source) VALUES(:i,:v,:k,'text/markdown',:n,:h,'review_approval')"
                                 " RETURNING id"), {'i': item, 'v': version, 'k': key, 'n': len(BODY), 'h': SHA})
        b.old_common = SimpleNamespace(item=item, version=version, file=file, key=key)
    before = b.w.path.read_bytes()
    with Session(b.w.runtime) as db:
        approval = stage(b, db, variant=variant, other_admin=other_admin)
        assert approval.waiting(db) is None  # Python binding refuses independently
        with pytest.raises(ApiError):
            boundary._gate(db, get_settings(), approval)
        db.rollback()
    with Session(b.w.runtime) as db:
        approval = stage(b, db, variant=variant, other_admin=other_admin)
        with pytest.raises(DBAPIError, match='does not bind this transaction'):
            db.execute(MIRROR, {'l': line_for(b, approval)})
        db.rollback()
    assert b.w.path.read_bytes() == before and state(b).requests[0][1] == 'open'


def test_sql_refuses_closure_committed_by_another_transaction(approvals):
    b = approvals
    with Session(b.w.engine) as db:  # privileged corruption: approval rows committed without the runtime path
        db.execute(text("SET LOCAL session_replication_role='replica'"))
        approval = stage(b, db)
        db.commit()
    with Session(b.w.runtime) as db, pytest.raises(DBAPIError, match='does not bind this transaction'):
        db.execute(MIRROR, {'l': line_for(b, approval)})


def test_deferred_violation_is_forced_before_first_journal_byte(approvals):
    b = approvals
    before = b.w.path.read_bytes()
    with Session(b.w.runtime) as db:
        approval = stage(b, db, variant='stray_pending')
        with pytest.raises(DBAPIError, match='pending common copy needs its access rights'):
            db.execute(MIRROR, {'l': line_for(b, approval)})
        db.rollback()
    assert b.w.path.read_bytes() == before


def test_legacy_withdraw_reject_routine_refuses_approval_line(approvals):
    b = approvals
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        with pytest.raises(DBAPIError, match='invalid review closure decision'):
            db.execute(text('SELECT ras_auth_review_closure_mirror(:l)'), {'l': line_for(b, approval)})
        db.rollback()


@pytest.mark.parametrize('sql', (
    "INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,"
    "review_request_id) VALUES(999,'review_closure',repeat('a',64),'{}','close','closed',true,gen_random_uuid())",
    'UPDATE revocation_journal_records SET journal_confirmed=true', 'DELETE FROM revocation_journal_records',
    'CREATE TABLE shadow(x int)', 'CREATE TEMP TABLE shadow(x int)', 'SET ROLE c1_migrator',
    'ALTER FUNCTION ras_auth_review_approval_mirror(text) OWNER TO c1_runtime',
    'CREATE OR REPLACE FUNCTION ras_auth_review_approval_confirm(p text) RETURNS void LANGUAGE sql AS $$ $$'))
def test_runtime_raw_privilege_and_fake_guc_denied(installation, sql):
    with installation.runtime.connect() as c, pytest.raises(DBAPIError):
        c.execute(text("SET LOCAL ras.is_rehash='true'"))
        c.execute(text("SET LOCAL ras.approval_bound='true'"))
        c.execute(text(sql))


def test_routine_acl_definer_and_search_path(world):
    w = world
    with w.engine.connect() as c:
        for fn in ('ras_auth_review_approval_mirror(text)', 'ras_auth_review_approval_confirm(text)'):
            assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"), {'f': fn})
            assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"), {'f': fn})
            assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a"
                                     " WHERE p.oid=to_regprocedure(:f) AND a.grantee=0)"), {'f': fn})
            row = c.execute(text("SELECT r.rolname,r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r"
                                 " ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:f)"), {'f': fn}).one()
            assert row[0] == 'c1_definer' and row[1] is False and row[2] is True
            assert 'search_path=pg_catalog, public, pg_temp' in row[3]
        assert not c.scalar(text("SELECT has_table_privilege('c1_runtime','revocation_journal_records','INSERT')"))
    with w.recovery.connect() as c, pytest.raises(DBAPIError):
        c.execute(MIRROR, {'l': '{}'})


@pytest.mark.parametrize('field', ('request_id', 'item_id', 'version_id', 'file_id', 'requester_user_id',
                                   'storage_key', 'byte_size', 'mime', 'checksum', 'epoch', 'actor_user_id',
                                   'closed_at', 'common_item_id', 'common_version_id', 'common_file_id', 'rights_id',
                                   'key'))
def test_pending_gate_subtracts_only_the_exact_staged_approval(approvals, field):
    b = approvals
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        assert boundary._gate(db, get_settings(), approval) is not None
        value = {'storage_key': 'synthetic/other', 'byte_size': 1, 'mime': 'application/pdf', 'checksum': 'b' * 64,
                 'epoch': 7, 'closed_at': NOW + timedelta(seconds=1), 'key': 'originals/' + 'b' * 32}.get(field, uuid.uuid4())
        if field in ('actor_user_id', 'closed_at', 'common_item_id', 'common_version_id', 'common_file_id',
                     'rights_id', 'key'):
            changed = replace(approval, **{field: value})
        else:
            changed = replace(approval, target=replace(approval.target, **{field: value}))
        with pytest.raises(ApiError):
            boundary._gate(db, get_settings(), changed)
        db.rollback()


@pytest.mark.parametrize('block', ('unjournaled', 'unconfirmed', 'hold', 'ahead', 'pending_cutover'))
def test_pending_approval_keeps_other_gate_failures(approvals, monkeypatch, block):
    b = approvals
    if block == 'hold':
        recovery.hold(b.w.engine, get_settings(), NOW)
    elif block == 'ahead':
        st = journal.read(b.w.path)
        journal.append(b.w.path, st, [journal.next_record(st.head, journal.RECOVERY_BEGIN, dict(
            recovery_id=str(uuid.uuid4()), started_at=NOW.isoformat(), db_head_seq=st.head.seq))])
    elif block == 'unconfirmed':
        replica(b, 'UPDATE revocation_journal_records SET journal_confirmed=false WHERE seq='
                   '(SELECT max(seq) FROM revocation_journal_records)')
    elif block == 'pending_cutover':
        from app import auth_safety
        monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'auth_safety_pending')
    with Session(b.w.runtime) as db:
        approval = stage(b, db)
        if block == 'unjournaled':
            ids = source(b.w, b.user.id, 'second source')
            replica(b, "INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,"
                       "file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,"
                       "submitted_at,closed_at,closed_by_user_id,close_reason_code) VALUES(:i,:v,:f,:u,'withdrawn',:h,0,"
                       "'t','s','r',:at,:at,:u,'withdrawn_by_requester')",
                    i=ids.item, v=ids.version, f=ids.file, u=b.user.id, h=SHA, at=NOW)
        with pytest.raises(ApiError):
            boundary._gate(db, get_settings(), approval)
        db.rollback()


# --- journal / outcome --------------------------------------------------------------

def recover(b, **kw):
    return recovery.recover(b.w.engine, SimpleNamespace(), get_settings(), NOW, **kw)


def mirrors_unconfirmed(b):
    with b.w.engine.connect() as c:
        return c.scalar(text('SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed'))


@pytest.mark.parametrize('fault', ('none', 'torn', 'full_fsync'))
def test_append_fault_known_approval_commit_mirror_false_gate_closed(approvals, monkeypatch, fault):
    b = approvals
    real = journal.append

    def broken(path, st, records):
        if fault == 'torn':
            with open(path, 'ab') as f:
                f.write(records[0].line.encode()[:30])
        elif fault == 'full_fsync':
            real(path, st, records)
        raise OSError('injected append/fsync fault')

    monkeypatch.setattr(journal, 'append', broken)
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'review_journal_unavailable'})
    s = state(b)
    assert s.requests[0][1] == 'approved' and len(s.common) == 1 and len(s.rights) == 1
    assert s.mirrors == [(b.rid, False)] and mirrors_unconfirmed(b) == 1
    assert len(b.storage.copies()) == 1 and b.storage.deleted == []
    assert not gate(b).open
    monkeypatch.setattr(journal, 'append', real)
    if fault == 'torn':  # the operator repairs the torn tail first (runbook), then recover
        st = journal.read(b.w.path)
        with open(b.w.path, 'r+b') as f:
            f.truncate(st.size)
    report = recover(b, confirm_db_current=fault != 'full_fsync')
    assert report.gate == "open" and gate(b).open
    assert state(b).requests[0][1] == 'approved' and len(state(b).common) == 1
    as_admin(b)
    assert b.client.get(old_url(b)).status_code == 404


def test_real_fsync_fault_full_line_stays_unconfirmed_until_recover(approvals, monkeypatch):
    b = approvals
    real = journal.os.fsync

    def fail(*a):
        raise OSError('injected fsync fault')

    monkeypatch.setattr(journal.os, 'fsync', fail)
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'review_journal_unavailable'})
    assert journal.read(b.w.path).head.body['status'] == 'approved'
    assert gate(b).reason == 'journal_unconfirmed'
    monkeypatch.setattr(journal.os, 'fsync', real)
    recover(b)
    assert gate(b).open and state(b).mirrors == [(b.rid, True)]


@pytest.mark.parametrize('fault', ('mirror', 'after_append', 'confirm'))
def test_phase_fault_no_dto_outcome_and_recovery_never_rebuilds_common(approvals, monkeypatch, fault):
    b = approvals
    real_append = journal.append
    if fault == 'after_append':
        def append(*a, **kw):
            real_append(*a, **kw)
            raise RuntimeError('fault after durable append')
        monkeypatch.setattr(journal, 'append', append)
    else:
        name = 'ras_auth_review_approval_mirror' if fault == 'mirror' else 'ras_auth_review_approval_confirm'

        class Faulty(Session):
            def execute(self, stmt, *a, **kw):
                if name in str(stmt):
                    raise RuntimeError('fault ' + fault)
                return super().execute(stmt, *a, **kw)
        monkeypatch.setattr(boundary, 'Session', Faulty)
    outcome = boundary.Outcome()
    admin, data = direct(b)
    with pytest.raises(ApiError) as caught:
        asyncio.run(boundary.attempt(None, admin, str(b.rid), data, b.w.actor.csrf_token, b.clock, get_settings(),
                                     b.storage, threading.Event(), outcome))
    assert caught.value.code == ('approval_unavailable' if fault == 'mirror' else 'review_closure_pending_recovery')
    assert outcome.record is not None and outcome.append_started == (fault != 'mirror')
    assert not outcome.commit_started and outcome.rollback_confirmed
    s = state(b)
    assert nothing(b, s) and s.requests[0][1] == 'open'
    # Known precommit rollback: only then is the attempt's own key removed.
    assert b.storage.copies() == {} and b.storage.deleted == b.storage.puts
    if fault == 'mirror':
        assert gate(b).open
        return
    assert gate(b).reason == 'db_behind_journal'
    monkeypatch.setattr(journal, 'append', real_append)
    recover(b)
    s = state(b)
    assert s.requests[0][1] == 'closed_by_recovery' and not s.common and not s.rights
    as_admin(b)
    assert b.client.get(old_url(b)).status_code == 404


@pytest.mark.parametrize('fault', ('written', 'none', 'full_fsync'))
def test_actor_expiry_after_append_is_auth_error_not_journal_failure(approvals, monkeypatch, fault):
    b = approvals
    real = journal.append

    def append(path, st, records):
        b.at = NOW + get_settings().auth.session_idle  # expires during the append/fsync
        if fault == 'none':
            raise OSError('nothing written')
        real(path, st, records)
        if fault == 'full_fsync':
            raise OSError('fsync failed after a full line')

    monkeypatch.setattr(journal, 'append', append)
    r = approve(b)
    assert r.status_code == 401, r.text
    s = state(b)
    assert nothing(b, s) and s.requests[0][1] == 'open' and b.storage.copies() == {}
    if fault == 'none':
        assert gate(b).open
        return
    # A durable intent may close the grant through recovery; it never makes an approval.
    assert gate(b).reason == 'db_behind_journal'
    monkeypatch.setattr(journal, 'append', real)
    recover(b)
    s = state(b)
    assert s.requests[0][1] == 'closed_by_recovery' and not s.common and not s.rights


@pytest.mark.parametrize('landed', (False, True))
@pytest.mark.parametrize('teardown', (False, True))
def test_commit_exception_uncertain_keeps_copy_no_dto_no_retry(approvals, monkeypatch, landed, teardown):
    b = approvals
    calls = []

    class Lost(Session):
        def commit(self):
            calls.append(1)
            (super().commit if landed else super().rollback)()
            raise RuntimeError('lost commit reply')

        def rollback(self):
            if teardown and calls:
                raise RuntimeError('teardown')
            return super().rollback()

        def close(self):
            if teardown and calls:
                super().invalidate()
                raise RuntimeError('teardown')
            return super().close()

    monkeypatch.setattr(boundary, 'Session', Lost)
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_outcome_uncertain'})
    assert len(calls) == 1  # never retried
    assert len(b.storage.copies()) == 1 and b.storage.deleted == []  # kept even if no SourceFile row is visible
    s = state(b)
    assert get_engine().pool.checkedout() == 0 and boundary.slots.active == 0
    if landed:
        assert s.requests[0][1] == 'approved' and len(s.common) == 1
        return
    assert nothing(b, s) and gate(b).reason == 'db_behind_journal'
    recover(b)
    assert state(b).requests[0][1] == 'closed_by_recovery' and not state(b).common


@pytest.mark.parametrize('fault', ('response', 'rollback', 'close', 'both'))
def test_known_commit_response_or_teardown_fault_keeps_rows_and_copy(approvals, monkeypatch, fault):
    b = approvals
    if fault == 'response':
        def fail(*a, **kw):
            raise RuntimeError('injected response fault')
        monkeypatch.setattr(boundary, 'respond', fail)
    else:
        class Teardown(Session):
            def rollback(self):
                if fault in ('rollback', 'both') and getattr(self, '_done', False):
                    raise RuntimeError('injected rollback fault')
                return super().rollback()

            def commit(self):
                super().commit()
                self._done = True

            def close(self):
                if fault in ('close', 'both') and getattr(self, '_done', False):
                    super().invalidate()
                    raise RuntimeError('injected close fault')
                return super().close()
        monkeypatch.setattr(boundary, 'Session', Teardown)
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_response_unavailable'}), r.text
    s = state(b)
    assert s.requests[0][1] == 'approved' and len(s.common) == 1 and len(b.storage.copies()) == 1
    assert b.storage.deleted == [] and get_engine().pool.checkedout() == 0


def test_known_commit_fresh_response_proof_detects_hold(approvals, monkeypatch):
    b = approvals
    real = boundary.respond

    def respond(*a, **kw):
        recovery.hold(b.w.engine, get_settings(), NOW)
        return real(*a, **kw)

    monkeypatch.setattr(boundary, 'respond', respond)
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'service_recovering'})
    assert state(b).requests[0][1] == 'approved' and len(b.storage.copies()) == 1


@pytest.mark.parametrize('writer', ('publish', 'revoke'))
def test_response_reflects_later_publish_or_revoke_through_current_gate(approvals, monkeypatch, writer):
    from app import publication
    from app.models.enums import RevocationReason
    b = approvals
    real = boundary.respond

    def respond(principal, approval, *a, **kw):
        if writer == 'publish':
            from tests.resource_fixtures import runtime_publish
            runtime_publish(b.w, approval.common_item_id, b.storage)
        else:
            from tests.resource_fixtures import runtime_revoke
            runtime_revoke(b.w,approval.common_item_id,clock=b.clock,reason='other')
        return real(principal, approval, *a, **kw)

    monkeypatch.setattr(boundary, 'respond', respond)
    r = approve(b)
    assert r.status_code == 200, r.text
    data = r.json()['request']
    assert data['status'] == 'approved' and data['pending_publication_id'] == str(state(b).common[0][0])
    assert state(b).common[0][1] == ('listed' if writer == 'publish' else 'revoked')


# --- races (deterministic barriers) --------------------------------------------------

def second_client(b):
    from app.main import app
    c = TestClient(app, base_url='http://web.test')
    c.cookies.set('ras_session', b.w.actor.token)
    c.cookies.set('ras_csrf', b.w.actor.csrf_token)
    return c


def post_approve(client, b):
    return client.post(f'/admin/review-requests/{b.rid}/approve', json=body(),
                       headers={'Origin': 'http://web.test', 'X-CSRF-Token': b.w.actor.csrf_token})


def pause_pending_gate(monkeypatch, entered, release, *, closure=False):
    """Pause the first writer AFTER its row locks/DML, before its journal lock."""
    real = review_writes._gate

    def gate_(db, s, pending=None):
        kind = review_writes.Closure if closure else boundary.Approval
        if isinstance(pending, kind) and not entered.is_set():
            entered.set()
            assert release.wait(15)
        return real(db, s, pending)

    monkeypatch.setattr(review_writes, '_gate', gate_)


def test_two_approvals_both_copied_one_commit_loser_key_dropped(approvals, monkeypatch):
    b = approvals
    barrier = threading.Barrier(2)
    real = boundary.accept

    def accept(*a, **kw):
        barrier.wait(15)  # both copies exist before either acceptance starts
        return real(*a, **kw)

    monkeypatch.setattr(boundary, 'accept', accept)
    with ThreadPoolExecutor(2) as pool, second_client(b) as other:
        one = pool.submit(approve, b)
        two = pool.submit(post_approve, other, b)
        out = sorted([one.result(30).status_code, two.result(30).status_code])
    assert out == [200, 409]
    assert len(b.storage.puts) == 2 and len(b.storage.copies()) == 1 and len(b.storage.deleted) == 1
    assert len(state(b).common) == 1 and len(state(b).rights) == 1
    assert len([r for r in journal.read(b.w.path).records if r.kind == journal.REVIEW_CLOSURE]) == 1


def test_second_approval_waits_on_request_lock_and_never_copies(approvals, monkeypatch):
    b = approvals
    entered, release = threading.Event(), threading.Event()
    pause_pending_gate(monkeypatch, entered, release)
    with ThreadPoolExecutor(2) as pool, second_client(b) as other:
        one = pool.submit(approve, b)
        assert entered.wait(15)
        two = pool.submit(post_approve, other, b)
        try:
            assert wait_for(b.w.engine, 'transactionid')  # its proof waits on request SHARE
        finally:
            release.set()
        assert one.result(30).status_code == 200
        assert two.result(30).json() == {'error': 'review_request_closed'}
    assert len(b.storage.puts) == 1 and len(b.storage.copies()) == 1


@pytest.mark.parametrize('closer', ('withdraw', 'reject'))
@pytest.mark.parametrize('first', ('approve', 'closer'))
def test_approval_races_withdraw_or_reject_both_orders(approvals, monkeypatch, closer, first):
    b = approvals
    entered, release = threading.Event(), threading.Event()
    pause_pending_gate(monkeypatch, entered, release, closure=first == 'closer')
    with ThreadPoolExecutor(2) as pool, second_client(b) as other:
        def close():
            if closer == 'withdraw':
                return other.post(f'/review-requests/{b.rid}/withdraw', headers={'Origin': 'http://web.test',
                    'X-CSRF-Token': b.owner_session.csrf_token}, cookies={'ras_session': b.owner_session.token,
                    'ras_csrf': b.owner_session.csrf_token})
            return other.post(f'/admin/review-requests/{b.rid}/reject', json={'reason_code': 'other'},
                              headers={'Origin': 'http://web.test', 'X-CSRF-Token': b.w.actor.csrf_token})
        one = pool.submit(approve if first == 'approve' else close, *((b,) if first == 'approve' else ()))
        assert entered.wait(15)
        two = pool.submit(close if first == 'approve' else approve, *(() if first == 'approve' else (b,)))
        try:
            assert wait_for(b.w.engine, 'transactionid')
        finally:
            release.set()
        a, z = one.result(30), two.result(30)
    approval, closing = (a, z) if first == 'approve' else (z, a)
    if first == 'approve':
        assert approval.status_code == 200 and closing.json() == {'error': 'review_request_closed'}
        assert state(b).requests[0][1] == 'approved' and len(b.storage.copies()) == 1
    else:
        assert closing.status_code == 200 and approval.json() == {'error': 'review_request_closed'}
        assert nothing(b) and b.storage.copies() == {}
    assert len([r for r in journal.read(b.w.path).records if r.kind == journal.REVIEW_CLOSURE]) == 1
    assert gate(b).open


@pytest.mark.parametrize('when', ('during_copy', 'holding_item', 'after_approval'))
def test_approval_races_epoch_bump(approvals, monkeypatch, when):
    b = approvals
    bump = 'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i'
    if when == 'during_copy':
        b.storage.hooks['put'] = lambda key: replica(b, bump, i=b.own.item)
        r = approve(b)
        assert r.json() == {'error': 'review_grant_inactive'} and nothing(b) and b.storage.copies() == {}
        return
    entered, release = threading.Event(), threading.Event()
    if when == 'holding_item':
        real = boundary.accept

        def accept(*a, **kw):
            entered.set()
            assert release.wait(15)
            return real(*a, **kw)
        monkeypatch.setattr(boundary, 'accept', accept)
        with ThreadPoolExecutor(1) as pool:
            f = pool.submit(approve, b)
            assert entered.wait(15)
            with b.w.engine.connect() as c:
                tx = c.begin()
                c.execute(text(bump), {'i': b.own.item})  # bump holds the private item row
                release.set()
                assert wait_for(b.w.engine, 'transactionid')  # acceptance waits on item SHARE
                tx.commit()
            r = f.result(30)
        assert r.json() == {'error': 'review_grant_inactive'} and nothing(b) and b.storage.copies() == {}
        return
    pause_pending_gate(monkeypatch, entered, release)
    with ThreadPoolExecutor(2) as pool:
        f = pool.submit(approve, b)
        assert entered.wait(15)
        def bumped():
            with b.w.engine.begin() as c:
                c.execute(text(bump), {'i': b.own.item})
        g = pool.submit(bumped)
        try:
            assert wait_for(b.w.engine, 'transactionid')  # the bump waits for the approval's item SHARE
        finally:
            release.set()
        assert f.result(30).status_code == 200
        g.result(30)
    assert state(b).requests[0][1] == 'approved' and state(b).common[0][4] == 0


@pytest.mark.parametrize('first', ('auth', 'approve'))
def test_approval_races_admin_session_revocation_both_orders(approvals, monkeypatch, first):
    b = approvals
    entered, release, auth_done = threading.Event(), threading.Event(), threading.Event()
    if first == 'approve':
        pause_pending_gate(monkeypatch, entered, release)
        real = boundary.respond
        monkeypatch.setattr(boundary, 'respond', lambda *a, **kw: (auth_done.wait(15), real(*a, **kw))[1])
    else:
        real_accept = boundary.accept

        def accept(*a, **kw):
            entered.set()
            assert release.wait(15)
            return real_accept(*a, **kw)
        monkeypatch.setattr(boundary, 'accept', accept)

    def auth():
        with Session(b.w.runtime) as db:
            revoke_session(db, session_id=b.w.actor.session_id, user_id=b.w.uid,
                           reason=SessionRevokeReason.USER_REVOKED, now=NOW)
            if first == 'auth':
                release.set()
                assert wait_for(b.w.engine, 'transactionid')  # acceptance waits on the User row
            commit_auth(db)
        auth_done.set()
        return 'revoked'

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(approve, b)
        assert entered.wait(15)
        two = pool.submit(auth)
        if first == 'approve':
            try:
                assert wait_for(b.w.engine, 'transactionid')  # revocation waits on the User row
            finally:
                release.set()
        r = one.result(30)
        assert two.result(30) == 'revoked'
    assert r.status_code == 401, r.text
    if first == 'auth':
        assert nothing(b) and b.storage.copies() == {}
    else:
        assert state(b).requests[0][1] == 'approved' and len(b.storage.copies()) == 1
    assert gate(b).open


@pytest.mark.parametrize('reader', ('history', 'original'))
@pytest.mark.parametrize('first', ('reader', 'approve'))
def test_approval_races_review_history_and_original(approvals, monkeypatch, reader, first):
    from app.api import original_downloads, review_publication_reads
    b = approvals
    entered, release = threading.Event(), threading.Event()
    path = f'/admin/review-requests/{b.rid}' + (f'/files/{b.own.file}/original' if reader == 'original' else '')
    if first == 'approve':
        pause_pending_gate(monkeypatch, entered, release)
    else:
        final = [False]
        real_proof = original_downloads.proof

        def proof(*a, **kw):
            final[0] = len(a) > 5 or kw.get('expected') is not None
            return real_proof(*a, **kw)
        real_caller = original_downloads._caller if reader == 'original' else review_publication_reads.final_caller

        def caller(*a, **kw):
            if (reader == 'history' or final[0]) and not entered.is_set():
                entered.set()
                assert release.wait(15)
            return real_caller(*a, **kw)
        if reader == 'original':
            monkeypatch.setattr(original_downloads, '_caller', caller)
            monkeypatch.setattr(original_downloads, 'proof', proof)
        else:
            monkeypatch.setattr(review_publication_reads, 'final_caller', caller)
    with ThreadPoolExecutor(2) as pool, second_client(b) as other:
        read = lambda: other.get(path)  # noqa: E731
        one = pool.submit(read if first == 'reader' else approve, *(() if first == 'reader' else (b,)))
        assert entered.wait(15)
        two = pool.submit(approve if first == 'reader' else read, *((b,) if first == 'reader' else ()))
        try:
            assert wait_for(b.w.engine, 'transactionid')
        finally:
            release.set()
        a, z = one.result(30), two.result(30)
    got, approval = (a, z) if first == 'reader' else (z, a)
    assert approval.status_code == 200, approval.text
    if reader == 'original':
        assert got.status_code == (200 if first == 'reader' else 404)
        if first == 'reader':
            assert got.content == BODY
    else:
        assert got.json()['request']['review_available'] == (first == 'reader')


def test_blocked_copy_holds_no_db_lock_or_connection(approvals):
    b = approvals
    entered, release = threading.Event(), threading.Event()

    def hold(key):
        if key == b.own.key:
            entered.set()
            assert release.wait(15)

    b.storage.hooks['open'] = hold
    with ThreadPoolExecutor(1) as pool, second_client(b) as other:
        f = pool.submit(approve, b)
        assert entered.wait(15)
        assert get_engine().pool.checkedout() == 0
        with b.w.engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)"
                                 " WHERE a.datname=current_database() AND a.pid<>pg_backend_pid()"
                                 " AND (l.locktype IN ('tuple','transactionid','advisory')"
                                 "  OR (l.locktype='relation' AND l.mode<>'AccessShareLock'))")) == 0
        # A closure and an auth commit both finish while the copy I/O is blocked.
        w = other.post(f'/review-requests/{b.rid}/withdraw', headers={'Origin': 'http://web.test',
            'X-CSRF-Token': b.owner_session.csrf_token}, cookies={'ras_session': b.owner_session.token,
            'ras_csrf': b.owner_session.csrf_token})
        assert w.status_code == 200, w.text
        with Session(b.w.runtime) as db:
            revoke_session(db, session_id=b.owner_session.session_id, user_id=b.user.id,
                           reason=SessionRevokeReason.USER_REVOKED, now=NOW)
            commit_auth(db)
        release.set()
        r = f.result(30)
    assert r.json() == {'error': 'review_request_closed'}  # mid-copy proof: nothing is put
    assert b.storage.puts == [] and nothing(b) and gate(b).open


# --- restore ------------------------------------------------------------------------

@pytest.mark.parametrize('snapshot', ('open', 'absent', 'approved'))
def test_snapshot_restore_never_reopens_grant_or_rebuilds_copy(writers, admin_engine, app_env, tmp_path, monkeypatch,
                                                              snapshot):
    from app.main import app
    from tests.conftest import upgrade
    from tests.test_recovery import backup
    b = writers
    b.storage = Originals(b.own)
    app.dependency_overrides[reviews._storage] = lambda: b.storage
    if snapshot != 'absent':
        opened(b)
    if snapshot == 'approved':
        assert approve(b).status_code == 200
    live = SimpleNamespace(engine=b.w.engine)
    with backup(live, admin_engine) as snap:
        with snap.engine.connect() as c:
            receipts = c.scalar(text('SELECT count(*) FROM auth_change_receipts'))
        if snapshot == 'absent':
            opened(b)
        if snapshot != 'approved':
            r = approve(b)
            assert r.status_code == 200, r.text
        copied = tmp_path / 'current-journal.jsonl'
        copied.write_bytes(b.w.path.read_bytes())
        monkeypatch.setenv('RAS_RECOVERY__JOURNAL_PATH', str(copied))
        get_settings.cache_clear()
        settings = get_settings()
        recovery.hold(snap.engine, settings, NOW)
        upgrade(snap.url)
        report = recovery.recover(snap.engine, SimpleNamespace(), settings, NOW)
        if snapshot != 'approved':
            assert report.replayed['recorded_request_absent' if snapshot == 'absent' else 'closed'] == 1
        raw = copied.read_bytes()
        recovery.recover(snap.engine, SimpleNamespace(), settings, NOW)  # idempotent
        assert len([r for r in journal.read(copied).records if r.kind == journal.REVIEW_CLOSURE]) == 1
        assert raw == copied.read_bytes()[:len(raw)]
        with snap.engine.connect() as c:
            status = c.scalar(text('SELECT status FROM review_requests WHERE id=:r'), {'r': b.rid})
            commons = c.scalar(text("SELECT count(*) FROM library_items WHERE visibility='common'"))
            rights = c.scalar(text('SELECT count(*) FROM access_rights'))
            assert c.scalar(text('SELECT count(*) FROM auth_change_receipts')) == receipts
        assert (status, commons, rights) == {'approved': ('approved', 1, 1), 'open': ('closed_by_recovery', 0, 0),
                                             'absent': (None, 0, 0)}[snapshot]
        assert recovery.evaluate_integrity(settings, snap.engine).open
        app_env(snap.url.set(username=b.w.runtime.url.username, password=b.w.runtime.url.password))
        as_admin(b)
        assert b.client.get(old_url(b)).status_code == 404
        detail = b.client.get(f'/admin/review-requests/{b.rid}')
        if snapshot == 'absent':
            assert detail.status_code == 404
        else:
            data = detail.json()['request']
            assert data['review_available'] is False and 'declaration' not in str(data)
            assert ('pending_publication_id' in data) == (snapshot == 'approved')
    # Restore and replay never touch the private original or remove the live copy.
    assert b.storage.objects[b.own.key] == BODY and len(b.storage.copies()) == 1


# --- real pending HTTP (no cutover patch) ------------------------------------------

def test_pending_http_without_cutover_patch_no_copy_dml_or_journal_change(installation):
    from app.main import app
    w = installation
    before = w.path.read_bytes()
    with w.engine.connect() as c:
        rows = c.scalar(text('SELECT (SELECT count(*) FROM library_items)+(SELECT count(*) FROM review_requests)'
                             '+(SELECT count(*) FROM audit_events)+(SELECT count(*) FROM revocation_journal_records)'))
    touched = []
    app.dependency_overrides[reviews._storage] = lambda: touched.append(1)
    try:
        with TestClient(app) as c:
            assert c.get('/healthz').status_code == 200 and c.get('/readyz').status_code == 503
            r = c.post(f'/admin/review-requests/{uuid.uuid4()}/approve', json=body(),
                       headers={'Origin': 'http://web.test'})
            assert (r.status_code, r.json()) == (503, {'error': 'service_recovering'})
    finally:
        app.dependency_overrides.pop(reviews._storage, None)
    assert w.path.read_bytes() == before and not touched
    with w.engine.connect() as c:
        assert c.scalar(text('SELECT (SELECT count(*) FROM library_items)+(SELECT count(*) FROM review_requests)'
                             '+(SELECT count(*) FROM audit_events)+(SELECT count(*) FROM revocation_journal_records)')) == rows


# --- PR #47 P2: short-proof Session teardown never escapes as HTTP 500 -----------------

TEARDOWN_FAULTS = ('none', 'rollback', 'close', 'both', 'both_before_release')


def teardown_session(fault, *, nth=None):
    """Session whose rollback/close fail (after or before doing their work) for instance ``nth`` (or all)."""
    made = []

    class Teardown(Session):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)
            self._fault = fault if nth is None or len(made) == nth else 'none'

        def rollback(self):
            if self._fault == 'both_before_release':
                raise RuntimeError('injected rollback fault before release')
            super().rollback()
            if self._fault in ('rollback', 'both'):
                raise RuntimeError('injected rollback fault')

        def close(self):
            if self._fault == 'both_before_release':
                raise RuntimeError('injected close fault before release')
            super().close()
            if self._fault in ('close', 'both'):
                raise RuntimeError('injected close fault')
    return Teardown


def raw_client(b):
    from app.main import app
    c = TestClient(app, base_url='http://web.test', raise_server_exceptions=False)
    c.cookies.set('ras_session', b.w.actor.token)
    c.cookies.set('ras_csrf', b.w.actor.csrf_token)
    return c


@pytest.mark.parametrize('rid_kind', ('valid', 'malformed'))
@pytest.mark.parametrize('fault', TEARDOWN_FAULTS)
def test_first_proof_teardown_fault_is_controlled_and_stops_before_storage(approvals, monkeypatch, rid_kind, fault):
    b = approvals
    before_journal = b.w.path.read_bytes()
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault))
    rid = b.rid if rid_kind == 'valid' else 'not-a-uuid'
    with raw_client(b) as c:
        r = c.post(f'/admin/review-requests/{rid}/approve', json=body(),
                   headers={'Origin': 'http://web.test', 'X-CSRF-Token': b.w.actor.csrf_token})
    if fault == 'none':
        assert r.status_code == (200 if rid_kind == 'valid' else 404), r.text
    else:
        assert (r.status_code, r.headers.get('content-type'), r.json()) == (
            503, 'application/json', {'error': 'approval_unavailable'}), r.text
        s = state(b)
        assert nothing(b, s) and s.requests[0][1] == 'open' and not s.audit
        assert b.storage.opened == [] and b.storage.puts == [] and b.w.path.read_bytes() == before_journal
    assert gate(b).open and boundary.slots.active == 0
    assert get_engine().pool.checkedout() == 0  # a failed release still invalidates the connection


@pytest.mark.parametrize('fault', TEARDOWN_FAULTS[1:])
def test_mid_copy_proof_teardown_fault_stops_before_put(approvals, monkeypatch, fault):
    b = approvals
    before_journal = b.w.path.read_bytes()
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=2))  # proof before the put
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_unavailable'})
    assert nothing(b) and b.storage.opened == [b.own.key] and b.storage.puts == []
    assert b.w.path.read_bytes() == before_journal and boundary.slots.active == 0
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('fault', TEARDOWN_FAULTS[1:])
def test_response_proof_teardown_fault_after_known_commit_keeps_rows_and_copy(approvals, monkeypatch, fault):
    b = approvals
    # Sessions in order: proof, mid proof, acceptance, response.
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=4))
    r = approve(b)
    assert (r.status_code, r.json()) == (503, {'error': 'approval_response_unavailable'})
    s = state(b)
    assert s.requests[0][1] == 'approved' and len(s.common) == 1 and s.mirrors == [(b.rid, True)]
    assert len(b.storage.copies()) == 1 and b.storage.deleted == []
    assert gate(b).open and boundary.slots.active == 0 and get_engine().pool.checkedout() == 0
