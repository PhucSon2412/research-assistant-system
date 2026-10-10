"""C2d-5e1 runtime cleanup: actual runtime login, PostgreSQL, MinIO and journal.

Only business cutover is scoped (``world``). Owner-engine statements are
labelled setup, corruption or adversary; ``operator_cleanup_revoked`` appears
only in labelled recovery/operator checks. The copy is indexed by the real
runtime document pipeline with a fake transport; no provider or arXiv call.
"""
import io
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, publication, recovery
from app.config import get_settings
from app.db import get_engine
from app.ingest import cleanup as boundary, runtime, worker as w
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, wait_for  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes, live_job  # noqa: F401
from tests.test_c2d5d2a_common_extraction import common  # noqa: F401
from tests.test_c2d5d2b_common_runtime import doc, run  # noqa: F401
from tests.resource_fixtures import BODY, derived, runtime_revoke, source

ROUTINE = 'ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)'
REAL_CUTOVER = auth_safety.cutover  # captured at import, before any scoped fixture patch
ITEM_TABLES = ('sections', 'chunks', 'pdf_pages', 'reference_entries', 'metadata_candidates', 'index_generations')


def s3_error(code, status):
    return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'HeadObject')


class Watched:
    """The real bucket; records each object call with the runtime pool state at that moment."""

    def __init__(self, raw):
        self.raw, self.calls, self.hooks, self.faults = raw, [], {}, {}

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

    def size(self, key):
        return self._call('size', key, lambda: self.raw.size(key))

    def delete(self, key):
        return self._call('delete', key, lambda: self.raw.delete(key))


def q(b, statement, **params):
    """Labelled owner read/setup; never the cleanup under test."""
    with b.w.engine.begin() as c:
        r = c.execute(text(statement), params)
        return r.scalar() if r.returns_rows else None


def present(b, key):
    try:
        b.raw_storage.size(key)
        return True
    except ClientError:
        return False


def tomb(b, item=None):
    with b.w.engine.connect() as c:
        return c.execute(text('SELECT id,purged_at,object_outcome,cleanup_attempts,last_cleanup_error,scrubbed_fields,'
                              'retained_fields FROM deletion_tombstones WHERE item_id=:i'), {'i': item or b.copy.item}).one()


def fences(b):
    with b.w.engine.connect() as c:
        return c.execute(text('SELECT storage_key,tombstone_id,item_id,source_file_id,rights_epoch FROM object_delete_fences'
                              ' ORDER BY storage_key')).all()


def snapshot(b, item=None):
    """Every row of the item and its ledger, for unchanged-state assertions."""
    i = item or b.copy.item
    queries = {t: f'SELECT * FROM {t} WHERE item_id=:i' for t in ITEM_TABLES} | {
        'entries': 'SELECT e.* FROM index_entries e JOIN index_generations g ON g.id=e.generation_id WHERE g.item_id=:i',
        'links': 'SELECT l.* FROM reference_citation_links l JOIN reference_entries r ON r.id=l.reference_id WHERE r.item_id=:i',
        'calls': 'SELECT c.* FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id WHERE f.item_id=:i',
        'item': 'SELECT * FROM library_items WHERE id=:i', 'files': 'SELECT * FROM source_files WHERE item_id=:i',
        'versions': 'SELECT * FROM paper_versions WHERE item_id=:i', 'rights': 'SELECT * FROM access_rights WHERE item_id=:i',
        'jobs': 'SELECT * FROM ingestion_jobs WHERE item_id=:i', 'members': 'SELECT * FROM collection_items WHERE item_id=:i'}
    with b.w.engine.connect() as c:
        return {k: sorted(tuple(map(str, r)) for r in c.execute(text(v), {'i': i}).all()) for k, v in queries.items()}


LEDGER = ('id', 'source_file_id', 'owner_user_id', 'model_id', 'provider', 'response_model_id', 'response_provider',
          'pipeline_version', 'status', 'dimensions', 'input_tokens', 'reserved_usd', 'cost_usd', 'cost_status',
          'reserved_day', 'reservation_token', 'reservation_job_id', 'reservation_job_token', 'finished_at')


def ledger(b):
    with b.w.engine.connect() as c:
        return sorted(tuple(r) for r in c.execute(text(f"SELECT {','.join('c.'+k for k in LEDGER)} FROM embedding_calls c"
            ' JOIN source_files f ON f.id=c.source_file_id WHERE f.item_id=:i'), {'i': b.copy.item}).all())


def clean(b, engine=None, storage=None):
    return boundary.cleanup_revoked(engine or get_engine(), storage or b.watch, get_settings())


@pytest.fixture
def gone(doc, monkeypatch):
    """Runtime-indexed listed copy + synthetic derived/citation/membership/live job; runtime revoke."""
    b = doc
    assert run(b) == 'ready'
    b.marker = 'C2D5E1-CANARY-' + uuid.uuid4().hex[:10]
    reference = derived(b.w, b.copy, b.marker)
    with b.w.engine.begin() as c:  # labelled setup rows the cleanup must remove
        c.execute(text("INSERT INTO reference_citation_links(reference_id,matcher_version,raw_citation_sha256,"
                       "match_status) VALUES(:r,'synthetic',repeat('d',64),'no_arxiv_id')"), {'r': reference})
        box = c.scalar(text("INSERT INTO collections(owner_user_id,name) VALUES(:u,'C2d-5e1') RETURNING id"),
                       {'u': b.user.id})
        c.execute(text('INSERT INTO collection_items(collection_id,item_id) VALUES(:c,:i)'), {'c': box, 'i': b.copy.item})
    b.live = live_job(b)
    b.private_before = snapshot(b, b.own.item)
    b.ledger = ledger(b)
    assert b.ledger and all(row[LEDGER.index('reservation_token')] for row in b.ledger)
    revoked = runtime_revoke(b.w, b.copy.item, clock=b.clock)
    assert revoked['item']['publication_state'] == 'revoked'
    b.tombstone = tomb(b).id
    b.watch = Watched(b.raw_storage)
    b.raw_storage.put(b.own.key, io.BytesIO(BODY), 'text/markdown')  # the private original, same bytes, other key
    assert present(b, b.copy.key) and present(b, b.own.key)
    return b


def assert_untouched(b, rows, *, object_present=True):
    assert snapshot(b) == rows
    assert tomb(b).purged_at is None
    assert present(b, b.copy.key) is object_present
    assert snapshot(b, b.own.item) == b.private_before


# --- full scope -------------------------------------------------------------------------------

def test_runtime_cleanup_removes_scope_keeps_ledger_private_and_audit(gone, monkeypatch):
    b = gone
    def observed_derived(original):
        def call(db, item_id, now):
            assert db.get_bind().url.username == 'c1_runtime'
            return original(db, item_id, now)
        return call
    monkeypatch.setattr(publication, '_remove_derived', observed_derived(publication._remove_derived))
    counts = {t: q(b, f'SELECT count(*) FROM {t} WHERE item_id=:i', i=b.copy.item) for t in ITEM_TABLES}
    assert all(counts[t] for t in ITEM_TABLES), counts
    assert clean(b) == {'deleted': 1}
    # S3 only on the authorised key, with no DB connection checked out by the attempt.
    assert b.watch.calls == [('size', b.copy.key, 0), ('delete', b.copy.key, 0), ('size', b.copy.key, 0)]
    assert not present(b, b.copy.key) and present(b, b.own.key)
    assert q(b, 'SELECT ras_common_copy_derived_left(:i)', i=b.copy.item) == []
    for t in ITEM_TABLES:
        assert q(b, f'SELECT count(*) FROM {t} WHERE item_id=:i', i=b.copy.item) == 0, t
    assert q(b, 'SELECT count(*) FROM collection_items WHERE item_id=:i', i=b.copy.item) == 0
    # Ledger: nonce/day/ceiling/cost/model/provider identical; only vector + hash removed.
    assert ledger(b) == b.ledger
    assert q(b, '''SELECT bool_and(c.vector IS NULL AND c.chunk_hash IS NULL AND c.content_removed_at=:t)
        FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id WHERE f.item_id=:i''', i=b.copy.item, t=NOW)
    job = q(b, 'SELECT state FROM ingestion_jobs WHERE id=:j', j=b.live)
    assert job == 'cancelled'
    item = q(b, 'SELECT row(title,content_status,active_index_generation_id,publication_state)::text FROM library_items WHERE id=:i',
             i=b.copy.item)
    assert item == '(,metadata_only,,revoked)'
    assert q(b, 'SELECT original_filename FROM source_files WHERE item_id=:i', i=b.copy.item) is None
    assert q(b, 'SELECT metadata_scrubbed_at FROM access_rights WHERE item_id=:i', i=b.copy.item) == NOW
    row = tomb(b)
    assert (row.purged_at, row.object_outcome, row.cleanup_attempts, row.last_cleanup_error) == (NOW, 'deleted', 1, None)
    assert 'title' in row.scrubbed_fields and row.retained_fields == []
    assert fences(b) == [(b.copy.key, b.tombstone, b.copy.item, b.copy.file, 1)]
    audits = q(b, "SELECT json_agg(details)::text FROM audit_events WHERE action='common_copy.cleanup'")
    assert audits.count('"object_outcome"') == 1 and b.marker not in audits and b.copy.key not in audits
    for table, column in (('sections', 'text'), ('chunks', 'text'), ('index_entries', 'text'), ('metadata_candidates', 'value'),
                          ('pdf_pages', 'text'), ('reference_entries', 'raw_citation')):
        assert q(b, f'SELECT count(*) FROM {table} WHERE {column} LIKE :m', m='%'+b.marker+'%') == 0
    assert snapshot(b, b.own.item) == b.private_before
    before = snapshot(b)
    b.watch.calls.clear()
    assert clean(b) == {} and snapshot(b) == before and not b.watch.calls  # idempotent rerun, no I/O


def test_retention_basis_keeps_only_that_field(doc):
    b = doc
    assert run(b) == 'ready'
    basis = dict(field='title', policy_source='synthetic policy', scope='this copy', reason='synthetic basis')
    runtime_revoke(b.w, b.copy.item, clock=b.clock, retain=[basis])
    b.watch = Watched(b.raw_storage)
    title = q(b, 'SELECT title FROM library_items WHERE id=:i', i=b.copy.item)
    assert title and clean(b) == {'deleted': 1}
    assert q(b, 'SELECT title FROM library_items WHERE id=:i', i=b.copy.item) == title
    row = tomb(b)
    assert row.retained_fields == ['title'] and 'title' not in row.scrubbed_fields
    assert q(b, 'SELECT original_filename FROM source_files WHERE item_id=:i', i=b.copy.item) is None


def test_derived_rows_shared_stops_before_fence_or_storage(gone):
    b = gone
    with b.w.engine.begin() as c:  # labelled corruption: B's private generation names the copy's chunk
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        g = c.scalar(text("""INSERT INTO index_generations(item_id,version_id,source_file_id,job_id,model_id,provider,dimensions,
            pipeline_version,index_version,expected_chunks,rights_epoch) SELECT :i,:v,:f,(SELECT id FROM ingestion_jobs
            WHERE item_id=:c LIMIT 1),'m','p',3,'synthetic','synthetic',1,0 RETURNING id"""),
            dict(i=b.own.item, v=b.own.version, f=b.own.file, c=b.copy.item))
        c.execute(text("""INSERT INTO index_entries(generation_id,chunk_id,embedding_call_id,chunk_hash,text)
            SELECT :g,ch.id,(SELECT ec.id FROM embedding_calls ec JOIN source_files f ON f.id=ec.source_file_id
            WHERE f.item_id=:c LIMIT 1),ch.chunk_hash,'shared' FROM chunks ch WHERE ch.item_id=:c LIMIT 1"""),
            dict(g=g, c=b.copy.item))
    rows = snapshot(b)
    assert clean(b) == {'error:derived_rows_shared': 1}
    assert snapshot(b) == rows and fences(b) == [] and not b.watch.calls and present(b, b.copy.key)
    row = tomb(b)
    assert (row.purged_at, row.cleanup_attempts, row.last_cleanup_error) == (None, 1, 'derived_rows_shared')


# --- entry and gate ---------------------------------------------------------------------------

@pytest.mark.parametrize('login', ['owner', 'recovery', 'no_routine_grant', 'raw_fence_grant'])
def test_entry_requires_actual_runtime_login(gone, login):
    b = gone
    rows = snapshot(b)
    engine = {'owner': b.w.engine, 'recovery': b.w.recovery}.get(login, get_engine())
    if login == 'no_routine_grant':
        q(b, f'REVOKE EXECUTE ON FUNCTION {ROUTINE} FROM c1_runtime')
    if login == 'raw_fence_grant':
        q(b, 'GRANT INSERT ON object_delete_fences TO c1_runtime')
    assert clean(b, engine) == {'halted:worker_runtime_required': 1}
    assert_untouched(b, rows)
    assert fences(b) == [] and not b.watch.calls and tomb(b).cleanup_attempts == 0


def test_operator_path_refuses_runtime_login(gone):
    b = gone
    rows = snapshot(b)
    with pytest.raises(publication.CleanupError, match='operator_cleanup_requires_operator'):
        publication.operator_cleanup_revoked(get_engine(), b.watch, NOW, 10)
    assert_untouched(b, rows)
    assert not b.watch.calls


def corrupt(b, gate, monkeypatch):
    if gate == 'pending':
        monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'auth_safety_pending')
    elif gate == 'missing':
        b.w.path.unlink()
    elif gate == 'hold':
        recovery.hold(b.w.engine, get_settings(), NOW)
    else:
        with b.w.path.open('ab') as f:
            f.write(b'{"torn":')


@pytest.mark.parametrize('gate', ['pending', 'missing', 'hold', 'corrupt'])
def test_closed_gate_no_mutation_no_storage_celery_cli_direct(gone, monkeypatch, gate):
    b = gone
    rows = snapshot(b)
    corrupt(b, gate, monkeypatch)
    assert clean(b) == {'halted:recovery_gate_closed': 1}
    from app.ingest import celery_app as tasks
    monkeypatch.setattr(tasks, 'settings', get_settings())
    monkeypatch.setattr(tasks, 'get_storage', lambda settings: b.watch)
    assert tasks.cleanup_revoked_copies.run() == {'skipped': 'recovery_gate_closed'}
    from app.cli import cleanup_revoked as cli
    monkeypatch.setattr(cli, 'get_storage', lambda settings: b.watch)
    assert cli.main() == 1
    assert_untouched(b, rows)
    assert fences(b) == [] and not b.watch.calls and tomb(b).cleanup_attempts == 0


def test_celery_and_cli_use_runtime_boundary(gone, monkeypatch, capsys):
    b = gone
    from app.ingest import celery_app as tasks
    from app.cli import cleanup_revoked as cli
    seen = []
    real = boundary.prepare
    monkeypatch.setattr(boundary, 'prepare', lambda engine, tid: seen.append(engine.url.username) or real(engine, tid))
    monkeypatch.setattr(tasks, 'settings', get_settings())
    monkeypatch.setattr(tasks, 'get_storage', lambda settings: b.watch)
    monkeypatch.setattr(publication, 'operator_cleanup_revoked', lambda *a, **k: pytest.fail('operator path'))
    assert tasks.cleanup_revoked_copies.run() == {'deleted': 1}
    monkeypatch.setattr(cli, 'get_storage', lambda settings: b.watch)
    assert cli.main() == 0 and 'nothing to do' in capsys.readouterr().out
    assert seen == ['c1_runtime'] and tomb(b).object_outcome == 'deleted'


def test_cli_without_connection_parts_refuses(monkeypatch, capsys):
    from app.cli import cleanup_revoked as cli
    for key in ('RAS_DB__HOST', 'RAS_DB__PORT', 'RAS_DB__USER', 'RAS_DB__PASSWORD', 'RAS_DB__NAME', 'RAS_DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        assert cli.main() == 2
    finally:
        get_settings.cache_clear()
    assert 'cleanup_runtime_configuration_required' in capsys.readouterr().err


@pytest.mark.parametrize('moment', ['after_prepare', 'during_head', 'after_delete'])
def test_gate_closing_between_checkpoints(gone, monkeypatch, moment):
    """No delete starts once the gate wins before pre-delete; an admitted delete may finish but never finalises."""
    b = gone
    rows = snapshot(b)
    close = lambda *a: monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'auth_safety_pending')
    real = boundary.prepare
    if moment == 'after_prepare':
        def prepared(engine, tid):
            proof = real(engine, tid); close(); return proof
        monkeypatch.setattr(boundary, 'prepare', prepared)
    elif moment == 'during_head':
        b.watch.hooks['size'] = close
    else:
        b.watch.hooks['delete'] = close
    assert clean(b) == {'halted:recovery_gate_closed': 1}
    assert snapshot(b) == rows and tomb(b).purged_at is None and tomb(b).cleanup_attempts == 0
    assert fences(b) == [(b.copy.key, b.tombstone, b.copy.item, b.copy.file, 1)]  # durable, not undone
    deleted = moment != 'after_prepare'
    assert [c[0] for c in b.watch.calls] == (['size', 'delete', 'size'] if deleted else [])
    assert present(b, b.copy.key) is not deleted  # an admitted delete completes; nothing recreates it
    monkeypatch.setattr(boundary, 'prepare', real)  # a later independent tick with the gate open again
    b.watch.hooks.clear()
    monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
    assert clean(b) == {('already_absent' if deleted else 'deleted'): 1}
    assert tomb(b).purged_at == NOW and len(fences(b)) == 1


# --- fence SQL --------------------------------------------------------------------------------

def call_routine(engine, **over):
    with engine.begin() as c:
        return c.scalar(text('SELECT ras_auth_cleanup_fence(:t,:i,:f,:k,:e)'), over)


@pytest.mark.parametrize('field', ['t', 'i', 'k', 'e', 'f', 'wrong_epoch', 'wrong_item', 'wrong_key', 'wrong_file',
                                   'other_tombstone', 'empty_key'])
def test_fence_routine_exact_binding(gone, field):
    b = gone
    good = dict(t=b.tombstone, i=b.copy.item, f=b.copy.file, k=b.copy.key, e=1)
    bad = dict(good)
    if field in good:
        bad[field] = None
    else:
        bad.update({'wrong_epoch': dict(e=2), 'wrong_item': dict(i=b.own.item), 'wrong_key': dict(k=b.own.key),
                    'wrong_file': dict(f=b.own.file), 'other_tombstone': dict(t=uuid.uuid4()),
                    'empty_key': dict(k='')}[field])
    with pytest.raises(DBAPIError, match='fence binding|invalid fence'):
        call_routine(b.w.runtime, **bad)
    assert fences(b) == []
    assert call_routine(b.w.runtime, **good) == 'fenced'
    assert call_routine(b.w.runtime, **good) == 'already_fenced'
    assert fences(b) == [(b.copy.key, b.tombstone, b.copy.item, b.copy.file, 1)]


def test_fence_routine_refuses_unrevoked_target_and_unconfirmed(gone):
    b = gone
    good = dict(t=b.tombstone, i=b.copy.item, f=b.copy.file, k=b.copy.key, e=1)
    with b.w.engine.begin() as c:  # labelled corruption: the target flips back to listed
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text("UPDATE library_items SET publication_state='listed' WHERE id=:i"), {'i': b.copy.item})
    with pytest.raises(DBAPIError, match='fence target not revoked'):
        call_routine(b.w.runtime, **good)
    rows = snapshot(b)
    assert clean(b) == {'error:target_not_revoked': 1}
    assert snapshot(b) == rows and fences(b) == [] and not b.watch.calls
    with b.w.engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text("UPDATE library_items SET publication_state='revoked' WHERE id=:i"), {'i': b.copy.item})
        c.execute(text('UPDATE revocation_journal_records SET journal_confirmed=false WHERE tombstone_id=:t'), {'t': b.tombstone})
    with pytest.raises(DBAPIError, match='fence binding mismatch'):
        call_routine(b.w.runtime, **good)
    assert fences(b) == []


@pytest.mark.parametrize('statement', [
    "INSERT INTO object_delete_fences(storage_key,tombstone_id,item_id,rights_epoch) VALUES('x',gen_random_uuid(),gen_random_uuid(),1)",
    "UPDATE object_delete_fences SET storage_key='moved'", 'DELETE FROM object_delete_fences', 'TRUNCATE object_delete_fences'])
def test_fence_rows_are_not_runtime_writable_and_permanent(gone, statement):
    b = gone
    assert clean(b) == {'deleted': 1}
    with pytest.raises(DBAPIError, match='permission denied'), b.w.runtime.begin() as c:
        c.execute(text(statement))
    if not statement.startswith('INSERT'):
        with pytest.raises(DBAPIError, match='permanent'), b.w.engine.begin() as c:  # even the owner
            c.execute(text(statement))
    assert fences(b) == [(b.copy.key, b.tombstone, b.copy.item, b.copy.file, 1)]


INSERT_FILE = """INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,checksum_sha256)
    VALUES(:i,:v,'text/markdown',:k,:n,:h) RETURNING id"""


def new_item(b, owner=None):
    with Session(b.w.runtime) as db:  # a private item/version through runtime DML (no file yet)
        i = db.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'race') RETURNING id"),
                      {'o': owner or b.user.id})
        v = db.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id"), {'i': i})
        db.commit()
        return i, v


def test_fenced_key_refuses_every_new_source_file(gone):
    b = gone
    assert clean(b) == {'deleted': 1}
    i, v = new_item(b)
    params = dict(i=i, v=v, k=b.copy.key, n=len(BODY), h=q(b, 'SELECT checksum_sha256 FROM source_files WHERE id=:f', f=b.own.file))
    for engine in (b.w.runtime, b.w.engine):  # raw runtime SQL and even the owner
        with pytest.raises(DBAPIError, match='fenced for deletion'), engine.begin() as c:
            c.execute(text(INSERT_FILE), params)
    with pytest.raises(DBAPIError, match='requires read committed'), \
            b.w.runtime.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        c.execute(text(INSERT_FILE), params | {'k': 'originals/' + uuid.uuid4().hex})
    with b.w.runtime.begin() as c:  # a new key is unaffected; existing files keep metadata updates
        assert c.scalar(text(INSERT_FILE), params | {'k': 'originals/' + uuid.uuid4().hex})
        c.execute(text('UPDATE source_files SET page_count=1 WHERE item_id=:i'), {'i': i})


def test_colliding_lock_hashes_are_still_two_keys(gone):
    b = gone
    pair = q(b, """SELECT array_agg(k) FROM (SELECT 'originals/' || md5(n::text) AS k, hashtext('originals/' || md5(n::text)) h
        FROM generate_series(1, 300000) n) s GROUP BY h HAVING count(*) > 1 LIMIT 1""")
    assert pair and pair[0] != pair[1] and q(b, 'SELECT hashtext(:x)=hashtext(:y)', x=pair[0], y=pair[1])
    with b.w.engine.begin() as c:  # labelled setup: the revoked copy's key replaced by a colliding synthetic key
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text('UPDATE source_files SET storage_key=:k WHERE item_id=:i'), {'k': pair[0], 'i': b.copy.item})
        c.execute(text('UPDATE deletion_tombstones SET storage_key=:k WHERE id=:t'), {'k': pair[0], 't': b.tombstone})
    assert call_routine(b.w.runtime, t=b.tombstone, i=b.copy.item, f=b.copy.file, k=pair[0], e=1) == 'fenced'
    i, v = new_item(b)
    with b.w.runtime.begin() as c:  # same lock hash, different object: allowed
        assert c.scalar(text(INSERT_FILE), dict(i=i, v=v, k=pair[1], n=1, h='a'*64))
    with pytest.raises(DBAPIError, match='fenced for deletion'), b.w.runtime.begin() as c:
        c.execute(text(INSERT_FILE), dict(i=i, v=v, k=pair[0], n=1, h='a'*64))


# --- races on the key -------------------------------------------------------------------------

def test_insert_first_holds_key_then_prepare_keeps_shared_object(gone):
    b = gone
    i, v = new_item(b)
    conn = b.w.runtime.connect()
    tx = conn.begin()
    conn.execute(text(INSERT_FILE), dict(i=i, v=v, k=b.copy.key, n=len(BODY), h='b'*64))  # holds the key lock
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(clean, b)
        assert wait_for(b.w.engine, 'advisory')  # prepare waits on the same key lock, holding no row
        assert not running.done() and fences(b) == []
        tx.commit(); conn.close()
        assert running.result(30) == {'shared_kept': 1}
    assert not b.watch.calls and present(b, b.copy.key) and fences(b) == []
    row = tomb(b)
    assert (row.object_outcome, row.purged_at) == ('shared_kept', NOW)
    assert q(b, 'SELECT ras_common_copy_derived_left(:i)', i=b.copy.item) == []


@pytest.mark.parametrize('lock_target', [False, True])
def test_prepare_first_refuses_waiting_insert_without_cycle(gone, monkeypatch, lock_target):
    b = gone
    entered, release = threading.Event(), threading.Event()
    real = boundary._final_gate
    def held(db, *, writer):
        if db.info.get('cleanup_checkpoint') == 'prepare' and writer:
            entered.set(); assert release.wait(30)
        return real(db, writer=writer)
    monkeypatch.setattr(boundary, '_final_gate', held)
    i, v = new_item(b)
    outcome = {}
    def insert():
        try:
            with b.w.runtime.begin() as c:
                if lock_target:  # inserter holds the revoked item first, then waits on the key: no cycle
                    c.execute(text('SELECT 1 FROM library_items WHERE id=:i FOR UPDATE'), {'i': b.copy.item})
                c.execute(text(INSERT_FILE), dict(i=i, v=v, k=b.copy.key, n=len(BODY), h='b'*64))
            outcome['inserted'] = True
        except DBAPIError as exc:
            outcome['refused'] = str(exc.orig)
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(clean, b)
        assert entered.wait(30)
        inserting = pool.submit(insert)
        assert wait_for(b.w.engine, 'advisory')
        release.set()
        inserting.result(30)
        assert running.result(30) == {'deleted': 1}
    assert 'fenced for deletion' in outcome['refused'] and 'inserted' not in outcome
    assert q(b, 'SELECT count(*) FROM source_files WHERE storage_key=:k', k=b.copy.key) == 1
    assert not present(b, b.copy.key) and tomb(b).object_outcome == 'deleted'


# --- reference isolation ----------------------------------------------------------------------

@pytest.mark.parametrize('holder', ['private', 'listed_common', 'pending_common', 'no_item', 'absent_item'])
def test_protected_reference_keeps_object_without_fence(gone, holder):
    b = gone
    with b.w.engine.begin() as c:  # labelled pre-existing physical sharing (inserted before any fence)
        if holder == 'private':
            c.execute(text(INSERT_FILE), dict(i=b.own.item, v=b.own.version, k=b.copy.key, n=len(BODY), h='c'*64))
        elif holder in ('listed_common', 'pending_common'):
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            state = 'listed' if holder == 'listed_common' else 'pending'
            i = c.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common',:s,'x') RETURNING id"),
                         {'s': state})
            v = c.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id"), {'i': i})
            c.execute(text(INSERT_FILE), dict(i=i, v=v, k=b.copy.key, n=len(BODY), h='c'*64))
        else:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            job = q(b, 'SELECT id FROM ingestion_jobs WHERE item_id=:i LIMIT 1', i=b.copy.item)
            item = None if holder == 'no_item' else uuid.uuid4()
            c.execute(text("""INSERT INTO source_files(item_id,job_id,verified_mime,storage_key,byte_size,checksum_sha256)
                VALUES(:i,:j,'text/markdown',:k,1,repeat('e',64))"""), dict(i=item, j=job, k=b.copy.key))
    assert clean(b) == {'shared_kept': 1}
    assert present(b, b.copy.key) and fences(b) == [] and not b.watch.calls
    assert tomb(b).object_outcome == 'shared_kept'
    assert q(b, 'SELECT ras_common_copy_derived_left(:i)', i=b.copy.item) == []


def test_same_checksum_private_key_and_new_approval_unaffected(gone):
    b = gone
    assert q(b, 'SELECT checksum_sha256 FROM source_files WHERE id=:f', f=b.own.file) == q(
        b, 'SELECT checksum_sha256 FROM source_files WHERE id=:f', f=b.copy.file)
    assert clean(b) == {'deleted': 1}
    assert present(b, b.own.key) and snapshot(b, b.own.item) == b.private_before
    # A new review/approval of the same private file mints a NEW key and is not fenced.
    from tests.test_c2d5c1_common_publish import pending
    again = pending(b)
    assert again.key not in (b.copy.key, b.own.key) and present(b, again.key)


# --- storage outcomes and outcome windows ------------------------------------------------------

@pytest.mark.parametrize('fault', ['403', '500', 'timeout'])
@pytest.mark.parametrize('when', ['head', 'verify'])
def test_storage_errors_never_count_as_absent(gone, fault, when):
    b = gone
    rows = snapshot(b)
    error = ConnectTimeoutError(endpoint_url='http://127.0.0.1') if fault == 'timeout' else s3_error(
        {'403': 'AccessDenied', '500': 'InternalError'}[fault], int(fault))
    b.watch.faults['size'] = [error] if when == 'head' else [None, error]
    assert clean(b) == {'error:storage_unavailable': 1}
    assert snapshot(b) == rows  # no DB content scrubbed as if the delete had succeeded
    assert present(b, b.copy.key) is (when == 'head')  # the object may already be gone; never recreated
    row = tomb(b)
    assert (row.purged_at, row.cleanup_attempts, row.last_cleanup_error) == (None, 1, 'storage_unavailable')
    assert len(fences(b)) == 1
    assert clean(b) == {('deleted' if when == 'head' else 'already_absent'): 1}
    assert tomb(b).cleanup_attempts == 2


def test_object_already_missing_is_absent(gone):
    b = gone
    b.raw_storage.delete(b.copy.key)
    assert clean(b) == {'already_absent': 1}
    assert [c[0] for c in b.watch.calls] == ['size']


def test_db_refusal_after_delete_rolls_back_content_keeps_fence(gone, monkeypatch):
    b = gone
    rows = snapshot(b)
    real = publication._remove_metadata
    # Metadata left without a basis: the 0023 purge guard refuses at flush, before the journal.
    monkeypatch.setattr(publication, '_remove_metadata', lambda db, item_id, at: ([], []))
    assert clean(b) == {'error:cleanup_failed': 1}
    assert snapshot(b) == rows and not present(b, b.copy.key) and len(fences(b)) == 1
    assert tomb(b).last_cleanup_error == 'cleanup_failed' and tomb(b).purged_at is None
    monkeypatch.setattr(publication, '_remove_metadata', real)
    assert clean(b) == {'already_absent': 1}


def faulty_commit(monkeypatch, checkpoint, landed):
    real = Session.commit
    def commit(self):
        if self.info.get('cleanup_checkpoint') == checkpoint:
            if landed:
                real(self)
            raise RuntimeError('synthetic commit fault')
        return real(self)
    monkeypatch.setattr(Session, 'commit', commit)
    return real


@pytest.mark.parametrize('landed', [True, False])
def test_prepare_commit_uncertain_no_storage_then_independent_resume(gone, monkeypatch, landed):
    b = gone
    rows = snapshot(b)
    real = faulty_commit(monkeypatch, 'prepare', landed)
    assert clean(b) == {'halted:worker_commit_uncertain': 1}
    assert not b.watch.calls and present(b, b.copy.key) and snapshot(b) == rows
    assert len(fences(b)) == (1 if landed else 0) and tomb(b).cleanup_attempts == 0  # no error writer
    monkeypatch.setattr(Session, 'commit', real)  # a later, independent tick
    assert clean(b) == {'deleted': 1} and len(fences(b)) == 1


@pytest.mark.parametrize('landed', [True, False])
def test_final_commit_uncertain_stops_batch_without_inference(gone, monkeypatch, landed):
    b = gone
    second = second_revoked(b)
    monkeypatch.setattr(w, 'utcnow', lambda: NOW + timedelta(seconds=2))
    real = faulty_commit(monkeypatch, 'finalize', landed)
    assert clean(b) == {'halted:worker_commit_uncertain': 1}  # no success count, no error writer, batch stopped
    first = tomb(b)
    assert (first.purged_at is not None) is landed and first.cleanup_attempts == (1 if landed else 0)
    assert first.last_cleanup_error is None and not present(b, b.copy.key)
    assert tomb(b, second).purged_at is None and tomb(b, second).cleanup_attempts == 0
    monkeypatch.setattr(Session, 'commit', real)
    expected = {'deleted': 1} | ({} if landed else {'already_absent': 1})
    assert clean(b) == expected
    assert q(b, "SELECT count(*) FROM audit_events WHERE action='common_copy.cleanup'") == 2


def test_error_checkpoint_commit_uncertain_halts(gone, monkeypatch):
    b = gone
    b.watch.faults['size'] = [s3_error('InternalError', 500)]
    faulty_commit(monkeypatch, 'error', False)  # rolled back: nothing recorded, batch stops
    assert clean(b) == {'halted:worker_commit_uncertain': 1}
    assert tomb(b).cleanup_attempts == 0


@pytest.mark.parametrize('checkpoint', ['prepare', 'finalize'])
def test_release_fault_after_known_commit_keeps_mutation_and_stops(gone, monkeypatch, checkpoint):
    b = gone
    real = Session.close
    def close(self):
        if self.info.get('cleanup_checkpoint') == checkpoint and self.info['worker_outcome'].committed:
            self.info['cleanup_checkpoint'] = 'closed'
            raise RuntimeError('synthetic close fault')
        return real(self)
    monkeypatch.setattr(Session, 'close', close)
    assert clean(b) == {'halted:worker_release_unavailable': 1}
    assert len(fences(b)) == 1
    if checkpoint == 'prepare':
        assert not b.watch.calls and present(b, b.copy.key) and tomb(b).purged_at is None
    else:
        assert tomb(b).object_outcome == 'deleted' and not present(b, b.copy.key)  # kept, not reported as success
    assert get_engine().pool.checkedout() == 0


def test_final_reference_conflict_after_delete_is_not_purged(gone):
    b = gone
    i, v = new_item(b)
    def appear(key):  # labelled corruption: the owner bypasses the INSERT fence while S3 runs
        with b.w.engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            c.execute(text(INSERT_FILE), dict(i=i, v=v, k=key, n=1, h='f'*64))
    b.watch.hooks['delete'] = appear
    assert clean(b) == {'halted:cleanup_reference_conflict': 1}
    assert tomb(b).purged_at is None


# --- concurrency and I/O isolation -------------------------------------------------------------

def second_revoked(b):
    """Another runtime-approved/published/revoked copy, so a batch holds two tombstones (later deadline)."""
    from tests.test_c2d5c1_common_publish import pending, publish
    copy = pending(b)
    saved, b.copy = b.copy, copy
    try:
        assert publish(b).status_code == 200
        runtime_revoke(b.w, copy.item, clock=lambda: NOW + timedelta(seconds=1))  # later deadline
    finally:
        b.copy = saved
    return copy.item


def test_two_concurrent_cleanups_one_success(gone):
    b = gone
    barrier = threading.Barrier(2, timeout=30)
    b.watch.hooks['delete'] = lambda key: barrier.wait()  # both attempts delete (at least once)
    with ThreadPoolExecutor(2) as pool:
        results = [f.result(60) for f in [pool.submit(clean, b) for _ in range(2)]]
    assert sorted(map(str, results)) == ["{'deleted': 1}", '{}']  # the loser skips the purged/locked tombstone
    assert [c[0] for c in b.watch.calls].count('delete') == 2
    assert q(b, "SELECT count(*) FROM audit_events WHERE action='common_copy.cleanup'") == 1
    assert len(fences(b)) == 1 and tomb(b).cleanup_attempts == 1


def test_blocked_storage_holds_no_db_lock(gone):
    b = gone
    entered, release = threading.Event(), threading.Event()
    def block(key):
        entered.set(); assert release.wait(30)
    b.watch.hooks['delete'] = block
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(clean, b)
        assert entered.wait(30)
        try:
            assert get_engine().pool.checkedout() == 0
            with b.w.runtime.begin() as c:  # other work proceeds immediately: no waits on cleanup
                c.execute(text("SET LOCAL lock_timeout='200ms'"))
                c.execute(text('SELECT 1 FROM library_items WHERE id=:i FOR UPDATE NOWAIT'), {'i': b.copy.item})
                c.execute(text('SELECT 1 FROM deletion_tombstones WHERE id=:t FOR UPDATE NOWAIT'), {'t': b.tombstone})
                assert c.scalar(text('SELECT pg_try_advisory_xact_lock(1918989163, hashtext(:k))'), {'k': b.copy.key})
                assert c.scalar(text('SELECT pg_try_advisory_xact_lock(:j)'), {'j': recovery.JOURNAL_LOCK})
            assert q(b, """SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                AND usename='c1_runtime' AND state LIKE 'idle in transaction%'""") == 0
        finally:
            release.set()
        assert running.result(30) == {'deleted': 1}


def test_clock_read_after_final_sql_and_journal(gone, monkeypatch):
    b = gone
    order = []
    real_gate, real_now = boundary._final_gate, boundary._now
    monkeypatch.setattr(boundary, '_final_gate', lambda db, *, writer: (real_gate(db, writer=writer),
                        order.append(('gate', db.info.get('cleanup_checkpoint'))))[0])
    monkeypatch.setattr(boundary, '_now', lambda: (order.append(('clock', None)), real_now())[1])
    assert clean(b) == {'deleted': 1}
    for checkpoint in ('prepare', 'pre_delete', 'finalize'):
        at = order.index(('gate', checkpoint))
        assert ('clock', None) in order[at + 1:], checkpoint


@pytest.mark.parametrize('checkpoint', ['prepare', 'finalize'])
def test_clock_behind_recorded_time_refuses_commit(gone, monkeypatch, checkpoint):
    b = gone
    rows = snapshot(b)
    real_gate = boundary._final_gate
    def late(db, *, writer):
        real_gate(db, writer=writer)
        if db.info.get('cleanup_checkpoint') == checkpoint:
            monkeypatch.setattr(w, 'utcnow', lambda: NOW - timedelta(seconds=1))
    monkeypatch.setattr(boundary, '_final_gate', late)
    assert clean(b) == {'halted:cleanup_clock_invalid': 1}
    assert tomb(b).purged_at is None and snapshot(b) == rows
    assert len(fences(b)) == (0 if checkpoint == 'prepare' else 1)


# --- index race and late accounting ------------------------------------------------------------

def test_cleanup_during_provider_then_late_result_financial_only(doc, monkeypatch):
    b = doc
    entered, release = threading.Event(), threading.Event()
    def paused():
        entered.set(); assert release.wait(30)
    b.action = paused
    b.watch = Watched(b.raw_storage)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(run, b)
        assert entered.wait(20)
        try:
            runtime_revoke(b.w, b.copy.item, clock=b.clock)
            assert clean(b) == {'deleted': 1}  # the reserved call keeps nonce/day/ceiling
            reserved = q(b, "SELECT row(status,reservation_token IS NOT NULL,reserved_day,reserved_usd)::text FROM embedding_calls")
            assert reserved.startswith('(reserved,t,')
        finally:
            release.set()
        assert running.result(30) in ('cancelled', 'checksum_mismatch', 'lease_lost')
    assert q(b, 'SELECT status FROM embedding_calls') == 'completed'  # settled once, original day
    assert q(b, 'SELECT reserved_usd FROM embedding_budget_days') == 0
    assert q(b, 'SELECT vector IS NULL AND chunk_hash IS NULL AND content_removed_at IS NOT NULL FROM embedding_calls')
    assert q(b, 'SELECT count(*) FROM index_entries') == 0 and tomb(b).object_outcome == 'deleted'
    assert q(b, 'SELECT active_index_generation_id IS NULL FROM library_items WHERE id=:i', i=b.copy.item)


@pytest.mark.parametrize('first', ['reap', 'settle'])
def test_late_reap_and_settle_after_cleanup_keep_ceiling_once(doc, monkeypatch, first):
    from app.ingest import document_accounting as money
    from app.ingest.embedding import EmbeddingResult
    b = doc
    entered, release = threading.Event(), threading.Event()
    proofs = []
    real_settle = money.settle
    def capture(db, proof, settings, **kw):
        proofs.append(proof)
        entered.set(); assert release.wait(30)
        raise runtime.Halt('test_settlement_withheld')  # the late paths below decide
    monkeypatch.setattr(money, 'settle', capture)
    b.watch = Watched(b.raw_storage)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(run, b)
        assert entered.wait(20)
        runtime_revoke(b.w, b.copy.item, clock=b.clock)
        assert clean(b) == {'deleted': 1}
        release.set()
        running.result(30)
    monkeypatch.setattr(money, 'settle', real_settle)
    reserved = q(b, 'SELECT reserved_usd FROM embedding_budget_days')
    assert reserved > 0
    result = EmbeddingResult(vector=[.1, .2, .3], model_id=b.settings.embedding.model_id, input_tokens=3,
                             response_model_id='text-embedding-3-small', response_provider='Azure')
    lease = lambda: monkeypatch.setattr(w, 'utcnow', lambda: NOW + timedelta(hours=2))
    if first == 'reap':
        lease()
        with Session(b.w.runtime) as db:
            assert money.reap(db, b.settings) == 1
        assert q(b, 'SELECT status FROM embedding_calls') == 'uncertain'
        assert q(b, 'SELECT reserved_usd FROM embedding_budget_days') == reserved  # ceiling retained
    with Session(b.w.runtime) as db:
        assert money.settle(db, proofs[0], b.settings, result=result) == 'completed'
    if first == 'settle':
        lease()
        with Session(b.w.runtime) as db:
            assert money.reap(db, b.settings) == 0
    with Session(b.w.runtime) as db:  # a second late result cannot settle again
        assert money.settle(db, proofs[0], b.settings, result=result) == 'completed'
    assert q(b, 'SELECT reserved_usd FROM embedding_budget_days') == 0
    assert q(b, 'SELECT committed_usd FROM embedding_budget_days') == q(b, 'SELECT cost_usd FROM embedding_calls')
    assert q(b, 'SELECT vector IS NULL AND chunk_hash IS NULL FROM embedding_calls')
    assert q(b, 'SELECT count(*) FROM index_entries') == 0


# --- recovery compatibility --------------------------------------------------------------------

def test_recover_uses_operator_path_and_sweeps_restored_object(gone, monkeypatch):
    b = gone
    recovery.hold(b.w.engine, get_settings(), NOW)
    assert clean(b) == {'halted:recovery_gate_closed': 1} and fences(b) == []
    used = []
    real = publication.operator_cleanup_revoked
    monkeypatch.setattr(publication, 'operator_cleanup_revoked', lambda *a: used.append(a[0].url.username) or real(*a))
    monkeypatch.setattr(boundary, 'cleanup_revoked', lambda *a, **k: pytest.fail('runtime path inside recover'))
    monkeypatch.setattr(auth_safety, 'cutover', REAL_CUTOVER)  # recover never makes safety ready
    report = recovery.recover(b.w.recovery, b.raw_storage, get_settings(), NOW)
    assert used and set(used) == {'c1_recovery'} and report.cleanup == {'deleted': 1}
    assert report.gate == 'auth_safety_pending' and report.reconciliation_complete
    assert tomb(b).purged_at == NOW and not present(b, b.copy.key)
    # The object store came back from an older backup: a purged tombstone's object is swept again.
    b.raw_storage.put(b.copy.key, io.BytesIO(BODY), 'text/markdown')
    recovery.hold(b.w.engine, get_settings(), NOW)
    again = recovery.recover(b.w.recovery, b.raw_storage, get_settings(), NOW)
    assert again.objects_removed == 1 and not present(b, b.copy.key)
    third = recovery.recover(b.w.recovery, b.raw_storage, get_settings(), NOW)
    assert third.objects_removed == 0 and third.gate == 'auth_safety_pending'
    assert present(b, b.own.key)
