"""C2d-5e2 manual orphan original reconcile: actual runtime login, PostgreSQL, MinIO and journal.

Only business cutover is scoped (``world``); a separate test keeps the real
pending gate. Owner-engine statements are labelled setup, corruption or
adversary. Storage faults are injections over real local MinIO, not crashes.
The environment is quiescent by construction: no upload/approval runs while
a pass runs unless the test names that race explicitly.
"""
import hashlib
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, recovery
from app.config import get_settings
from app.db import get_engine
from app.ingest import orphans, runtime
from app.storage import ListedObject, get_storage
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, wait_for  # noqa: F401
from tests.resource_fixtures import BODY

FENCE = 'ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz)'
COMPLETE = 'ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz)'
REAL_CUTOVER = auth_safety.cutover  # captured at import, before any scoped fixture patch
GRACE = timedelta(hours=24)
BUSINESS = ('library_items', 'paper_versions', 'source_files', 'access_rights', 'ingestion_jobs', 'embedding_calls',
            'deletion_tombstones', 'object_delete_fences', 'common_publications', 'review_requests', 'upload_intents')


def s3_error(code, status, op='HeadObject'):
    return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, op)


FAULTS = {'403': lambda: s3_error('AccessDenied', 403), '500': lambda: s3_error('InternalError', 500),
          'timeout': lambda: ConnectTimeoutError(endpoint_url='http://127.0.0.1')}


class Watched:
    """The real bucket; records each object call with the runtime pool state at that moment."""

    def __init__(self, raw):
        self.raw, self.calls, self.hooks, self.faults, self.forbidden = raw, [], {}, {}, []

    def _call(self, name, key, action):
        self.calls.append((name, key, get_engine().pool.checkedout()))
        hook = self.hooks.get(name)
        if hook:
            hook(key)
        queued = self.faults.get(name)
        if queued:
            fault = queued.pop(0)
            if fault is not None:
                raise fault
        return action()

    def list_originals(self, *, start_after, max_keys):
        return self._call('list', start_after, lambda: self.raw.list_originals(start_after=start_after,
                                                                               max_keys=max_keys))

    def head(self, key):
        return self._call('head', key, lambda: self.raw.head(key))

    def delete(self, key):
        return self._call('delete', key, lambda: self.raw.delete(key))

    def __getattr__(self, name):  # put/copy/open/ensure_bucket/...: reconcile never writes or reads bytes
        self.forbidden.append(name)
        raise AssertionError('storage call not allowed: ' + name)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def orphan(writers, monkeypatch):
    import os
    b = writers
    assert all(os.environ.get(k) for k in ('MINIO_ROOT_USER', 'MINIO_ROOT_PASSWORD', 'MINIO_API_HOST_PORT'))
    monkeypatch.setenv('RAS_STORAGE__ENDPOINT_URL', 'http://127.0.0.1:' + os.environ['MINIO_API_HOST_PORT'])
    monkeypatch.setenv('RAS_STORAGE__ACCESS_KEY', os.environ['MINIO_ROOT_USER'])
    monkeypatch.setenv('RAS_STORAGE__SECRET_KEY', os.environ['MINIO_ROOT_PASSWORD'])
    monkeypatch.setenv('RAS_STORAGE__BUCKET', 'ras-c2d5e2-' + uuid.uuid4().hex)
    get_settings.cache_clear()
    b.settings = get_settings()
    b.raw = get_storage(b.settings)
    b.raw.ensure_bucket()  # labelled setup: the test creates its bucket; reconcile never does
    b.watch = Watched(b.raw)
    b.shift = timedelta(days=2)  # every object created now is older than the 24 h grace
    monkeypatch.setattr(orphans, 'utcnow', lambda: datetime.now(UTC) + b.shift)
    assert b.settings.files.orphan_grace == GRACE
    yield b
    assert get_engine().pool.checkedout() == 0
    for key, _ in b.raw.orphan_keys(''):
        b.raw.delete(key)
    b.raw.client.delete_bucket(Bucket=b.raw.bucket)


def put(b, key=None, data=BODY):
    key = key or 'originals/' + uuid.uuid4().hex
    b.raw.client.put_object(Bucket=b.raw.bucket, Key=key, Body=data, ContentType='text/markdown')
    return key


def present(b, key):
    try:
        b.raw.size(key)
        return True
    except ClientError:
        return False


def body(b, key):
    stream = b.raw.open(key)
    try:
        return stream.read()
    finally:
        stream.close()


def q(b, statement, **params):
    """Labelled owner read/setup; never the reconcile under test."""
    with b.w.engine.begin() as c:
        r = c.execute(text(statement), params)
        return r.scalar() if r.returns_rows else None


def fences(b):
    with b.w.engine.connect() as c:
        return c.execute(text('SELECT storage_key,observed_size FROM orphan_object_fences ORDER BY storage_key')).all()


def completions(b):
    with b.w.engine.connect() as c:
        return c.execute(text('SELECT f.storage_key,c.outcome FROM orphan_object_completions c'
                              ' JOIN orphan_object_fences f ON f.id=c.fence_id ORDER BY f.storage_key')).all()


def audits(b):
    return q(b, "SELECT count(*) FROM audit_events WHERE action='original.orphan_reconcile'")


def business(b):
    with b.w.engine.connect() as c:
        return {t: sorted(tuple(map(str, r)) for r in c.execute(text(f'SELECT * FROM {t}')).all()) for t in BUSINESS}


def run(b, *, apply=True, engine=None, storage=None, settings=None):
    return orphans.reconcile_originals(engine or get_engine(), storage or b.watch, settings or b.settings, apply=apply)


def bounded(b, **over):
    s = b.settings.model_copy(deep=True)
    for k, v in over.items():
        setattr(s.files, k, v)
    return s


def refer(b, key, holder, *, data=BODY):
    """Labelled setup: a SourceFile row naming ``key`` in one holder shape, inserted by the owner."""
    h = hashlib.sha256(data).hexdigest()
    with b.w.engine.begin() as c:
        if holder in ('private_a', 'private_b'):
            owner = b.user.id if holder == 'private_a' else b.w.uid
            i = c.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'ref')"
                              ' RETURNING id'), {'o': owner})
        elif holder in ('pending', 'listed', 'revoked'):
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            i = c.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common',:s,'ref')"
                              ' RETURNING id'), {'s': holder})
        else:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            i = None if holder == 'no_item' else uuid.uuid4()
            return c.scalar(text("""INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,
                checksum_sha256) VALUES(:i,:v,'text/markdown',:k,:n,:h) RETURNING id"""),
                dict(i=i, v=uuid.uuid4(), k=key, n=len(data), h=h))
        v = c.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id"), {'i': i})
        return c.scalar(text("""INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,
            checksum_sha256) VALUES(:i,:v,'text/markdown',:k,:n,:h) RETURNING id"""), dict(i=i, v=v, k=key, n=len(data), h=h))


def head_time(b, key):
    return b.raw.head(key)[2].astimezone(UTC)


# --- full scope -------------------------------------------------------------------------------

def test_only_unreferenced_originals_are_deleted_rows_and_bytes_kept(orphan):
    b = orphan
    holders = ('private_a', 'private_b', 'shared', 'pending', 'listed', 'revoked', 'no_item', 'absent_item')
    kept = {}
    for holder in holders:
        key = put(b)
        refer(b, key, 'private_a' if holder == 'shared' else holder)
        if holder == 'shared':
            refer(b, key, 'private_b')  # one object named by two items
        kept[key] = holder
    same_bytes = put(b)  # same bytes as every referenced object, other key: still an orphan
    other = put(b, data=b'# another orphan\n')
    before = business(b)
    result = run(b)
    assert result == {'listed': 10, 'kept:referenced': 8, 'deleted': 2}, result
    assert not present(b, same_bytes) and not present(b, other)
    for key in kept:
        assert body(b, key) == BODY, kept[key]
    assert business(b) == before  # item/version/file/job/rights/ledger/tombstone/fence rows byte-identical
    assert fences(b) == sorted([(same_bytes, len(BODY)), (other, len(b'# another orphan\n'))])
    assert completions(b) == sorted([(same_bytes, 'deleted'), (other, 'deleted')])
    assert audits(b) == 2
    # Storage calls: only the configured prefix, never write/copy/read bytes, no DB connection held.
    assert not b.watch.forbidden and all(pool == 0 for _, _, pool in b.watch.calls)
    assert b.watch.names().count('list') == 1
    assert {c[1] for c in b.watch.calls if c[0] == 'delete'} == {same_bytes, other}
    details = q(b, "SELECT json_agg(details)::text FROM audit_events WHERE action='original.orphan_reconcile'")
    assert same_bytes not in details and other not in details and 'deleted' in details
    assert q(b, 'SELECT start_after FROM orphan_reconcile_cursors') is None  # listing exhausted: wraps
    b.watch.calls.clear()
    assert run(b) == {'listed': 8, 'kept:referenced': 8}  # idempotent
    assert audits(b) == 2 and 'delete' not in b.watch.names()


def test_reference_hints_are_not_the_authority(orphan, monkeypatch):
    """With the early hint read bypassed, the routine still refuses every referenced key."""
    b = orphan
    referenced = [put(b) for _ in range(3)]
    for key, holder in zip(referenced, ('private_a', 'revoked', 'absent_item')):
        refer(b, key, holder)
    lone = put(b)
    monkeypatch.setattr(orphans, 'hints', lambda engine, keys: (frozenset(), frozenset()))
    assert run(b) == {'listed': 4, 'kept:referenced': 3, 'deleted': 1}
    assert all(present(b, k) for k in referenced) and not present(b, lone)
    assert fences(b) == [(lone, len(BODY))]


def test_preview_writes_nothing_and_touches_no_object(orphan):
    b = orphan
    key = put(b)
    refer(b, put(b), 'private_a')
    put(b, 'originals/crash-orphan')
    before = business(b)
    assert run(b, apply=False) == {'listed': 3, 'would_reconcile': 1, 'kept:referenced': 1, 'kept:foreign_key': 1}
    assert b.watch.names() == ['list'] and present(b, key)
    assert business(b) == before and fences(b) == [] and audits(b) == 0
    assert q(b, 'SELECT count(*) FROM orphan_reconcile_cursors') == 0


def test_foreign_keys_and_other_prefixes_are_never_touched(orphan):
    b = orphan
    foreign = [put(b, 'originals/crash-orphan'), put(b, 'originals/' + uuid.uuid4().hex.upper()),
               put(b, 'originals/' + uuid.uuid4().hex + '.bak'), put(b, 'originals/sub/' + uuid.uuid4().hex)]
    outside = [put(b, 'synthetic/' + uuid.uuid4().hex), put(b, 'originalsx/' + uuid.uuid4().hex)]
    assert run(b) == {'listed': 4, 'kept:foreign_key': 4}
    assert all(present(b, k) for k in foreign + outside)
    assert all(c[1] is None for c in b.watch.calls if c[0] == 'list') and 'head' not in b.watch.names()


# --- entry and gate ---------------------------------------------------------------------------

@pytest.mark.parametrize('login', ['owner', 'recovery', 'no_fence_grant', 'no_complete_grant', 'raw_fence_write',
                                   'raw_completion_write', 'temp', 'protected_write'])
@pytest.mark.parametrize('caller', ['direct', 'cli'])
def test_entry_requires_actual_runtime_login_before_any_storage(orphan, monkeypatch, capsys, login, caller):
    b = orphan
    key = put(b)
    engine = {'owner': b.w.engine, 'recovery': b.w.recovery}.get(login, get_engine())
    statements = {  # labelled misprovisioning of THIS test database only (no role attributes)
        'no_fence_grant': f'REVOKE EXECUTE ON FUNCTION {FENCE} FROM c1_runtime',
        'no_complete_grant': f'REVOKE EXECUTE ON FUNCTION {COMPLETE} FROM c1_runtime',
        'raw_fence_write': 'GRANT INSERT ON orphan_object_fences TO c1_runtime',
        'raw_completion_write': 'GRANT INSERT ON orphan_object_completions TO c1_runtime',
        'temp': 'GRANT TEMPORARY ON DATABASE "{db}" TO c1_runtime',
        'protected_write': 'GRANT UPDATE ON users TO c1_runtime'}
    if login in statements:
        q(b, statements[login].format(db=b.w.engine.url.database))
    if caller == 'direct':
        assert run(b, engine=engine) == {'halted:worker_runtime_required': 1}
    else:
        from app.cli import reconcile_originals as cli
        monkeypatch.setattr(cli, 'get_engine', lambda: engine)
        monkeypatch.setattr(cli, 'get_storage', lambda settings: b.watch)
        assert cli.main(['--apply']) == 1
        assert 'halted:worker_runtime_required=1' in capsys.readouterr().out
    assert b.watch.calls == [] and present(b, key) and fences(b) == [] and audits(b) == 0


@pytest.mark.parametrize('fault', ['probe', 'release'])
def test_entry_probe_or_release_fault_stops_before_storage(orphan, monkeypatch, fault):
    b = orphan
    key = put(b)
    if fault == 'probe':
        def broken(db):
            raise RuntimeError('synthetic probe fault')
        monkeypatch.setattr(runtime, 'require_runtime', broken)
        expected = 'halted:worker_checkpoint_unavailable'
    else:
        class Faulty(Session):
            def close(self):
                super().invalidate()
                raise RuntimeError('synthetic release fault')
        monkeypatch.setattr(orphans, 'Session', Faulty)
        expected = 'halted:worker_release_unavailable'
    assert run(b) == {expected: 1}
    assert b.watch.calls == [] and present(b, key) and fences(b) == []


def close_gate(b, gate, monkeypatch):
    if gate == 'pending':
        monkeypatch.setattr(auth_safety, 'cutover', REAL_CUTOVER)  # the real production answer
    elif gate == 'missing':
        b.w.path.unlink()
    elif gate == 'hold':
        recovery.hold(b.w.engine, get_settings(), NOW)
    else:
        with b.w.path.open('ab') as f:
            f.write(b'{"torn":')


@pytest.mark.parametrize('gate', ['pending', 'missing', 'hold', 'corrupt'])
def test_closed_gate_no_storage_no_mutation_cli_and_direct(orphan, monkeypatch, capsys, gate):
    b = orphan
    key = put(b)
    before = business(b)
    close_gate(b, gate, monkeypatch)
    assert run(b) == {'halted:recovery_gate_closed': 1}
    assert run(b, apply=False) == {'halted:recovery_gate_closed': 1}  # even the preview lists nothing
    from app.cli import reconcile_originals as cli
    monkeypatch.setattr(cli, 'get_storage', lambda settings: b.watch)
    assert cli.main(['--apply']) == 1 and 'halted:recovery_gate_closed=1' in capsys.readouterr().out
    assert b.watch.calls == [] and present(b, key) and business(b) == before and fences(b) == []


def test_cli_without_configuration_refuses(monkeypatch, capsys):
    from app.cli import reconcile_originals as cli
    for key in ('RAS_DB__HOST', 'RAS_DB__PORT', 'RAS_DB__USER', 'RAS_DB__PASSWORD', 'RAS_DB__NAME', 'RAS_DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        assert cli.main(['--apply']) == 2
    finally:
        get_settings.cache_clear()
    assert 'reconcile_runtime_configuration_required' in capsys.readouterr().err


def test_cli_apply_and_preview_use_the_runtime_boundary(orphan, monkeypatch, capsys):
    b = orphan
    from app.cli import reconcile_originals as cli
    key = put(b)
    seen = []
    real = orphans.prepare
    monkeypatch.setattr(orphans, 'prepare', lambda engine, *a: seen.append(engine.url.username) or real(engine, *a))
    monkeypatch.setattr(cli, 'get_storage', lambda settings: b.watch)
    assert cli.main([]) == 0 and 'would_reconcile=1' in capsys.readouterr().out and present(b, key)
    assert cli.main(['--apply']) == 0
    out = capsys.readouterr().out
    assert 'deleted=1' in out and key not in out and not present(b, key) and seen == ['c1_runtime']
    with pytest.raises(SystemExit):
        cli.main(['--apply', '--bucket', 'other'])  # no bucket/key arguments exist


# --- grace, clock and listing -----------------------------------------------------------------

def test_grace_boundary_is_strict_at_whole_seconds(orphan, monkeypatch):
    b = orphan
    key = put(b)
    at = head_time(b, key)
    monkeypatch.setattr(orphans, 'utcnow', lambda: at + GRACE)
    assert run(b) == {'listed': 1, 'kept:recent': 1} and present(b, key)
    monkeypatch.setattr(orphans, 'utcnow', lambda: (at + GRACE + timedelta(microseconds=1)).astimezone(
        __import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')))  # any timezone, compared in UTC
    assert run(b) == {'listed': 1, 'deleted': 1} and not present(b, key)
    assert q(b, 'SELECT observed_last_modified FROM orphan_object_fences') == at


def test_recent_object_never_heads_or_fences(orphan):
    b = orphan
    b.shift = timedelta(hours=1)
    key = put(b)
    assert run(b) == {'listed': 1, 'kept:recent': 1}
    assert b.watch.names() == ['list'] and present(b, key) and fences(b) == []


@pytest.mark.parametrize('clock', ['naive', 'not_datetime'])
def test_invalid_clock_halts_without_fence(orphan, monkeypatch, clock):
    b = orphan
    key = put(b)
    monkeypatch.setattr(orphans, 'utcnow', lambda: datetime.now() if clock == 'naive' else 'now')
    assert run(b) == {'halted:orphan_clock_invalid': 1}
    assert present(b, key) and fences(b) == [] and b.watch.calls == []


def test_clock_behind_after_final_sql_refuses_fence_commit(orphan, monkeypatch):
    b = orphan
    key = put(b)
    real = orphans._final_gate
    def late(db, *, writer):
        real(db, writer=writer)
        if db.info.get('orphan_checkpoint') == 'prepare':
            b.shift -= timedelta(seconds=5)
    monkeypatch.setattr(orphans, '_final_gate', late)
    assert run(b) == {'listed': 1, 'halted:orphan_clock_invalid': 1}
    assert present(b, key) and fences(b) == [] and b.watch.names() == ['list', 'head']


def test_clock_read_after_final_sql_and_journal(orphan, monkeypatch):
    b = orphan
    put(b)
    order = []
    real_gate, real_now = orphans._final_gate, orphans._now
    monkeypatch.setattr(orphans, '_final_gate', lambda db, *, writer: (real_gate(db, writer=writer),
                        order.append(('gate', db.info.get('orphan_checkpoint'))))[0])
    monkeypatch.setattr(orphans, '_now', lambda: (order.append(('clock', None)), real_now())[1])
    assert run(b) == {'listed': 1, 'deleted': 1}
    for checkpoint in ('prepare', 'pre_delete', 'finalize'):
        at = order.index(('gate', checkpoint))
        assert ('clock', None) in order[at + 1:], checkpoint


@pytest.mark.parametrize('change', ['size', 'rewritten'])
def test_object_changed_between_list_and_head_is_kept(orphan, change):
    b = orphan
    key = put(b)
    def rewrite(_):
        if change == 'size':
            put(b, key, BODY + b'grown')
        else:
            import time
            time.sleep(1.1)  # a later whole second: a fresh Last-Modified
            put(b, key, BODY)
    b.watch.hooks['head'] = lambda k: (rewrite(k), b.watch.hooks.pop('head'))
    assert run(b) == {'listed': 1, 'kept:changed': 1}
    assert present(b, key) and fences(b) == [] and 'delete' not in b.watch.names()


def test_object_rewritten_after_fence_is_kept_and_fence_stays(orphan, monkeypatch):
    b = orphan
    key = put(b)
    real = orphans.pre_delete
    def rewritten(*a):
        result = real(*a)
        put(b, key, BODY + b'overwritten after the fence')
        return result
    monkeypatch.setattr(orphans, 'pre_delete', rewritten)
    assert run(b) == {'listed': 1, 'kept:identity_changed': 1}
    assert body(b, key) == BODY + b'overwritten after the fence' and 'delete' not in b.watch.names()
    assert completions(b) == [(key, 'kept_identity_changed')] and len(fences(b)) == 1
    item = q(b, "INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'late') RETURNING id",
             o=b.user.id)
    version = q(b, "INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id", i=item)
    with pytest.raises(DBAPIError, match='fenced for deletion'), b.w.runtime.begin() as c:
        c.execute(text(INSERT_FILE), dict(i=item, v=version, k=key, n=1, h='a' * 64))
    monkeypatch.setattr(orphans, 'pre_delete', real)
    assert run(b) == {'listed': 1, 'kept:fenced': 1}  # completed: never retried, never deleted


@pytest.mark.parametrize('bad', ['naive', 'missing_time', 'bool_size', 'negative_size', 'missing_size'])
def test_unusable_listing_metadata_is_kept(orphan, monkeypatch, bad):
    b = orphan
    key = put(b)
    real = b.raw.list_originals
    def listed(*, start_after, max_keys):
        entries, truncated = real(start_after=start_after, max_keys=max_keys)
        e = entries[0]
        values = {'naive': dict(last_modified=e.last_modified.replace(tzinfo=None)), 'missing_time': dict(last_modified=None),
                  'bool_size': dict(size=True), 'negative_size': dict(size=-1), 'missing_size': dict(size=None)}[bad]
        return [ListedObject(**(dict(key=e.key, last_modified=e.last_modified, size=e.size, etag=e.etag) | values))], truncated
    monkeypatch.setattr(b.raw, 'list_originals', listed)
    assert run(b) == {'listed': 1, 'kept:metadata_invalid': 1}
    assert present(b, key) and fences(b) == [] and 'head' not in b.watch.names()


@pytest.mark.parametrize('fault', ['403', '500', 'timeout', 'no_bucket'])
def test_listing_error_is_never_an_empty_success(orphan, monkeypatch, capsys, fault):
    b = orphan
    key = put(b)
    settings = b.settings
    if fault == 'no_bucket':
        settings = b.settings.model_copy(deep=True)
        settings.storage.bucket = 'ras-c2d5e2-absent-' + uuid.uuid4().hex
        storage = Watched(get_storage(settings))
    else:
        storage = b.watch
        storage.faults['list'] = [FAULTS[fault]()]
    assert run(b, storage=storage, settings=settings) == {'halted:storage_unavailable': 1}
    if fault == 'no_bucket':  # never created by reconcile
        with pytest.raises(ClientError):
            storage.raw.client.head_bucket(Bucket=settings.storage.bucket)
    assert present(b, key) and fences(b) == [] and q(b, 'SELECT count(*) FROM orphan_reconcile_cursors') == 0
    from app.cli import reconcile_originals as cli
    monkeypatch.setattr(cli, 'get_storage', lambda s: storage)
    storage.faults['list'] = [FAULTS['500']()]
    monkeypatch.setattr(cli, 'get_settings', lambda: settings)
    assert cli.main(['--apply']) == 1 and 'nothing to do' not in capsys.readouterr().out


@pytest.mark.parametrize('shape', ['truncated_empty', 'not_ascending', 'not_bool'])
def test_untrustworthy_listing_halts(orphan, monkeypatch, shape):
    b = orphan
    keys = sorted(put(b) for _ in range(2))
    real = b.raw.list_originals
    def listed(*, start_after, max_keys):
        entries, truncated = real(start_after=start_after, max_keys=max_keys)
        if shape == 'truncated_empty':
            return [], True
        if shape == 'not_ascending':
            return list(reversed(entries)), truncated
        return entries, 'false'
    monkeypatch.setattr(b.raw, 'list_originals', listed)
    assert run(b) == {'halted:storage_listing_invalid': 1}
    assert all(present(b, k) for k in keys) and fences(b) == [] and 'head' not in b.watch.names()


def test_pagination_bounds_and_cursor_avoid_starvation(orphan):
    b = orphan
    keys = sorted('originals/' + uuid.uuid4().hex for _ in range(5))
    for key in keys:
        put(b, key)
    for key in keys[:3]:
        refer(b, key, 'private_a')  # the first page is all retained files
    s = bounded(b, orphan_list_page_size=2, orphan_max_pages=1)
    assert run(b, settings=s) == {'listed': 2, 'kept:referenced': 2, 'bound:pages': 1}
    assert q(b, 'SELECT start_after FROM orphan_reconcile_cursors') == keys[1]
    assert run(b, settings=s) == {'listed': 2, 'kept:referenced': 1, 'deleted': 1, 'bound:pages': 1}
    assert not present(b, keys[3]) and present(b, keys[4])
    assert run(b, settings=s) == {'listed': 1, 'deleted': 1}  # end of listing: the cursor wraps
    assert q(b, 'SELECT start_after FROM orphan_reconcile_cursors') is None
    assert [c[1] for c in b.watch.calls if c[0] == 'list'] == [None, keys[1], keys[3]]
    assert run(b, settings=s) == {'listed': 2, 'kept:referenced': 2, 'bound:pages': 1}


def test_delete_bound_per_pass_resumes_from_cursor(orphan):
    b = orphan
    keys = sorted(put(b) for _ in range(3))
    s = bounded(b, orphan_max_deletes=1)
    assert run(b, settings=s) == {'listed': 3, 'deleted': 1, 'bound:deletes': 1}
    assert [present(b, k) for k in keys] == [False, True, True]
    assert q(b, 'SELECT start_after FROM orphan_reconcile_cursors') == keys[0]
    assert run(b, settings=s) == {'listed': 2, 'deleted': 1, 'bound:deletes': 1}
    assert run(b, settings=s) == {'listed': 1, 'deleted': 1}
    assert not any(present(b, k) for k in keys) and audits(b) == 3


def test_malformed_cursor_halts_before_listing(orphan):
    b = orphan
    key = put(b)
    with b.w.engine.begin() as c:  # labelled corruption: the format CHECK removed, then a foreign cursor
        c.execute(text('ALTER TABLE orphan_reconcile_cursors DROP CONSTRAINT ck_orphan_reconcile_cursors_original_prefix'))
        c.execute(text("INSERT INTO orphan_reconcile_cursors(id,start_after,updated_at) VALUES(1,'other/x',now())"))
    assert run(b) == {'halted:orphan_cursor_invalid': 1}
    assert 'list' not in b.watch.names() and present(b, key)


# --- SQL fence, grants and the INSERT trigger --------------------------------------------------

INSERT_FILE = """INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,checksum_sha256)
    VALUES(:i,:v,'text/markdown',:k,:n,:h) RETURNING id"""


def fence_call(engine, **over):
    at = datetime(2026, 10, 9, tzinfo=UTC)
    params = dict(r=uuid.uuid4(), k='originals/' + uuid.uuid4().hex, n=1, m=at - timedelta(days=2),
                  c=at - GRACE, t=at) | over
    with engine.begin() as c:
        return c.scalar(text('SELECT ras_auth_orphan_fence(:r,:k,:n,:m,:c,:t)'), params)


@pytest.mark.parametrize('field', ['r', 'k', 'n', 'm', 'c', 't', 'bad_key', 'upper_key', 'negative', 'not_old',
                                   'cutoff_after'])
def test_orphan_fence_routine_exact_null_safe_binding(orphan, field):
    b = orphan
    at = datetime(2026, 10, 9, tzinfo=UTC)
    bad = {'bad_key': dict(k='other/' + uuid.uuid4().hex), 'upper_key': dict(k='originals/' + uuid.uuid4().hex.upper()),
           'negative': dict(n=-1), 'not_old': dict(m=at - GRACE), 'cutoff_after': dict(c=at)}.get(field, {field: None})
    with pytest.raises(DBAPIError, match='invalid orphan fence binding'):
        fence_call(b.w.runtime, **bad)
    assert fences(b) == []
    key = 'originals/' + uuid.uuid4().hex
    assert fence_call(b.w.runtime, k=key) == 'fenced'
    assert fence_call(b.w.runtime, k=key) == 'already_fenced'
    assert len(fences(b)) == 1


@pytest.mark.parametrize('holder', ['private_a', 'pending', 'listed', 'revoked', 'no_item', 'absent_item'])
def test_routine_refuses_any_reference_and_writes_nothing(orphan, holder):
    b = orphan
    key = 'originals/' + uuid.uuid4().hex
    refer(b, key, holder)
    assert fence_call(b.w.runtime, k=key) == 'referenced'
    assert fences(b) == []


def test_routine_requires_read_committed(orphan):
    b = orphan
    with pytest.raises(DBAPIError, match='requires read committed'), \
            b.w.runtime.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        c.execute(text('SELECT ras_auth_orphan_fence(:r,:k,1,:m,:c,:t)'), dict(
            r=uuid.uuid4(), k='originals/' + uuid.uuid4().hex, m=NOW - timedelta(days=2), c=NOW - GRACE, t=NOW))


def test_completion_routine_binding_and_once(orphan):
    b = orphan
    key = 'originals/' + uuid.uuid4().hex
    assert fence_call(b.w.runtime, k=key) == 'fenced'
    fid = q(b, 'SELECT id FROM orphan_object_fences WHERE storage_key=:k', k=key)
    at = datetime(2026, 10, 9, 1, tzinfo=UTC)
    def complete(**over):
        params = dict(f=fid, k=key, r=uuid.uuid4(), o='deleted', t=at) | over
        with b.w.runtime.begin() as c:
            return c.scalar(text('SELECT ras_auth_orphan_complete(:f,:k,:r,:o,:t)'), params)
    for over in (dict(f=None), dict(k=None), dict(r=None), dict(o=None), dict(t=None), dict(o='purged')):
        with pytest.raises(DBAPIError, match='invalid orphan completion'):
            complete(**over)
    for over in (dict(f=uuid.uuid4()), dict(k='originals/' + uuid.uuid4().hex), dict(t=at - timedelta(days=1))):
        with pytest.raises(DBAPIError, match='completion binding mismatch'):
            complete(**over)
    assert complete() == 'completed' and complete(o='already_absent') == 'already_completed'
    assert completions(b) == [(key, 'deleted')]


@pytest.mark.parametrize('statement', [
    "INSERT INTO orphan_object_fences(storage_key,run_id,observed_size,observed_last_modified,grace_cutoff,fenced_at)"
    " VALUES('originals/' || md5('x'),gen_random_uuid(),1,now()-interval '3 days',now()-interval '1 day',now())",
    "INSERT INTO orphan_object_completions(fence_id,outcome,run_id,completed_at) SELECT id,'deleted',run_id,now()"
    " FROM orphan_object_fences", "UPDATE orphan_object_fences SET observed_size=2",
    'DELETE FROM orphan_object_fences', 'TRUNCATE orphan_object_fences CASCADE',
    "UPDATE orphan_object_completions SET outcome='already_absent'", 'DELETE FROM orphan_object_completions',
    'TRUNCATE orphan_object_completions'])
def test_fence_and_completion_rows_are_not_runtime_writable_and_permanent(orphan, statement):
    b = orphan
    key = put(b)
    assert run(b) == {'listed': 1, 'deleted': 1}
    with pytest.raises(DBAPIError, match='permission denied'), b.w.runtime.begin() as c:
        c.execute(text(statement))
    if not statement.startswith('INSERT'):
        with pytest.raises(DBAPIError, match='permanent'), b.w.engine.begin() as c:  # even the owner
            c.execute(text(statement))
    assert fences(b) == [(key, len(BODY))] and completions(b) == [(key, 'deleted')]


def test_runtime_grants_are_narrow(orphan):
    b = orphan
    with b.w.engine.connect() as c:
        for table in ('orphan_object_fences', 'orphan_object_completions'):
            for role in ('c1_runtime', 'c1_recovery'):
                assert c.scalar(text("SELECT has_table_privilege(:r,:t,'SELECT')"), {'r': role, 't': table})
                for priv in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES'):
                    assert not c.scalar(text('SELECT has_table_privilege(:r,:t,:p)'), {'r': role, 't': table, 'p': priv})
        for routine in (FENCE, COMPLETE):
            assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"), {'f': routine})
            assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"), {'f': routine})
            owner = c.execute(text('SELECT r.rolname,r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r'
                                   ' ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:f)'), {'f': routine}).one()
            assert owner[:3] == ('c1_definer', False, True) and 'search_path=pg_catalog, public, pg_temp' in owner[3]
            assert not c.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a'
                                     ' WHERE p.oid=to_regprocedure(:f) AND a.grantee=0)'), {'f': routine})
        assert c.scalar(text("SELECT has_table_privilege('c1_runtime','orphan_reconcile_cursors','INSERT,UPDATE')"))


def new_item(b):
    with Session(b.w.runtime) as db:  # a private item/version through runtime DML (no file yet)
        i = db.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'race') RETURNING id"),
                      {'o': b.user.id})
        v = db.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id"), {'i': i})
        db.commit()
        return i, v


def test_fenced_key_refuses_every_new_source_file_and_metadata_update_works(orphan):
    b = orphan
    key = put(b)
    assert run(b) == {'listed': 1, 'deleted': 1}
    i, v = new_item(b)
    params = dict(i=i, v=v, k=key, n=len(BODY), h='a' * 64)
    for engine in (b.w.runtime, b.w.engine):  # raw runtime SQL and even the owner
        with pytest.raises(DBAPIError, match='fenced for deletion'), engine.begin() as c:
            c.execute(text(INSERT_FILE), params)
    with pytest.raises(DBAPIError, match='requires read committed'), \
            b.w.runtime.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        c.execute(text(INSERT_FILE), params | {'k': 'originals/' + uuid.uuid4().hex})
    with b.w.runtime.begin() as c:  # a new key is unaffected; existing files keep metadata updates
        assert c.scalar(text(INSERT_FILE), params | {'k': 'originals/' + uuid.uuid4().hex})
        c.execute(text('UPDATE source_files SET page_count=1 WHERE item_id=:i'), {'i': i})
    with pytest.raises(DBAPIError, match='immutable'), b.w.runtime.begin() as c:  # the key never moves onto a fence
        c.execute(text('UPDATE source_files SET storage_key=:k WHERE item_id=:i'), {'k': key, 'i': i})


def test_common_fence_still_refuses_insert_after_0045(orphan):
    b = orphan
    key = 'originals/' + uuid.uuid4().hex
    with b.w.engine.begin() as c:  # labelled setup: a 0044 revoked-common fence (no runtime path needed here)
        c.execute(text('INSERT INTO object_delete_fences(storage_key,tombstone_id,item_id,rights_epoch)'
                       ' VALUES(:k,gen_random_uuid(),gen_random_uuid(),1)'), {'k': key})
    i, v = new_item(b)
    with pytest.raises(DBAPIError, match='fenced for deletion'), b.w.runtime.begin() as c:
        c.execute(text(INSERT_FILE), dict(i=i, v=v, k=key, n=1, h='a' * 64))


def test_colliding_lock_hashes_are_still_two_keys(orphan):
    b = orphan
    pair = q(b, """SELECT array_agg(k) FROM (SELECT 'originals/' || md5(n::text) AS k, hashtext('originals/' || md5(n::text)) h
        FROM generate_series(1, 300000) n) s GROUP BY h HAVING count(*) > 1 LIMIT 1""")
    assert pair and pair[0] != pair[1] and q(b, 'SELECT hashtext(:x)=hashtext(:y)', x=pair[0], y=pair[1])
    put(b, pair[0])
    refer(b, pair[1], 'private_a')  # same lock hash, a referenced object: no effect on the other key
    assert run(b) == {'listed': 1, 'deleted': 1} and fences(b) == [(pair[0], len(BODY))]
    i, v = new_item(b)
    with b.w.runtime.begin() as c:
        assert c.scalar(text(INSERT_FILE), dict(i=i, v=v, k=pair[1], n=1, h='a' * 64))
    with pytest.raises(DBAPIError, match='fenced for deletion'), b.w.runtime.begin() as c:
        c.execute(text(INSERT_FILE), dict(i=i, v=v, k=pair[0], n=1, h='a' * 64))


# --- races on the key -------------------------------------------------------------------------

def test_insert_first_holds_key_then_prepare_keeps_object(orphan):
    b = orphan
    key = put(b)
    i, v = new_item(b)
    conn = b.w.runtime.connect()
    tx = conn.begin()
    conn.execute(text(INSERT_FILE), dict(i=i, v=v, k=key, n=len(BODY), h='b' * 64))  # holds the key lock
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(run, b)
        assert wait_for(b.w.engine, 'advisory')  # prepare waits on the same key lock, holding no row
        assert not running.done() and fences(b) == []
        tx.commit(); conn.close()
        assert running.result(30) == {'listed': 1, 'kept:referenced': 1}
    assert present(b, key) and fences(b) == [] and 'delete' not in b.watch.names()


def test_insert_rollback_while_waiting_lets_prepare_fence(orphan):
    b = orphan
    key = put(b)
    i, v = new_item(b)
    conn = b.w.runtime.connect()
    tx = conn.begin()
    conn.execute(text(INSERT_FILE), dict(i=i, v=v, k=key, n=len(BODY), h='b' * 64))
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(run, b)
        assert wait_for(b.w.engine, 'advisory')
        tx.rollback(); conn.close()
        assert running.result(30) == {'listed': 1, 'deleted': 1}


@pytest.mark.parametrize('lock_item', [False, True])
def test_prepare_first_refuses_waiting_insert_without_cycle(orphan, monkeypatch, lock_item):
    b = orphan
    key = put(b)
    entered, release = threading.Event(), threading.Event()
    real = orphans._final_gate
    def held(db, *, writer):
        if db.info.get('orphan_checkpoint') == 'prepare' and writer:
            entered.set(); assert release.wait(30)
        return real(db, writer=writer)
    monkeypatch.setattr(orphans, '_final_gate', held)
    i, v = new_item(b)
    outcome = {}
    def insert():
        try:
            with b.w.runtime.begin() as c:
                if lock_item:  # inserter holds its item first, then waits on the key: no cycle
                    c.execute(text('SELECT 1 FROM library_items WHERE id=:i FOR UPDATE'), {'i': i})
                c.execute(text(INSERT_FILE), dict(i=i, v=v, k=key, n=len(BODY), h='b' * 64))
            outcome['inserted'] = True
        except DBAPIError as exc:
            outcome['refused'] = str(exc.orig)
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(run, b)
        assert entered.wait(30)
        inserting = pool.submit(insert)
        assert wait_for(b.w.engine, 'advisory')
        release.set()
        inserting.result(30)
        assert running.result(30) == {'listed': 1, 'deleted': 1}
    assert 'fenced for deletion' in outcome['refused'] and 'inserted' not in outcome
    assert q(b, 'SELECT count(*) FROM source_files WHERE storage_key=:k', k=key) == 0 and not present(b, key)


def test_blocked_storage_holds_no_db_lock_or_connection(orphan):
    b = orphan
    key = put(b)
    entered, release = threading.Event(), threading.Event()
    def block(_):
        entered.set(); assert release.wait(30)
    b.watch.hooks['delete'] = block
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(run, b)
        assert entered.wait(30)
        try:
            assert get_engine().pool.checkedout() == 0
            with b.w.runtime.begin() as c:  # other work proceeds immediately: no waits on reconcile
                c.execute(text("SET LOCAL lock_timeout='200ms'"))
                assert c.scalar(text('SELECT pg_try_advisory_xact_lock(1918989163, hashtext(:k))'), {'k': key})
                assert c.scalar(text('SELECT pg_try_advisory_xact_lock(:j)'), {'j': recovery.JOURNAL_LOCK})
            assert q(b, """SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                AND usename='c1_runtime' AND state LIKE 'idle in transaction%'""") == 0
        finally:
            release.set()
        assert running.result(30) == {'listed': 1, 'deleted': 1}


# --- outcomes ---------------------------------------------------------------------------------

def faulty_commit(monkeypatch, checkpoint, landed):
    calls = []
    class Lost(Session):
        def commit(self):
            if self.info.get('orphan_checkpoint') != checkpoint:
                return super().commit()
            calls.append(1)
            (super().commit if landed else super().rollback)()
            raise RuntimeError('lost commit reply')
    monkeypatch.setattr(orphans, 'Session', Lost)
    return calls


@pytest.mark.parametrize('landed', [True, False])
def test_prepare_commit_uncertain_no_storage_then_independent_resume(orphan, monkeypatch, landed):
    b = orphan
    key, later = sorted(put(b) for _ in range(2))
    calls = faulty_commit(monkeypatch, 'prepare', landed)
    assert run(b) == {'listed': 2, 'halted:worker_commit_uncertain': 1}
    assert len(calls) == 1 and b.watch.names() == ['list', 'head']  # no delete, no next candidate
    assert present(b, key) and present(b, later) and len(fences(b)) == int(landed)
    i, v = new_item(b)
    if landed:  # the landed fence already refuses a late SourceFile
        with pytest.raises(DBAPIError, match='fenced for deletion'), b.w.runtime.begin() as c:
            c.execute(text(INSERT_FILE), dict(i=i, v=v, k=key, n=1, h='a' * 64))
    monkeypatch.setattr(orphans, 'Session', Session)
    # Landed: resumed from the durable fence first, then the listing; otherwise both from the listing.
    assert run(b) == ({'deleted': 2, 'listed': 1} if landed else {'listed': 2, 'deleted': 2})
    assert not present(b, key) and not present(b, later) and audits(b) == 2


@pytest.mark.parametrize('landed', [True, False])
def test_final_commit_uncertain_reports_no_success_and_resumes(orphan, monkeypatch, landed):
    b = orphan
    key, later = sorted(put(b) for _ in range(2))
    faulty_commit(monkeypatch, 'finalize', landed)
    result = run(b)
    assert result == {'listed': 2, 'halted:worker_commit_uncertain': 1}  # never 'deleted' without a known commit
    assert not present(b, key) and present(b, later)  # the admitted delete is not undone; no next candidate
    assert completions(b) == ([(key, 'deleted')] if landed else [])
    monkeypatch.setattr(orphans, 'Session', Session)
    b.watch.calls.clear()
    second = run(b)
    if landed:
        assert second == {'listed': 1, 'deleted': 1}
    else:  # resumed from the fence without any listing of the key, which is gone
        assert second == {'already_absent': 1, 'listed': 1, 'deleted': 1}
        assert b.watch.calls[0] == ('head', key, 0)
    assert completions(b) == sorted([(key, 'deleted' if landed else 'already_absent'), (later, 'deleted')])
    assert audits(b) == 2


@pytest.mark.parametrize('checkpoint', ['prepare', 'finalize'])
def test_release_fault_after_known_commit_keeps_mutation_and_stops(orphan, monkeypatch, checkpoint):
    b = orphan
    key, later = sorted(put(b) for _ in range(2))
    class Faulty(Session):
        def close(self):
            if self.info.get('orphan_checkpoint') == checkpoint and self.info.get('worker_outcome') \
                    and self.info['worker_outcome'].committed:
                super().invalidate()
                raise RuntimeError('synthetic release fault')
            return super().close()
    monkeypatch.setattr(orphans, 'Session', Faulty)
    assert run(b) == {'listed': 2, 'halted:worker_release_unavailable': 1}
    assert len(fences(b)) == 1 and present(b, later)
    assert present(b, key) is (checkpoint == 'prepare')  # a fence without clean release reaches no storage
    assert completions(b) == ([] if checkpoint == 'prepare' else [(key, 'deleted')])
    monkeypatch.setattr(orphans, 'Session', Session)
    expected = {'deleted': 2, 'listed': 1} if checkpoint == 'prepare' else {'listed': 1, 'deleted': 1}
    assert run(b) == expected
    assert not present(b, key) and not present(b, later) and audits(b) == 2


@pytest.mark.parametrize('fault', ['403', '500', 'timeout'])
@pytest.mark.parametrize('when', ['candidate_head', 'pre_delete_head', 'delete', 'verify_head'])
def test_storage_errors_halt_never_count_as_absent(orphan, fault, when):
    b = orphan
    key, later = sorted(put(b) for _ in range(2))
    error = FAULTS[fault]()
    b.watch.faults.update({'candidate_head': {'head': [error]}, 'pre_delete_head': {'head': [None, error]},
                           'delete': {'delete': [error]}, 'verify_head': {'head': [None, None, error]}}[when])
    assert run(b) == {'listed': 2, 'halted:storage_unavailable': 1}
    assert present(b, later) and [c[1] for c in b.watch.calls if c[0] != 'list'] == [key] * len(
        [c for c in b.watch.calls if c[0] != 'list'])  # the next candidate is never reached
    assert len(fences(b)) == int(when != 'candidate_head') and completions(b) == []
    assert present(b, key) is (when != 'verify_head')
    expected = {'candidate_head': {'listed': 2, 'deleted': 2}, 'pre_delete_head': {'deleted': 2, 'listed': 1},
                'delete': {'deleted': 2, 'listed': 1}, 'verify_head': {'already_absent': 1, 'listed': 1, 'deleted': 1}}
    assert run(b) == expected[when]
    assert not present(b, key) and not present(b, later)


def test_missing_object_is_absent_only_when_storage_says_so(orphan, monkeypatch):
    b = orphan
    put(b)
    b.watch.hooks['head'] = lambda k: (b.raw.delete(k), b.watch.hooks.pop('head'))
    assert run(b) == {'listed': 1, 'skipped:vanished': 1} and fences(b) == []
    key = put(b)
    real = orphans.pre_delete
    def gone(*a):
        result = real(*a)
        b.raw.delete(key)  # removed by someone else after admission
        return result
    monkeypatch.setattr(orphans, 'pre_delete', gone)
    assert run(b) == {'listed': 1, 'already_absent': 1}
    assert completions(b) == [(key, 'already_absent')] and 'delete' not in b.watch.names()


def test_object_still_present_after_delete_halts(orphan, monkeypatch):
    b = orphan
    key = put(b)
    monkeypatch.setattr(b.watch, 'delete', lambda k: b.watch.calls.append(('delete', k, 0)))  # a no-op delete
    assert run(b) == {'listed': 1, 'halted:orphan_object_still_present': 1}
    assert present(b, key) and completions(b) == [] and len(fences(b)) == 1


def test_reference_conflict_on_a_fence_halts(orphan, monkeypatch):
    b = orphan
    key = put(b)
    real = orphans.pre_delete
    def bypass(*a):  # labelled adversary: owner inserts with every trigger disabled
        refer(b, key, 'absent_item')
        return real(*a)
    monkeypatch.setattr(orphans, 'pre_delete', bypass)
    assert run(b) == {'listed': 1, 'halted:orphan_reference_conflict': 1}
    assert present(b, key) and completions(b) == []


# --- idempotence and concurrency --------------------------------------------------------------

def test_two_concurrent_passes_one_completion(orphan):
    b = orphan
    key = put(b)
    headed, deleting = threading.Barrier(2, timeout=30), threading.Barrier(2, timeout=30)
    first_heads, guard = [], threading.Lock()
    def head(_):  # each pass's FIRST HEAD comes after its pending/hint reads: neither has fenced yet
        with guard:
            first = len(first_heads) < 2
            first_heads.append(1)
        if first:
            headed.wait()
    b.watch.hooks['head'] = head
    b.watch.hooks['delete'] = lambda k: deleting.wait()  # both passes delete (at least once)
    with ThreadPoolExecutor(2) as pool:
        results = [f.result(60) for f in [pool.submit(run, b) for _ in range(2)]]
    assert sorted(map(str, results)) == ["{'listed': 1, 'already_completed': 1}", "{'listed': 1, 'deleted': 1}"]
    assert b.watch.names().count('delete') == 2
    assert completions(b) == [(key, 'deleted')] and audits(b) == 1 and len(fences(b)) == 1


def test_resume_prepared_fence_even_when_listing_no_longer_shows_it(orphan, monkeypatch):
    b = orphan
    key = put(b)
    real = orphans.prepare
    def prepared(*a):
        proof = real(*a)
        monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'auth_safety_pending')  # gate wins after the fence
        return proof
    monkeypatch.setattr(orphans, 'prepare', prepared)
    assert run(b) == {'listed': 1, 'halted:recovery_gate_closed': 1}
    assert len(fences(b)) == 1 and present(b, key) and 'delete' not in b.watch.names()
    monkeypatch.setattr(orphans, 'prepare', real)
    monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
    monkeypatch.setattr(b.raw, 'list_originals', lambda **kw: ([], False))  # the key is not listed any more
    assert run(b) == {'deleted': 1, 'listed': 0}
    assert not present(b, key) and completions(b) == [(key, 'deleted')]


@pytest.mark.parametrize('moment', ['during_head', 'after_delete'])
def test_gate_closing_after_admission_never_finalises_or_moves_on(orphan, monkeypatch, moment):
    b = orphan
    key, later = sorted(put(b) for _ in range(2))
    close = lambda *a: monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'auth_safety_pending')
    heads = []
    def head_hook(k):
        heads.append(k)
        if len(heads) == 2:  # the storage HEAD after pre_delete admission
            close()
    if moment == 'during_head':
        b.watch.hooks['head'] = head_hook
    else:
        b.watch.hooks['delete'] = close
    assert run(b) == {'listed': 2, 'halted:recovery_gate_closed': 1}
    assert not present(b, key) and present(b, later)  # an admitted delete completes; the next candidate waits
    assert completions(b) == [] and len(fences(b)) == 1 and audits(b) == 0
    b.watch.hooks.clear()
    monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
    assert run(b) == {'already_absent': 1, 'listed': 1, 'deleted': 1}


@pytest.mark.parametrize('gate', ['pending', 'hold'])
def test_gate_closing_inside_finalize_refuses_commit(orphan, monkeypatch, gate):
    b = orphan
    key = put(b)
    real = orphans.record_event
    def closing(db, **kw):
        close_gate(b, gate, monkeypatch)  # after entry and the completion routine, before the final gate
        return real(db, **kw)
    monkeypatch.setattr(orphans, 'record_event', closing)
    assert run(b) == {'listed': 1, 'halted:recovery_gate_closed': 1}
    assert not present(b, key) and completions(b) == [] and audits(b) == 0 and len(fences(b)) == 1


def test_gate_closed_before_pre_delete_starts_no_delete(orphan, monkeypatch):
    b = orphan
    key = put(b)
    real = orphans.prepare
    monkeypatch.setattr(orphans, 'prepare', lambda *a: (real(*a), recovery.hold(b.w.engine, get_settings(), NOW))[0])
    assert run(b) == {'listed': 1, 'halted:recovery_gate_closed': 1}
    assert present(b, key) and 'delete' not in b.watch.names() and len(fences(b)) == 1


# --- compatibility: upload, approval, recovery ------------------------------------------------

from tests.test_c2d4a_upload_intents import consent  # noqa: E402,F401
from tests.test_c2d4b_upload_original import upload, send, keys  # noqa: E402,F401


def test_upload_known_success_kept_and_still_downloads(upload, monkeypatch):
    b = upload
    token = b.make()
    created = send(b, token)
    assert created.status_code == 201, created.text
    file_id = created.json()['file_id']
    crash = 'originals/' + uuid.uuid4().hex
    b.storage.put(crash, io.BytesIO(b'crash-left'), 'text/markdown')
    monkeypatch.setattr(orphans, 'utcnow', lambda: datetime.now(UTC) + timedelta(days=2))
    assert orphans.reconcile_originals(get_engine(), b.storage, get_settings(), apply=True) == {
        'listed': 2, 'kept:referenced': 1, 'deleted': 1}
    assert crash not in keys(b) and len(keys(b)) == 1
    r = b.client.get(f'/files/{file_id}/original')
    assert r.status_code == 200 and r.content == b.data  # the retained original opens under its own permission


@pytest.mark.parametrize('landed', [True, False])
def test_approval_uncertain_commit_copy_kept_or_reconciled_after_recover(orphan, monkeypatch, landed):
    from app.api import review_approval
    from app.api.errors import ApiError
    from app.models.enums import AuditOutcome  # noqa: F401
    from tests.resource_fixtures import runtime_approve
    from tests.test_c2d5a_review_writes import opened
    from types import SimpleNamespace
    b = orphan
    b.raw.put(b.own.key, io.BytesIO(BODY), 'text/markdown')  # the private original (synthetic/ key, outside the prefix)
    opened(b)
    class Lost(Session):
        def commit(self):
            (super().commit if landed else super().rollback)()
            raise RuntimeError('lost commit reply')
    monkeypatch.setattr(review_approval, 'Session', Lost)
    with pytest.raises(ApiError) as refused:
        runtime_approve(b.w, b.rid, b.raw)
    assert refused.value.code == 'approval_outcome_uncertain'
    monkeypatch.setattr(review_approval, 'Session', Session)
    copies = [k for k, _ in b.raw.orphan_keys()]
    assert len(copies) == 1  # the approval keeps its copy on an uncertain commit
    copy = copies[0]
    referenced = q(b, 'SELECT count(*) FROM source_files WHERE storage_key=:k', k=copy)
    assert referenced == int(landed)
    if landed:
        assert run(b) == {'listed': 1, 'kept:referenced': 1} and present(b, copy)
    else:  # the journal is ahead of the DB: reconcile is refused until the operator recovers
        assert run(b) == {'halted:recovery_gate_closed': 1} and b.watch.calls == [] and present(b, copy)
        recovery.recover(b.w.engine, SimpleNamespace(), get_settings(), NOW)  # labelled operator recovery
        assert run(b) == {'listed': 1, 'deleted': 1} and not present(b, copy)
    assert body(b, b.own.key) == BODY


def test_recover_and_operator_cleanup_never_call_orphan_reconcile(orphan, monkeypatch):
    from types import SimpleNamespace
    b = orphan
    key = put(b)
    monkeypatch.setattr(orphans, 'reconcile_originals', lambda *a, **k: pytest.fail('orphan path'))
    recovery.hold(b.w.engine, get_settings(), NOW)
    recovery.recover(b.w.engine, SimpleNamespace(), get_settings(), NOW)  # labelled operator recovery
    assert present(b, key) and fences(b) == []  # recover is not an orphan GC and does not bypass the gate
