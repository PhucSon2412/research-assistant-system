"""One runtime revoke POST, real PG/roles/journal and synthetic MinIO originals.

Shared business setup uses the real C4 contract. Privileged replica writes below are explicitly
corruption/adversary setup, never the runtime mutation under test.
"""
import asyncio
import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, journal, publication, recovery
from app.api import common_revoke as boundary, common_publish, resource_writes
from app.api.errors import ApiError
from app.api.routes.publications import RevokeInput
from app.config import get_settings
from app.db import get_engine
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import operational_world as world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, wait_for  # noqa: F401
from tests.test_c2d5c1_common_publish import publishes, pending, publish, replica, scalar  # noqa: F401
from tests.resource_fixtures import BODY, SHA, derived, runtime_revoke


def revoke(b, *, item=None, data=None, who='admin', headers=None):
    issued = b.w.actor if who == 'admin' else b.owner_session
    b.client.cookies.set('ras_session', issued.token)
    b.client.cookies.set('ras_csrf', issued.csrf_token)
    h = {'Origin': 'http://web.test', 'X-CSRF-Token': issued.csrf_token} | (headers or {})
    return b.client.post(f'/admin/common-copies/{item or b.copy.item}/revoke',
        json=data if data is not None else dict(confirm_revoke=True, reason_code='rights_withdrawn'),
        headers={k: v for k, v in h.items() if v is not None})


def direct(b, *, outcome=None, stopped=None, body=None):
    return asyncio.run(boundary.attempt(None, b.admin, str(b.copy.item), body or RevokeInput(
        confirm_revoke=True, reason_code='rights_withdrawn'), b.w.actor.csrf_token,
        b.clock, get_settings(), stopped or threading.Event(), outcome))


def state(b):
    return scalar(b, 'SELECT publication_state FROM library_items WHERE id=:i', i=b.copy.item)


@pytest.mark.parametrize('listed', [False, True])
def test_runtime_success_atomic_fresh_history_no_storage_or_private_change(publishes, monkeypatch, listed):
    b = publishes
    if listed:
        assert publish(b).status_code == 200
    before = b.w.path.read_bytes()
    statements = []
    def sql(c, cursor, stmt, params, ctx, many): statements.append(stmt)
    event.listen(get_engine(), 'before_cursor_execute', sql)
    try:
        r = revoke(b)
    finally:
        event.remove(get_engine(), 'before_cursor_execute', sql)
    assert r.status_code == 200, r.text
    dto = r.json()['item']
    assert dto['publication_state'] == 'revoked' and dto['content_status'] == 'metadata_only'
    assert r.json()['cancelled_jobs'] == 0
    assert scalar(b, 'SELECT rights_epoch FROM library_items WHERE id=:i', i=b.copy.item) == 1
    assert scalar(b, 'SELECT active_index_generation_id FROM library_items WHERE id=:i', i=b.copy.item) is None
    assert dto['rights']['revocation_reason_code'] == 'rights_withdrawn'
    assert dto['tombstone']['prior_publication_state'] == ('listed' if listed else 'pending')
    assert scalar(b, "SELECT count(*) FROM revocation_journal_records WHERE kind='revocation' AND journal_confirmed") == 1
    assert journal.read(b.w.path).head.kind == 'revocation' and b.w.path.read_bytes().startswith(before)
    assert not b.storage.deleted and not b.storage.puts and len(b.storage.opened) == int(listed)
    assert b.raw_storage.size(b.copy.key) == len(BODY)
    assert scalar(b, 'SELECT visibility FROM library_items WHERE id=:i', i=b.own.item) == 'private'
    assert b.copy.key not in r.text and 'PRIVATE-SYNTHETIC' not in r.text
    exclusive = next(i for i, s in enumerate(statements) if 'SELECT pg_advisory_xact_lock(' in s)
    # Dependency activity has its own earlier transaction/User lock. The
    # acceptance's last User lock must precede its first resource lock too.
    user_lock = max(i for i,s in enumerate(statements[:exclusive]) if 'ras_auth_lock_users' in s)
    item_lock = next(i for i,s in enumerate(statements) if 'FOR UPDATE' in s and 'FROM library_items' in s)
    rights_lock = next(i for i,s in enumerate(statements) if 'FOR UPDATE' in s and s.startswith('SELECT access_rights.'))
    assert user_lock < item_lock < rights_lock < exclusive
    assert any('INSERT INTO deletion_tombstones' in s for s in statements[:exclusive])
    assert any('INSERT INTO audit_events' in s for s in statements[:exclusive])
    assert not any('FOR UPDATE' in s or 'FOR SHARE' in s for s in statements[exclusive:]
                   if 'pg_advisory_xact_lock_shared' not in s and 'SELECT library_items.id' not in s
                   and 'SELECT access_rights.id' not in s and 'SELECT paper_versions.id' not in s and 'SELECT source_files.id' not in s)
    second = revoke(b)
    assert (second.status_code, second.json()) == (409, {'error': 'common_copy_revoked'})
    assert scalar(b, 'SELECT count(*) FROM deletion_tombstones') == 1
    assert recovery.evaluate_integrity(get_settings(), b.w.runtime).open


@pytest.mark.parametrize('confirm', [False, None, 1, 0, 'true', '1'])
def test_literal_boolean_only(publishes, confirm):
    b = publishes; before = b.w.path.read_bytes()
    assert revoke(b, data=dict(confirm_revoke=confirm, reason_code='other')).status_code == 422
    assert state(b) == 'pending' and b.w.path.read_bytes() == before


@pytest.mark.parametrize('case', ['missing_confirm', 'reason', 'missing', 'malformed', 'private', 'user', 'csrf', 'origin',
                                  'retention_missing_confirmation', 'retention_duplicate', 'retention_nonbool'])
def test_input_and_acl_no_mutation(publishes, case):
    b = publishes; kwargs = {}; expected = 422
    data = dict(confirm_revoke=True, reason_code='other')
    basis = dict(field='title', policy_source='synthetic policy', scope='this copy', reason='synthetic basis')
    if case == 'missing_confirm': data.pop('confirm_revoke')
    elif case == 'reason': data['reason_code'] = 'bad'
    elif case in ('missing', 'malformed', 'private'):
        kwargs['item'] = uuid.uuid4() if case == 'missing' else 'bad-id' if case == 'malformed' else b.own.item
        expected = 404
    elif case == 'user': kwargs['who'] = 'user'; expected = 403
    elif case in ('csrf', 'origin'): kwargs['headers'] = {'X-CSRF-Token' if case == 'csrf' else 'Origin': 'bad'}; expected = 403
    elif case == 'retention_missing_confirmation': data['retain'] = [basis]
    elif case == 'retention_duplicate': data |= dict(retain=[basis, basis], confirm_retention_basis=True)
    else: data |= dict(retain=[basis], confirm_retention_basis=1)
    before = b.w.path.read_bytes()
    r = revoke(b, data=data, **kwargs)
    assert r.status_code == expected, r.text
    assert state(b) == 'pending' and b.w.path.read_bytes() == before


@pytest.mark.parametrize('fault', ['none', 'torn', 'full'])
@pytest.mark.parametrize('expired', [False, True])
def test_append_fault_commits_false_restriction_even_ttl_crosses(publishes, monkeypatch, fault, expired):
    b = publishes; real = journal.append; before = b.w.path.read_bytes()
    def append(path, current, records):
        if fault == 'torn':
            with path.open('ab') as f: f.write(records[0].line[:30].encode())
        if fault == 'full': real(path, current, records)
        if expired: b.at = NOW + get_settings().auth.session_idle
        raise OSError('synthetic fsync fault')
    monkeypatch.setattr(journal, 'append', append)
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='revocation_journal_unavailable'):
        direct(b, outcome=outcome)
    assert outcome.committed and outcome.append_started and not outcome.written
    assert state(b) == 'revoked'
    assert scalar(b, "SELECT count(*) FROM revocation_journal_records WHERE kind='revocation' AND NOT journal_confirmed") == 1
    gate = recovery.evaluate_integrity(get_settings(), b.w.runtime)
    assert not gate.open
    assert gate.reason == {'none':'journal_behind_db', 'torn':'journal_torn', 'full':'journal_unconfirmed'}[fault]
    if fault == 'none': assert before == b.w.path.read_bytes()


@pytest.mark.parametrize('landed', [False, True])
@pytest.mark.parametrize('teardown', ['none', 'rollback', 'close', 'both'])
def test_commit_exception_never_selects_replays_or_responds(publishes, monkeypatch, landed, teardown):
    b = publishes; calls = []
    class Lost(Session):
        def commit(self):
            if landed: super().commit()
            else: super().rollback()
            self.lost = True
            raise ApiError(401, 'session_expired')
        def rollback(self):
            if getattr(self, 'lost', False) and teardown in ('rollback', 'both'): raise RuntimeError('rollback')
            return super().rollback()
        def close(self):
            if getattr(self, 'lost', False) and teardown in ('close', 'both'): raise RuntimeError('close')
            return super().close()
    monkeypatch.setattr(boundary, 'Session', Lost)
    monkeypatch.setattr(boundary, 'respond', lambda *a: calls.append('response'))
    monkeypatch.setattr(recovery, 'replay_record', lambda *a: calls.append('replay'))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='revocation_outcome_uncertain'): direct(b, outcome=outcome)
    assert outcome.commit_started and not outcome.committed and not outcome.rollback_confirmed and calls == []
    assert state(b) == ('revoked' if landed else 'pending')
    assert journal.read(b.w.path).head.kind == 'revocation'
    assert recovery.evaluate_integrity(get_settings(), b.w.runtime).open == landed
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('phase', ['mirror', 'confirm', 'audit'])
def test_sql_fault_before_append_vs_after_durable_intent(publishes, monkeypatch, phase):
    b = publishes; before = b.w.path.read_bytes()
    def fail(c, cursor, statement, params, ctx, many):
        target = {'mirror':'ras_auth_common_revocation_mirror', 'confirm':'ras_auth_common_revocation_confirm',
                  'audit':'INSERT INTO audit_events'}[phase]
        if target in statement: raise RuntimeError('synthetic SQL fault')
    event.listen(get_engine(), 'before_cursor_execute', fail)
    try: r = revoke(b)
    finally: event.remove(get_engine(), 'before_cursor_execute', fail)
    assert r.status_code == 503, r.text
    assert r.json() == {'error':'revocation_pending_recovery' if phase == 'confirm' else 'revocation_unavailable'}
    assert state(b) == 'pending' and scalar(b, 'SELECT count(*) FROM deletion_tombstones') == 0
    assert (b.w.path.read_bytes() == before) == (phase != 'confirm')


@pytest.mark.parametrize('phase', ['preappend', 'postfsync', 'response'])
@pytest.mark.parametrize('kind', ['idle', 'absolute'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_current_clock_after_final_predicate(publishes, monkeypatch, phase, kind, offset):
    b = publishes; armed = [False]
    settings = get_settings()
    edge = NOW + (settings.auth.session_idle if kind == 'idle' else settings.auth.session_absolute)
    if kind == 'absolute':
        b.after_dependency = lambda db, ctx: replica(b, 'UPDATE sessions SET last_activity_at=:at WHERE id=:s',
            at=edge-timedelta(minutes=1), s=ctx.session_id)
    real_live = resource_writes.session_live
    def live(*a, **kw):
        result = real_live(*a, **kw)
        if armed[0]: b.at = edge + timedelta(microseconds=offset)
        return result
    monkeypatch.setattr(resource_writes, 'session_live', live)
    if phase == 'preappend':
        def arm(c, cur, stmt, params, ctx, many):
            if 'ras_auth_common_revocation_mirror' in stmt: armed[0] = True
        event.listen(get_engine(), 'after_cursor_execute', arm)
    elif phase == 'postfsync':
        real = journal.append
        def append(*a): real(*a); armed[0] = True
        monkeypatch.setattr(journal, 'append', append)
    else:
        real = boundary.respond
        def respond(*a): armed[0] = True; return real(*a)
        monkeypatch.setattr(boundary, 'respond', respond)
    try: r = revoke(b)
    finally:
        if phase == 'preappend': event.remove(get_engine(), 'after_cursor_execute', arm)
    assert r.status_code == (200 if offset < 0 else 401), r.text
    if offset >= 0:
        assert state(b) == ('revoked' if phase == 'response' else 'pending')
        assert 'item' not in r.json()
        assert (journal.read(b.w.path).head.kind == 'revocation') == (phase != 'preappend')


@pytest.mark.parametrize('phase', ['dependency', 'response'])
@pytest.mark.parametrize('case', ['role', 'logout', 'reset', 'csrf', 'hold', 'disable',
                                  'held', 'quarantine', 'binding', 'generation', 'receipt', 'head'])
def test_current_actor_and_gate_not_dependency_snapshot(publishes, monkeypatch, phase, case):
    from tests.test_c2d5c1_common_publish import change
    b = publishes
    if phase == 'dependency':
        b.after_dependency = lambda db, ctx: change(b, case)
    else:
        real = boundary.respond
        def respond(*a): change(b, case); return real(*a)
        monkeypatch.setattr(boundary, 'respond', respond)
    r = revoke(b)
    assert r.status_code == (503 if case in ('hold','receipt','head') else 403 if case in ('role', 'csrf') else 401), r.text
    assert 'item' not in r.json() and state(b) == ('revoked' if phase == 'response' else 'pending')


@pytest.mark.parametrize('field,value', [
    ('id', '00000000-0000-0000-0000-000000000001'), ('item_id', '00000000-0000-0000-0000-000000000001'), ('version_id', '00000000-0000-0000-0000-000000000001'),
    ('source_file_id', '00000000-0000-0000-0000-000000000001'), ('access_rights_id', '00000000-0000-0000-0000-000000000001'), ('recorded_by_user_id', '00000000-0000-0000-0000-000000000001'),
    ('recorded_at', (NOW+timedelta(seconds=1)).isoformat()), ('purge_due_at', (NOW+timedelta(days=3)).isoformat()),
    ('rights_epoch', 2), ('rights_epoch', None), ('rights_epoch', True), ('rights_epoch', '1'),
    ('storage_key', 'originals/'+'a'*32), ('storage_key', None), ('prior_publication_state', 'listed'),
    ('reason_code', 'rights_expired'), ('reason_code', 'bad'), ('target_kind', 'private'), ('version_id', None),
    ('retentions', [dict(field='title', policy_source='x', scope='y', reason='z', confirmed_by_user_id='00000000-0000-0000-0000-000000000001', recorded_at=NOW.isoformat())]),
    ('retentions', None), ('extra', 'forbidden'),
])
def test_independent_sql_rejects_mismatched_or_malformed_body_before_byte(publishes, monkeypatch, field, value):
    from dataclasses import replace
    import json
    b = publishes; before = b.w.path.read_bytes(); real = journal.next_record
    def record(*a):
        valid = real(*a); envelope = json.loads(valid.line)
        envelope['body'][field] = value
        envelope['hash'] = journal._digest(envelope['seq'], envelope['kind'], envelope['body'], envelope['prev'])
        return replace(valid, line=journal.canonical(envelope))
    monkeypatch.setattr(journal, 'next_record', record)
    r = revoke(b)
    assert (r.status_code, r.json()) == (503, {'error':'revocation_unavailable'}), r.text
    assert b.w.path.read_bytes() == before and state(b) == 'pending'


@pytest.mark.parametrize('case', ['kind', 'null_line', 'envelope_type', 'seq', 'hash', 'prev', 'extra'])
def test_sql_envelope_validation(publishes, monkeypatch, case):
    from dataclasses import replace
    import json
    b = publishes; real = journal.next_record; before = b.w.path.read_bytes()
    def record(*a):
        valid = real(*a); e = json.loads(valid.line)
        if case == 'null_line': line = None
        elif case == 'envelope_type': line = '[]'
        else:
            e[case] = dict(kind='review_closure', seq=True, hash=None, prev=1, extra='x')[case]
            line = journal.canonical(e)
        return replace(valid, line=line)
    monkeypatch.setattr(journal, 'next_record', record)
    r = revoke(b)
    assert r.status_code == 503 and state(b) == 'pending' and b.w.path.read_bytes() == before


def test_false_mirror_uncommitted_is_not_confirmable_by_other_transaction(publishes, monkeypatch):
    b = publishes; saved = []
    def fault(path, state_, records): saved.extend(records); raise OSError('fsync')
    monkeypatch.setattr(journal, 'append', fault)
    assert revoke(b).json() == {'error':'revocation_journal_unavailable'}
    for routine in ('mirror', 'confirm'):
        with b.w.runtime.connect() as c, pytest.raises(DBAPIError):
            c.execute(text(f'SELECT ras_auth_common_revocation_{routine}(:l)'), {'l': saved[0].line})
    with b.w.runtime.begin() as c:
        c.execute(text('SELECT ras_auth_confirm(:seq)'),{'seq':saved[0].seq})
    assert scalar(b, "SELECT count(*) FROM revocation_journal_records WHERE kind='revocation' AND NOT journal_confirmed") == 1


def teardown_session(fault, nth=1):
    made = []
    class Fault(Session):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw); made.append(self); self.fail = len(made) == nth
        def rollback(self):
            if self.fail and fault in ('rollback_before','both_before'): raise RuntimeError('rollback before')
            super().rollback()
            if self.fail and fault == 'rollback_after': raise RuntimeError('rollback after')
        def close(self):
            if self.fail and fault in ('close_before','both_before'): raise RuntimeError('close before')
            super().close()
            if self.fail and fault == 'close_after': raise RuntimeError('close after')
    return Fault


@pytest.mark.parametrize('fault', ['rollback_before', 'close_before', 'both_before', 'rollback_after', 'close_after'])
@pytest.mark.parametrize('phase', ['accept', 'response'])
def test_teardown_failure_is_controlled_no_dto_no_leak(publishes, monkeypatch, fault, phase):
    b = publishes
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=1 if phase == 'accept' else 2))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='revocation_response_unavailable'): direct(b, outcome=outcome)
    assert outcome.committed and outcome.teardown_failed and state(b) == 'revoked'
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('fault', ['rollback_before', 'close_before', 'both_before'])
def test_false_commit_keeps_journal_error_primary_over_teardown(publishes, monkeypatch, fault):
    b = publishes
    monkeypatch.setattr(boundary, 'Session', teardown_session(fault, nth=1))
    monkeypatch.setattr(journal, 'append', lambda *a: (_ for _ in ()).throw(OSError('fsync')))
    outcome = boundary.Outcome()
    with pytest.raises(ApiError, match='revocation_journal_unavailable'): direct(b, outcome=outcome)
    assert outcome.committed and outcome.teardown_failed and not outcome.written
    assert state(b) == 'revoked' and not recovery.evaluate_integrity(get_settings(), b.w.runtime).open


@pytest.mark.parametrize('case', ['no_rights', 'missing_file', 'wrong_version', 'wrong_checksum', 'epoch'])
def test_corrupt_target_controlled_without_journal(publishes, case):
    b = publishes
    sql = {'no_rights':'DELETE FROM access_rights WHERE item_id=:i',
           'missing_file':'DELETE FROM source_files WHERE id=:f',
           'wrong_version':'UPDATE access_rights SET version_id=:v WHERE item_id=:i',
           'wrong_checksum':"UPDATE source_files SET checksum_sha256=repeat('a',64) WHERE id=:f",
           'epoch':'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i'}[case]
    replica(b, sql, i=b.copy.item, f=b.copy.file, v=b.own.version)
    before = b.w.path.read_bytes(); r = revoke(b)
    # ACL reviewed-copy clause deliberately hides a row missing all rights.
    assert r.status_code == (404 if case == 'no_rights' else 409), r.text
    assert b.w.path.read_bytes() == before and state(b) == 'pending'


@pytest.mark.parametrize('field,value',[('field','source_url'),('scope','another copy'),
    ('reason','another reason'),('policy_source','another policy'),('confirmed_by_user_id','00000000-0000-0000-0000-000000000001'),
    ('recorded_at',(NOW+timedelta(seconds=1)).isoformat()),('scope',None)])
def test_sql_retention_basis_exact_values_before_append(publishes,monkeypatch,field,value):
    from dataclasses import replace
    import json
    b=publishes;before=b.w.path.read_bytes();real=journal.next_record
    def record(*a):
        valid=real(*a);envelope=json.loads(valid.line)
        envelope['body']['retentions'][0][field]=value
        envelope['hash']=journal._digest(envelope['seq'],envelope['kind'],envelope['body'],envelope['prev'])
        return replace(valid,line=journal.canonical(envelope))
    monkeypatch.setattr(journal,'next_record',record)
    r=revoke(b,data=dict(confirm_revoke=True,reason_code='rights_withdrawn',confirm_retention_basis=True,
        retain=[dict(field='title',policy_source='synthetic policy',scope='this copy',reason='synthetic reason')]))
    assert r.status_code==503 and state(b)=='pending' and b.w.path.read_bytes()==before


def test_actual_audit_foreign_key_fails_before_any_journal_byte(publishes,monkeypatch):
    from dataclasses import replace
    b=publishes;before=b.w.path.read_bytes();real=publication._audit
    def invalid(db,principal,*a,**kw):
        return real(db,replace(principal,user_id=uuid.uuid4()),*a,**kw)
    monkeypatch.setattr(publication,'_audit',invalid)
    r=revoke(b)
    assert r.status_code==503 and state(b)=='pending' and b.w.path.read_bytes()==before
    assert scalar(b,'SELECT count(*) FROM deletion_tombstones')==0


def test_late_formatter_denial_rechecks_current_actor(publishes,monkeypatch):
    from tests.test_c2d5c1_common_publish import change
    from app.api.routes import publications
    b=publishes
    def deny(*a):
        change(b,'role')
        raise ApiError(404,'not_found')
    monkeypatch.setattr(publications,'_copy_out',deny)
    r=revoke(b)
    assert (r.status_code,r.json())==(403,{'error':'forbidden'})
    assert state(b)=='revoked'


@pytest.mark.parametrize('case', ['missing_object', 'corrupt_object', 'conditions', 'no_permits', 'indexed'])
def test_revoke_needs_no_content_or_publish_eligibility(publishes, case):
    b = publishes
    if case == 'missing_object': b.raw_storage.delete(b.copy.key)
    elif case == 'corrupt_object':
        import io
        b.raw_storage.put(b.copy.key, io.BytesIO(b'x'*len(BODY)), 'text/markdown')
    elif case == 'conditions': replica(b, "UPDATE access_rights SET scope_conditions='synthetic condition' WHERE item_id=:i", i=b.copy.item)
    elif case == 'no_permits': replica(b, 'UPDATE access_rights SET permits_original_download=false,permits_fulltext_search=false,permits_external_processing=false WHERE item_id=:i', i=b.copy.item)
    else:
        assert publish(b).status_code == 200
        replica(b, "UPDATE library_items SET content_status='indexed',active_version_id=:v WHERE id=:i", i=b.copy.item, v=b.copy.version)
    opened = list(b.storage.opened)
    r = revoke(b)
    assert r.status_code == 200, r.text
    assert b.storage.opened == opened and b.storage.deleted == b.storage.puts == []
    assert r.json()['item']['content_status'] == 'metadata_only'


@pytest.mark.parametrize('retain', [False, True])
def test_cleanup_wins_before_new_response_scrubbed_and_count_from_attempt(publishes, monkeypatch, retain):
    b = publishes
    derived(b.w, b.copy, 'SYNTHETIC COMMON DERIVED')
    with b.w.engine.begin() as c:
        c.execute(text("INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,source_file_id,rights_epoch_at_start,idempotency_key,state)"
            " VALUES('promotion','common',:i,:v,:f,0,:k,'received')"),
            dict(i=b.copy.item,v=b.copy.version,f=b.copy.file,k='00000000-0000-0000-0000-000000000001'))
    real = boundary.respond
    def respond(*a):
        result = publication.operator_cleanup_revoked(b.w.engine, b.raw_storage, NOW+timedelta(days=2), 10)
        assert sum(result.values()) == 1
        return real(*a)
    monkeypatch.setattr(boundary, 'respond', respond)
    data = dict(confirm_revoke=True,reason_code='other')
    if retain: data |= dict(retain=[dict(field='title',policy_source='synthetic policy',scope='this copy',reason='synthetic basis')],confirm_retention_basis=True)
    r = revoke(b,data=data)
    assert r.status_code == 200, r.text
    dto = r.json()['item']
    assert r.json()['cancelled_jobs'] == 1  # cleanup finished the job; this count is attempt-owned
    assert (dto['title'] is not None) == retain
    assert dto['file']['filename'] is None and dto['rights']['evidence_note'] is None
    assert dto['tombstone']['purged_at'] and dto['tombstone']['retained_fields'] == (['title'] if retain else [])
    assert scalar(b,'SELECT count(*) FROM chunks WHERE item_id=:i',i=b.copy.item) == 0
    assert scalar(b,"SELECT count(*) FROM ingestion_jobs WHERE item_id=:i AND state='cancelled'",i=b.copy.item) == 1
    assert b.approval_storage.objects[b.own.key] == BODY


def test_two_runtime_revokes_one_epoch_and_one_journal_with_lock_wait(publishes, monkeypatch):
    b = publishes; entered, release = threading.Event(), threading.Event()
    real = publication._cancel_jobs; once = []
    def pause(*a):
        if not once:
            once.append(1); entered.set(); assert release.wait(15)
        return real(*a)
    monkeypatch.setattr(publication, '_cancel_jobs', pause)
    def run():
        try: return direct(b)['item']['publication_state']
        except ApiError as e: return e.code
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run); assert entered.wait(10)
        second = pool.submit(run)
        try: assert wait_for(b.w.engine, 'transactionid')
        finally: release.set()
        assert sorted([first.result(20), second.result(20)]) == ['common_copy_revoked', 'revoked']
    assert scalar(b, "SELECT count(*) FROM revocation_journal_records WHERE kind='revocation'") == 1
    assert scalar(b, 'SELECT rights_epoch FROM library_items WHERE id=:i', i=b.copy.item) == 1


@pytest.mark.parametrize('lock', ['user', 'item', 'rights', 'journal'])
@pytest.mark.parametrize('expired', [False, True])
def test_current_clock_after_real_row_or_journal_lock_wait(publishes, monkeypatch, lock, expired):
    from app.identity.predicates import lock_users
    b = publishes; entered, release = threading.Event(), threading.Event(); real = boundary.accept
    def accept(*a): entered.set(); assert release.wait(15); return real(*a)
    monkeypatch.setattr(boundary, 'accept', accept)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(revoke, b); assert entered.wait(10)
        with Session(b.w.runtime) as blocker:
            if lock == 'user': lock_users(blocker, [b.w.uid])
            elif lock == 'journal': blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'), {'k':recovery.JOURNAL_LOCK})
            else:
                table = 'library_items' if lock == 'item' else 'access_rights'
                column = 'id' if lock == 'item' else 'item_id'
                blocker.execute(text(f'SELECT id FROM {table} WHERE {column}=:i FOR SHARE'), {'i':b.copy.item})
            release.set()
            assert wait_for(b.w.engine, 'advisory' if lock == 'journal' else 'transactionid')
            if expired: b.at = NOW+get_settings().auth.session_idle
            blocker.rollback()
        r = future.result(20)
    assert r.status_code == (401 if expired else 200), r.text
    assert state(b) == ('pending' if expired else 'revoked')


@pytest.mark.parametrize('first', ['auth', 'revoke'])
def test_session_writer_races_revoke_no_deadlock_new_response_proof(publishes, monkeypatch, first):
    from app.identity.sessions import revoke_session
    from app.identity.session_commit import commit_auth
    from app.models.enums import SessionRevokeReason
    b = publishes; entered, release, auth_done = threading.Event(), threading.Event(), threading.Event()
    if first == 'revoke':
        real = publication._cancel_jobs
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(publication, '_cancel_jobs', hold)
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
        one = pool.submit(revoke,b); assert entered.wait(10)
        two = pool.submit(auth)
        if first == 'revoke':
            try: assert wait_for(b.w.engine,'transactionid')
            finally: release.set()
        r = one.result(30); two.result(30)
    assert r.status_code == 401 and state(b) == ('revoked' if first == 'revoke' else 'pending'),r.text


@pytest.mark.parametrize('first', ['worker', 'revoke'])
def test_worker_item_job_checkpoint_order_and_late_write_fence(publishes, monkeypatch, first):
    b = publishes; entered, release = threading.Event(), threading.Event()
    with b.w.engine.begin() as c:
        job = c.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,source_file_id,rights_epoch_at_start,idempotency_key,state)"
            " VALUES('promotion','common',:i,:v,:f,0,:k,'received') RETURNING id"),
            dict(i=b.copy.item,v=b.copy.version,f=b.copy.file,k='00000000-0000-0000-0000-000000000001'))
    if first == 'revoke':
        real = publication._cancel_jobs
        def hold(*a): entered.set(); assert release.wait(15); return real(*a)
        monkeypatch.setattr(publication, '_cancel_jobs', hold)
    def worker():
        # Legacy worker checkpoint protocol only; not a new runtime worker boundary.
        with Session(b.w.engine) as d:
            d.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'),{'i':b.copy.item})
            d.execute(text('SELECT id FROM ingestion_jobs WHERE id=:j FOR UPDATE'),{'j':job})
            if first == 'worker': entered.set(); assert release.wait(15)
            cancelled = d.scalar(text('SELECT cancel_requested FROM ingestion_jobs WHERE id=:j'),{'j':job})
            d.rollback(); return cancelled
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(worker) if first == 'worker' else pool.submit(direct,b)
        assert entered.wait(10)
        z = pool.submit(direct,b) if first == 'worker' else pool.submit(worker)
        try: assert wait_for(b.w.engine,'transactionid')
        finally: release.set()
        a.result(20); result = z.result(20)
        if first == 'revoke': assert result is True
    assert state(b)=='revoked'
    with Session(b.w.engine) as db, pytest.raises(DBAPIError):
        from app.models import MetadataCandidate
        db.add(MetadataCandidate(item_id=b.copy.item,version_id=b.copy.version,source_file_id=b.copy.file,
            field_name='title',value='late',locator_kind='markdown',start_offset=0,end_offset=4,pipeline_version='synthetic'))
        db.flush()


@pytest.mark.parametrize('retain', [False, True])
@pytest.mark.parametrize('snapshot', ['present', 'absent'])
def test_restore_current_intent_retention_cleanup_private_intact_idempotent(publishes, admin_engine, tmp_path, monkeypatch, snapshot, retain):
    from types import SimpleNamespace
    from tests.test_recovery import backup
    b = publishes
    assert publish(b).status_code == 200
    derived(b.w,b.copy,'DERIVED COMMON')
    with b.w.engine.begin() as c:
        job = c.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,source_file_id,rights_epoch_at_start,idempotency_key,state)"
            " VALUES('promotion','common',:i,:v,:f,0,:k,'received') RETURNING id"),
            dict(i=b.copy.item,v=b.copy.version,f=b.copy.file,k=str(uuid.uuid4())))
    with backup(SimpleNamespace(engine=b.w.engine),admin_engine) as snap:
        if snapshot == 'absent':
            with snap.engine.begin() as c:
                c.execute(text("SET LOCAL session_replication_role='replica'"))
                for table in ('reference_entries','chunks','sections','metadata_candidates','pdf_pages','ingestion_jobs','common_publications','access_rights','source_files','paper_versions'):
                    c.execute(text(f'DELETE FROM {table} WHERE item_id=:i'),{'i':b.copy.item})
                c.execute(text('DELETE FROM library_items WHERE id=:i'),{'i':b.copy.item})
        bases = [dict(field='title',policy_source='synthetic policy',scope='this copy',reason='synthetic basis')] if retain else []
        runtime_revoke(b.w,b.copy.item,clock=b.clock,retain=bases)
        current = tmp_path/'restored-current.jsonl';current.write_bytes(b.w.path.read_bytes())
        monkeypatch.setenv('RAS_RECOVERY__JOURNAL_PATH',str(current));get_settings.cache_clear();settings=get_settings()
        assert not recovery.evaluate_integrity(settings,snap.engine).open
        recovery.hold(snap.engine,settings,NOW)
        report = recovery.recover(snap.engine,b.raw_storage,settings,NOW)
        assert report.reconciliation_complete and report.safety_ready
        assert recovery.evaluate_integrity(settings,snap.engine).open
        with snap.engine.connect() as c:
            tomb = c.execute(text('SELECT rights_epoch,reason_code,storage_key,purged_at FROM deletion_tombstones WHERE item_id=:i'),{'i':b.copy.item}).one()
            assert tomb[0:3] == (1,'rights_withdrawn',b.copy.key) and tomb[3]
            # Existing recovery contract: absent item has no metadata to retain;
            # the full basis remains in the mirrored journal, not synthesized rows.
            assert c.scalar(text('SELECT count(*) FROM revocation_metadata_retentions WHERE item_id=:i'),{'i':b.copy.item})==int(retain and snapshot=='present')
            mirrored_bases=c.scalar(text("SELECT line::jsonb->'body'->'retentions' FROM revocation_journal_records WHERE kind='revocation'"))
            assert len(mirrored_bases)==int(retain)
            if retain: assert mirrored_bases[0]['policy_source']=='synthetic policy'
            assert c.scalar(text('SELECT checksum_sha256 FROM source_files WHERE id=:f'),{'f':b.own.file})==SHA
            if snapshot == 'present':
                assert c.scalar(text('SELECT publication_state FROM library_items WHERE id=:i'),{'i':b.copy.item})=='revoked'
                assert (c.scalar(text('SELECT title FROM library_items WHERE id=:i'),{'i':b.copy.item}) is not None)==retain
                assert c.execute(text('SELECT cancel_requested,state FROM ingestion_jobs WHERE id=:j'),{'j':job}).one()==(True,'cancelled')
            else:
                assert c.scalar(text('SELECT count(*) FROM ingestion_jobs WHERE id=:j'),{'j':job})==0
            assert c.scalar(text('SELECT count(*) FROM chunks WHERE item_id=:i'),{'i':b.copy.item})==0
        report = recovery.recover(snap.engine,b.raw_storage,settings,NOW)
        assert report.reconciliation_complete and report.safety_ready
    assert b.approval_storage.objects[b.own.key]==BODY


@pytest.mark.parametrize('first',['cleanup','response'])
def test_cleanup_tombstone_item_vs_response_share_real_wait_no_upgrade(publishes,monkeypatch,first):
    b=publishes;outcome=boundary.Outcome();settings=get_settings()
    boundary.accept(b.admin,str(b.copy.item),RevokeInput(confirm_revoke=True,reason_code='other'),
        b.w.actor.csrf_token,b.clock,settings,threading.Event(),outcome)
    assert outcome.committed
    entered,release=threading.Event(),threading.Event()
    if first=='cleanup':
        real=publication._remove_derived
        def held(*a):entered.set();assert release.wait(15);return real(*a)
        monkeypatch.setattr(publication,'_remove_derived',held)
    else:
        real=common_publish._lock_item
        def held(*a):
            result=real(*a);entered.set();assert release.wait(15);return result
        monkeypatch.setattr(common_publish,'_lock_item',held)
    def cleanup():return publication.operator_cleanup_revoked(b.w.engine,b.raw_storage,NOW+timedelta(days=2),10)
    def response():return boundary.respond(b.admin,b.copy.item,b.w.actor.csrf_token,b.clock,settings,outcome)
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(cleanup if first=='cleanup' else response)
        assert entered.wait(10)
        z=pool.submit(response if first=='cleanup' else cleanup)
        try:assert wait_for(b.w.engine,'transactionid')
        finally:release.set()
        ar,zr=a.result(20),z.result(20)
    dto=zr['item'] if first=='cleanup' else ar['item']
    assert (dto['title'] is None)==(first=='cleanup')
    assert scalar(b,'SELECT title FROM library_items WHERE id=:i',i=b.copy.item) is None
    assert scalar(b,'SELECT rights_epoch FROM library_items WHERE id=:i',i=b.copy.item)==1
    assert get_engine().pool.checkedout()==0


def test_pending_http_unpatched_gate_no_mutation(installation):
    from fastapi.testclient import TestClient
    from app.main import app
    before=installation.path.read_bytes()
    with TestClient(app) as c:
        r=c.post(f'/admin/common-copies/{uuid.uuid4()}/revoke',json=dict(confirm_revoke=True,reason_code='other'),headers={'Origin':'http://web.test'})
        assert r.status_code==503 and r.json()=={'error':'service_recovering'}
        assert c.get('/healthz').status_code==200 and c.get('/readyz').status_code==503
    assert installation.path.read_bytes()==before
    with installation.engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM deletion_tombstones'))==0


@pytest.mark.parametrize('guard', ['python', 'sql'])
def test_savepoint_subtransaction_xmin_fails_closed(publishes,monkeypatch,guard):
    b=publishes;before=b.w.path.read_bytes()
    class Nested(Session):
        def __init__(self,*a,**kw): super().__init__(*a,**kw);self.begin_nested()
    monkeypatch.setattr(boundary,'Session',Nested)
    if guard=='sql': monkeypatch.setattr(boundary,'_binding',lambda *a:None)
    r=revoke(b)
    assert r.status_code==(409 if guard=='python' else 503) and state(b)=='pending' and b.w.path.read_bytes()==before,r.text


@pytest.mark.parametrize('phase',['before_intent','during_append'])
@pytest.mark.parametrize('cancel',['attempt','receive','disconnect'])
def test_cancel_keeps_thread_owned_until_settle_restriction_after_append(publishes,monkeypatch,phase,cancel):
    b=publishes;entered,release=threading.Event(),threading.Event();outcome=boundary.Outcome()
    if phase=='before_intent':
        real=boundary._binding;once=[]
        def hold(*a):
            result=real(*a)
            if not once:once.append(1);entered.set();assert release.wait(15)
            return result
        monkeypatch.setattr(boundary,'_binding',hold)
    else:
        real=journal.append
        def hold(*a):entered.set();assert release.wait(15);return real(*a)
        monkeypatch.setattr(journal,'append',hold)
    class Request:
        headers={'x-csrf-token':b.w.actor.csrf_token}
        async def is_disconnected(self):return cancel=='disconnect'
    async def run():
        stop=threading.Event()
        if cancel=='receive': task=asyncio.create_task(boundary.receive(Request(),b.admin,str(b.copy.item),RevokeInput(confirm_revoke=True,reason_code='other'),b.clock,get_settings()))
        else: task=asyncio.create_task(boundary.attempt(Request(),b.admin,str(b.copy.item),RevokeInput(confirm_revoke=True,reason_code='other'),b.w.actor.csrf_token,b.clock,get_settings(),stop,outcome))
        assert await asyncio.to_thread(entered.wait,10)
        if cancel in ('attempt','receive'):
            task.cancel()
            # Deliver cancellation before releasing the held executor. receive
            # sets its private stop event synchronously on cancellation.
            await asyncio.sleep(0)
        else:stop.set()
        # Barrier proves the thread still holds its transaction before settle.
        assert get_engine().pool.checkedout()==1 and not outcome.committed
        release.set()
        if cancel in ('attempt','receive'):
            with pytest.raises(asyncio.CancelledError):await task
            await asyncio.gather(*list(boundary._attempts),return_exceptions=True)
        else:
            with pytest.raises(ApiError):await task
    asyncio.run(run())
    assert state(b)==('pending' if phase=='before_intent' else 'revoked')
    assert (journal.read(b.w.path).head.kind=='revocation')==(phase=='during_append')
    assert get_engine().pool.checkedout()==0 and not boundary._attempts
