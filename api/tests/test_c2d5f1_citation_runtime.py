"""C2d-5f1 citation reconcile through the runtime: actual runtime login, PostgreSQL and journal.

Shared business setup uses the real C4 contract; separate tests keep the
real pending gate. Owner-engine statements are labelled setup, corruption or
adversary and never stand in for the reconcile under test. Discovery pages are
synthetic OAI XML written by ``_write_page`` (no network). Faults are named
injections inside one process, not crashes; the Linux process smoke is separate.
"""
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import auth_safety, citations, recovery
from app.config import CitationPolicy, get_settings
from app.db import get_engine
from app.discovery import _write_page, parse_page
from app.ingest import runtime
from app.main import app
from app.models import DiscoverySource
from tests.resource_fixtures import (resource_api, resource_world, installation, source, common_copy,  # noqa: F401
                                     runtime_revoke)
from tests.test_auth_safety_foundation import NOW
from tests.test_citations import MD_REFS, SEED, LATE, oai_page

REAL_CUTOVER = auth_safety.cutover  # captured before named unit fault injections
BUSINESS = ('library_items', 'paper_versions', 'source_files', 'access_rights', 'common_publications',
            'deletion_tombstones', 'reference_entries', 'ingestion_jobs', 'embedding_calls', 'index_generations',
            'chunks', 'sections', 'metadata_candidates', 'discovery_papers', 'discovery_deleted_records')
PRIVATE_SQL = re.compile(r'\b(FROM|JOIN)\s+reference_entries\b|\bINTO\s+reference_citation_links\b'
                         r'|\bUPDATE\s+reference_citation_links\b|\bFROM\s+reference_citation_links\b', re.I)
LINK_KEY = citations.LINK_LOCK


@pytest.fixture
def cit(resource_api):
    h = resource_api
    h.w = h.world
    h.aid = h.create_user('cite-a@example.test')
    h.bid = h.create_user('cite-b@example.test')
    with Session(h.w.engine) as db:  # labelled setup: the topic row discovery pages hang off
        db.add(DiscoverySource(topic='cs.AI', window_from=NOW.date()))
        db.commit()
    assert get_engine().url.username == 'c1_runtime'
    yield h
    settle()  # a failed assertion must not leave a paused thread holding a connection
    assert get_engine().pool.checkedout() == 0


def q(h, statement, **params):
    """Labelled owner read/setup/adversary; never the reconcile under test."""
    with h.w.engine.begin() as c:
        r = c.execute(text(statement), params)
        return r.scalar() if r.returns_rows else None


def refs(h, ids, raws, *, provenance='synthetic'):
    """Labelled setup: reference rows of one item/version/file, inserted by the owner."""
    out = []
    with h.w.engine.begin() as c:
        for n, raw in enumerate(raws):
            out.append(c.scalar(text("""INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,
                raw_citation,fields,confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version)
                VALUES(:i,:v,:f,:n,:r,'{}','raw_only',:p,'markdown',0,:e,:p) RETURNING id"""),
                dict(i=ids.item, v=ids.version, f=ids.file, n=n, r=raw, p=provenance, e=max(1, len(raw)))))
    return out


def page(h, records=(), deleted=(), at=NOW):
    """Discovery page through the runtime login in its own transaction (the hook under test)."""
    with Session(get_engine()) as db:
        _write_page(db, 'cs.AI', parse_page(oai_page(records, deleted), 10**6), at)
        db.commit()


def links(h, ids=None):
    sql = """SELECT l.reference_id, l.match_status, l.arxiv_id, l.cited_version, l.id_form, p.remote_id,
        l.raw_citation_sha256 = encode(sha256(convert_to(r.raw_citation,'UTF8')),'hex') AS fresh, l.matcher_version
        FROM reference_citation_links l JOIN reference_entries r ON r.id=l.reference_id
        LEFT JOIN discovery_papers p ON p.id=l.discovery_paper_id"""
    with h.w.engine.connect() as c:
        rows = c.execute(text(sql)).all()
    return {r.reference_id: r for r in rows if ids is None or r.reference_id in ids}


def tombstone(h, item):
    """Labelled corruption/adversary: a tombstone row without the revoke's state change or journal."""
    with h.w.engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text("""INSERT INTO deletion_tombstones(target_kind,item_id,version_id,source_file_id,storage_key,
            access_rights_id,reason_code,prior_publication_state,rights_epoch,recorded_at,recorded_by_user_id,
            purge_due_at) SELECT 'common_copy',:i,version_id,id,storage_key,(SELECT id FROM access_rights
            WHERE item_id=:i),'rights_withdrawn','listed',1,:t,:u,:d FROM source_files WHERE item_id=:i"""),
            {'i': item, 't': NOW, 'u': h.w.uid, 'd': NOW + timedelta(days=1)})


def business(h):
    with h.w.engine.connect() as c:
        return {t: sorted(tuple(map(str, r)) for r in c.execute(text(f'SELECT * FROM {t}')).all()) for t in BUSINESS}


def run(engine=None, **policy):
    return citations.reconcile_pending(engine or get_engine(), CitationPolicy(**policy))


class Statements:
    """Every statement one engine sends; private reads/writes are recognised by table name."""

    def __init__(self, engine):
        self.engine, self.seen = engine, []
        event.listen(engine, 'before_cursor_execute', self.hook)

    def hook(self, conn, cursor, statement, params, context, executemany):
        self.seen.append(statement)

    def private(self):
        return [s for s in self.seen if PRIVATE_SQL.search(s)]

    def close(self):
        event.remove(self.engine, 'before_cursor_execute', self.hook)


def close_gate(h, gate, monkeypatch):
    if gate == 'pending':
        with h.w.engine.begin() as conn:  # trusted setup: no completed C4 checkpoint
            conn.execute(text('DELETE FROM auth_operational_state'))
    elif gate == 'missing':
        h.w.path.unlink()
    elif gate == 'hold':
        recovery.hold(h.w.engine, get_settings(), NOW)
    else:
        with h.w.path.open('ab') as f:
            f.write(b'{"torn":')


def advisory(h):
    """(pid, mode, granted) of the citation advisory lock in this database."""
    with h.w.engine.connect() as c:
        return c.execute(text("""SELECT pid, mode, granted FROM pg_locks WHERE locktype='advisory'
            AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
            AND classid=0 AND objid=:k AND objsubid=1"""), {'k': LINK_KEY}).all()


def lock_waiters(h, needle):
    """Runtime backends waiting on a heavyweight lock whose current query contains ``needle``."""
    with h.w.engine.connect() as c:
        return c.execute(text("""SELECT pid, wait_event FROM pg_stat_activity WHERE datname=current_database()
            AND usename='c1_runtime' AND wait_event_type='Lock' AND query ILIKE :n"""), {'n': '%' + needle + '%'}).all()


def eventually(predicate, label, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError(label)


_PAUSES, _THREADS = [], []


def settle():
    """Release every pause and join every helper thread (also after a failed assertion)."""
    for pause in _PAUSES:
        pause.go.set()
    for t in _THREADS:
        t.join(60)
    _PAUSES.clear()
    _THREADS.clear()


class Pause:
    """Park one named thread at a hook; the test observes locks, then releases it."""

    def __init__(self):
        self.reached, self.go = threading.Event(), threading.Event()
        _PAUSES.append(self)

    def __call__(self):
        self.reached.set()
        assert self.go.wait(30), 'pause never released'


def thread(target, name):
    box = {}

    def body():
        try:
            box['result'] = target()
        except BaseException as exc:  # noqa: BLE001
            box['error'] = exc
    t = threading.Thread(target=body, name=name, daemon=True)
    _THREADS.append(t)
    t.start()
    return t, box


# --- data: exact forms, private A/B, late/deleted/newer metadata, stale rows -------------------

def test_runtime_backfill_exact_forms_private_a_b_and_nothing_else(cit, monkeypatch):
    h = cit
    page(h, SEED)
    raws = [raw for raw, *_ in MD_REFS]
    a, b = source(h.w, h.aid, 'A PRIVATE'), source(h.w, h.bid, 'B PRIVATE')
    assert q(h, 'SELECT count(DISTINCT checksum_sha256) FROM source_files WHERE id IN (:a,:b)', a=a.file, b=b.file) == 1
    ra, rb = refs(h, a, raws), refs(h, b, raws)
    before = business(h)
    assert run(reconcile_batch_size=7) == {'batches': 5, 'references': 30, 'links_written': 30}
    got = links(h)
    for ids in (ra, rb):  # each reference has its own row, statuses exactly as the matcher says
        assert [got[r].match_status for r in ids] == [status for _, status, *_ in MD_REFS]
        assert [got[r].arxiv_id for r in ids] == [arxiv for _, _, arxiv, *_ in MD_REFS]
        assert [got[r].cited_version for r in ids] == [v for _, _, _, v, _ in MD_REFS]
        assert all(got[r].fresh and got[r].matcher_version == citations.MATCHER_VERSION for r in ids)
    assert business(h) == before  # item/file/rights/index/ledger/discovery rows untouched
    stamp = q(h, 'SELECT max(reconciled_at) FROM reference_citation_links')
    assert run() == {}  # idempotent: nothing pending, no write, no timestamp bump
    assert q(h, 'SELECT max(reconciled_at) FROM reference_citation_links') == stamp

    # Metadata arriving late: the discovery hook re-targets both owners' exact rows atomically.
    page(h, [LATE], at=NOW + timedelta(minutes=1))
    late = [n for n, (_, _, arxiv, *_) in enumerate(MD_REFS) if arxiv == '2609.00014']
    assert [links(h)[ids[n]].match_status for ids in (ra, rb) for n in late] == ['linked'] * 2
    # OAI deleted hides the target; newer metadata after the tombstone restores it.
    page(h, deleted=[('2609.00011', '2026-09-29')], at=NOW + timedelta(minutes=2))
    assert {links(h)[ids[0]].match_status for ids in (ra, rb)} == {'discovery_record_deleted'}
    page(h, [('2609.00011', 'Exact Linking Study', 2, '2026-09-30')], at=NOW + timedelta(minutes=3))
    assert {links(h)[ids[0]].match_status for ids in (ra, rb)} == {'linked'}
    # Bare/title-only/multiple/conflicting stay unlinked: metadata never makes them links.
    for n, (_, status, *_rest) in enumerate(MD_REFS):
        if status not in citations.EXACT:
            assert links(h)[ra[n]].match_status == status

    # A changed raw citation is stale until reconciled; only that reference is written.
    q(h, "UPDATE reference_entries SET raw_citation='A. Alpha. Exact Linking Study. 2026.' WHERE id=:r", r=ra[0])
    assert not links(h)[ra[0]].fresh
    assert run() == {'batches': 1, 'references': 1, 'links_written': 1}
    assert links(h)[ra[0]].match_status == 'no_arxiv_id' and links(h)[rb[0]].match_status == 'linked'
    # A matcher version bump makes every row pending again; one row per reference remains.
    monkeypatch.setattr(citations, 'MATCHER_VERSION', 'arxiv-exact-test-next')
    assert run() == {'batches': 1, 'references': 30, 'links_written': 30}
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 30
    assert q(h, "SELECT count(DISTINCT matcher_version) FROM reference_citation_links") == 1


def test_each_reference_binds_its_own_version_file_and_provenance(cit):
    h = cit
    page(h, SEED)
    a = source(h.w, h.aid, 'TWO VERSIONS')
    with h.w.engine.begin() as c:  # labelled setup: a second version/file of the same item
        v2 = c.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v2') RETURNING id"), {'i': a.item})
        f2 = c.scalar(text("""INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,
            checksum_sha256) VALUES(:i,:v,'text/markdown',:k,1,:h) RETURNING id"""),
            dict(i=a.item, v=v2, k='synthetic/' + uuid.uuid4().hex, h='c' * 64))
    from types import SimpleNamespace
    second = SimpleNamespace(item=a.item, version=v2, file=f2)
    raw = 'Exact. arXiv:2609.00012v2.'
    first_ids = refs(h, a, [raw], provenance='markdown-v1')
    second_ids = refs(h, second, [raw], provenance='pdf-v1')
    assert run() == {'batches': 1, 'references': 2, 'links_written': 2}
    with h.w.engine.connect() as c:
        rows = c.execute(text("""SELECT r.id, r.version_id, r.source_file_id, r.provenance, l.match_status,
            l.cited_version FROM reference_citation_links l JOIN reference_entries r ON r.id=l.reference_id
            ORDER BY r.provenance""")).all()
    assert [(r.id, r.version_id, r.source_file_id, r.provenance, r.match_status, r.cited_version) for r in rows] == [
        (first_ids[0], a.version, a.file, 'markdown-v1', 'linked', 'v2'),
        (second_ids[0], v2, f2, 'pdf-v1', 'linked', 'v2')]


def test_common_allowed_linked_revoked_and_tombstoned_never_written(cit):
    h = cit
    page(h, SEED)
    allowed = common_copy(h.w, indexed=False)  # runtime approve/publish; derived() adds one reference
    revoked = common_copy(h.w, pending=True)  # one listed copy per checksum: the others stay pending
    stoned = common_copy(h.w, pending=True)
    private = source(h.w, h.aid, 'PRIVATE SAME BYTES')
    refs(h, private, ['arXiv:2609.00011'])
    runtime_revoke(h.w, revoked.item)  # real C2d-5c2 journal/tombstone; cleanup has not run
    tombstone(h, stoned.item)  # labelled corruption: a tombstone without the revoke state change
    before = business(h)
    seen = Statements(get_engine())
    try:
        result = run()
    finally:
        seen.close()
    assert result == {'batches': 1, 'references': 2, 'links_written': 2}  # allowed copy + private only
    def of(item):
        return q(h, """SELECT count(*) FROM reference_citation_links l JOIN reference_entries r ON r.id=l.reference_id
            WHERE r.item_id=:i""", i=item)
    assert (of(allowed.item), of(private.item), of(revoked.item), of(stoned.item)) == (1, 1, 0, 0)
    assert business(h) == before
    # The batch locked exactly the open common item, never the closed ones.
    locks = [s for s in seen.seen if 'FOR KEY SHARE' in s]
    assert len(locks) == 1
    # Discovery re-target never touches a closed copy either (it would hit the 0024 fence).
    page(h, deleted=[('2609.00011', '2026-09-29')], at=NOW + timedelta(minutes=1))
    assert of(revoked.item) == 0 and of(stoned.item) == 0
    assert q(h, """SELECT l.match_status FROM reference_citation_links l JOIN reference_entries r
        ON r.id=l.reference_id WHERE r.item_id=:i""", i=allowed.item) == 'discovery_record_deleted'


# --- entry: login, grants, probe/release, disabled; CLI/direct/Celery ---------------------------

def celery_module(monkeypatch):
    import importlib
    monkeypatch.setenv('RAS_REDIS_PASSWORD', 'unused-in-tests')
    get_settings.cache_clear()
    module = importlib.import_module('app.ingest.celery_app')
    monkeypatch.setattr(module, 'settings', get_settings())
    return module


@pytest.mark.parametrize('caller', ['direct-owner', 'direct-recovery', 'cli-owner', 'celery-owner'])
def test_entry_requires_actual_runtime_login_before_private_read(cit, monkeypatch, capsys, caller):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    engine = h.w.recovery if caller.endswith('recovery') else h.w.engine
    seen = Statements(engine)
    try:
        if caller.startswith('direct'):
            result = run(engine)
        elif caller == 'cli-owner':
            from app.cli import reconcile_citations as cli
            monkeypatch.setattr(cli, 'get_engine', lambda: engine)  # labelled: a misconfigured owner deployment
            assert cli.main([]) == 1
            out = capsys.readouterr().out
            result = {'halted:worker_runtime_required': 1} if 'halted:worker_runtime_required=1' in out else out
        else:
            module = celery_module(monkeypatch)
            monkeypatch.setattr(module, 'get_engine', lambda: engine)  # the task gate alone does not protect
            result = module.reconcile_citation_links()
    finally:
        seen.close()
    expected = {'skipped': 'recovery_gate_closed'} if caller == 'celery-owner' else {'halted:worker_runtime_required': 1}
    assert result == expected
    assert seen.private() == []  # refused before any private reference read or link write
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 0


@pytest.mark.parametrize('grant', ['UPDATE ON reference_entries', 'INSERT ON reference_citation_links',
                                   'SELECT ON discovery_papers', 'UPDATE ON library_items'])
def test_entry_missing_citation_grant_refused(cit, grant):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    q(h, f'REVOKE {grant} FROM c1_runtime')  # labelled adversary on this test database only
    seen = Statements(get_engine())
    try:
        assert run() == {'halted:worker_runtime_required': 1}
    finally:
        seen.close()
    assert seen.private() == [] and q(h, 'SELECT count(*) FROM reference_citation_links') == 0


def test_entry_probe_fault_and_release_fault_and_disabled(cit, monkeypatch):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    with monkeypatch.context() as m:
        m.setattr(citations, 'GRANTS', text('SELECT 1/0'))  # named probe fault
        assert run() == {'halted:worker_runtime_unavailable': 1}
    calls = {'n': 0}
    class ReleaseFaultSession(Session):
        def close(self):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('named release fault')
            return super().close()
    with monkeypatch.context() as m:
        # Target citation entry release after the real C4 gate's own Session.
        m.setattr(citations, 'Session', ReleaseFaultSession)
        assert run() == {'halted:worker_release_unavailable': 1}  # entry release fault: no batch
    assert get_engine().pool.checkedout() == 0 and q(h, 'SELECT count(*) FROM reference_citation_links') == 0
    seen = Statements(get_engine())
    try:
        assert run(enabled=False) == {'skipped:disabled': 1}
    finally:
        seen.close()
    assert seen.seen == []  # disabled: not even the runtime probe
    assert run() == {'batches': 1, 'references': 1, 'links_written': 1}


def test_cli_codes_only_and_configuration(cit, monkeypatch, capsys):
    h = cit
    from app.cli import reconcile_citations as cli
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['RAWCANARY arXiv:2609.00011', 'RAWCANARY two'])
    assert cli.main([]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == 'Citation reconcile: batches=1, links_written=2, references=2'
    assert 'RAWCANARY' not in out.out + out.err and '2609.00011' not in out.out
    assert cli.main([]) == 0 and capsys.readouterr().out.strip() == 'Citation reconcile: nothing to do'
    with monkeypatch.context() as m:
        for key in ('RAS_DB__HOST', 'RAS_DB__PORT', 'RAS_DB__USER', 'RAS_DB__PASSWORD', 'RAS_DB__NAME',
                    'RAS_DATABASE_URL'):
            m.delenv(key, raising=False)
        get_settings.cache_clear()
        try:
            assert cli.main([]) == 2
        finally:
            get_settings.cache_clear()
    assert 'reconcile_runtime_configuration_required' in capsys.readouterr().err


def test_celery_task_runs_the_runtime_boundary(cit, monkeypatch):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    page(h, SEED)
    module = celery_module(monkeypatch)
    entries = []
    real = citations.entry
    monkeypatch.setattr(citations, 'entry', lambda db: entries.append(1) or real(db))
    assert module.reconcile_citation_links() == {'batches': 1, 'references': 1, 'links_written': 1}
    assert len(entries) == 1  # one entry per batch; a short batch ends the pass
    assert q(h, "SELECT match_status FROM reference_citation_links") == 'linked'


# --- gate: before entry, between batches, before final ----------------------------------------

@pytest.mark.parametrize('gate', ['pending', 'missing', 'hold', 'corrupt'])
def test_closed_gate_refuses_before_private_read_cli_direct_celery(cit, monkeypatch, capsys, gate):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    close_gate(h, gate, monkeypatch)
    seen = Statements(get_engine())
    try:
        assert run() == {'halted:recovery_gate_closed': 1}
        from app.cli import reconcile_citations as cli
        assert cli.main([]) == 1 and 'halted:recovery_gate_closed=1' in capsys.readouterr().out
        module = celery_module(monkeypatch)
        assert module.reconcile_citation_links() == {'skipped': 'recovery_gate_closed'}
        monkeypatch.setattr(module, '_gate', lambda task: True)  # labelled: wrapper bypassed, boundary still closes
        assert module.reconcile_citation_links() == {'halted:recovery_gate_closed': 1}
    finally:
        seen.close()
    assert seen.private() == [] and q(h, 'SELECT count(*) FROM reference_citation_links') == 0
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/readyz').status_code == 503


@pytest.mark.parametrize('gate', ['pending', 'hold'])
def test_gate_closing_between_batches_stops_the_pass(cit, monkeypatch, gate):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    ids = refs(h, a, ['arXiv:2609.00011', 'arXiv:2609.00012', 'arXiv:2609.00013'])
    real = citations.entry
    calls = {'n': 0}

    def entry(db):
        calls['n'] += 1
        if calls['n'] == 2:
            close_gate(h, gate, monkeypatch)  # after the first known commit, before the next batch
        return real(db)
    monkeypatch.setattr(citations, 'entry', entry)
    assert run(reconcile_batch_size=1) == {'batches': 1, 'references': 1, 'links_written': 1,
                                           'halted:recovery_gate_closed': 1}
    assert list(links(h)) == [sorted(ids)[0]]


@pytest.mark.parametrize('gate', ['pending', 'hold', 'corrupt'])
def test_gate_closing_before_final_writes_nothing(cit, monkeypatch, gate):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    real = citations._upsert

    def upsert(db, rows, now):
        result = real(db, rows, now)
        close_gate(h, gate, monkeypatch)  # after the link DML, before the journal boundary
        return result
    monkeypatch.setattr(citations, '_upsert', upsert)
    assert run() == {'halted:recovery_gate_closed': 1}
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 0


# --- outcome: uncertain commit, release, binding, clock, busy, bounds --------------------------

def arm_commit(monkeypatch, *, landed):
    """Named fault: the next commit after a link upsert raises (landed or not)."""
    armed = {'on': False}
    real_upsert, real_commit = citations._upsert, Session.commit

    def upsert(db, rows, now):
        armed['on'] = True
        return real_upsert(db, rows, now)

    def commit(self):
        if armed['on']:
            armed['on'] = False
            if landed:
                real_commit(self)
            raise RuntimeError('named commit fault: response lost')
        return real_commit(self)
    monkeypatch.setattr(citations, '_upsert', upsert)
    monkeypatch.setattr(Session, 'commit', commit)


@pytest.mark.parametrize('landed', [True, False])
def test_commit_uncertain_stops_without_inference_then_independent_resume(cit, monkeypatch, landed):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    ids = sorted(refs(h, a, ['arXiv:2609.00011', 'arXiv:2609.00012']))
    with monkeypatch.context() as m:
        arm_commit(m, landed=landed)
        assert run(reconcile_batch_size=1) == {'halted:worker_commit_uncertain': 1}  # nothing counted, no next batch
    assert list(links(h)) == ([ids[0]] if landed else [])
    assert get_engine().pool.checkedout() == 0
    assert run(reconcile_batch_size=1) == {'batches': 2 if not landed else 1, 'references': 2 if not landed else 1,
                                           'links_written': 2 if not landed else 1}
    assert sorted(links(h)) == ids and q(h, 'SELECT count(*) FROM reference_citation_links') == 2
    assert run() == {}


def test_release_fault_after_known_commit_keeps_rows_and_stops(cit, monkeypatch):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    ids = sorted(refs(h, a, ['arXiv:2609.00011', 'arXiv:2609.00012']))
    armed = {'on': False}
    real_upsert, real_close = citations._upsert, Session.close

    def close(self):
        if armed['on']:
            armed['on'] = False
            raise RuntimeError('named release fault after commit')
        return real_close(self)
    with monkeypatch.context() as m:
        m.setattr(citations, '_upsert', lambda db, rows, now: armed.update(on=True) or real_upsert(db, rows, now))
        m.setattr(Session, 'close', close)
        assert run(reconcile_batch_size=1) == {'halted:worker_release_unavailable': 1, 'committed_unreleased': 1}
    assert list(links(h)) == [ids[0]]  # the known commit stays; the next batch never ran
    assert get_engine().pool.checkedout() == 0  # invalidated, not leaked
    assert run() == {'batches': 1, 'references': 1, 'links_written': 1}


def test_binding_change_before_final_refuses_commit(cit, monkeypatch):
    h = cit
    copy = common_copy(h.w, indexed=False)
    real = citations._upsert

    def upsert(db, rows, now):
        result = real(db, rows, now)
        # Labelled adversary in another transaction: a tombstone needs no item lock (no FK), so only
        # the scalar rebinding under the journal lock can see it before commit.
        tombstone(h, copy.item)
        return result
    monkeypatch.setattr(citations, '_upsert', upsert)
    assert run() == {'halted:citation_binding_changed': 1}
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 0


@pytest.mark.parametrize('clock', ['naive', 'backwards'])
def test_clock_fault_refuses_commit(cit, monkeypatch, clock):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    if clock == 'naive':
        monkeypatch.setattr(citations, 'utcnow', lambda: datetime(2026, 10, 10, 12))
    else:
        reads = iter([NOW, NOW - timedelta(seconds=1)])
        monkeypatch.setattr(citations, 'utcnow', lambda: next(reads))
    assert run() == {'halted:citation_clock_invalid': 1}
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 0


def test_citation_lock_timeout_is_busy_not_a_fault(cit, monkeypatch):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011'])
    monkeypatch.setenv('RAS_RECOVERY__GATE_LOCK_TIMEOUT_MS', '200')
    get_settings.cache_clear()
    with Session(get_engine()) as holder:  # a discovery re-target holding the exclusive citation lock
        holder.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': LINK_KEY})
        started = time.monotonic()
        assert run() == {'busy:citation_lock': 1}
        assert time.monotonic() - started < 5
        from app.cli import reconcile_citations as cli
        assert cli.main([]) == 0  # busy is reported, never a halt
        holder.rollback()
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 0
    assert run() == {'batches': 1, 'references': 1, 'links_written': 1}


def test_busy_common_item_is_skipped_without_blocking_later_candidates(cit, monkeypatch):
    h = cit
    copy = common_copy(h.w, indexed=False)  # one pending reference on a common copy
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, ['arXiv:2609.00011', 'arXiv:2609.00012', 'arXiv:2609.00013'])
    with h.w.engine.connect() as holder:  # labelled: a revoke/extraction transaction holding the item FOR UPDATE
        holder.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'), {'i': copy.item})
        started = time.monotonic()
        result = run(reconcile_batch_size=1, reconcile_max_batches=10)
        assert time.monotonic() - started < 2  # SKIP LOCKED: never waited for the item
        holder.rollback()
    assert result == {'batches': 3, 'references': 3, 'links_written': 3, 'skipped:busy': 1}
    assert run() == {'batches': 1, 'references': 1, 'links_written': 1}  # the next pass takes it


def test_batch_bounds_and_config_validation(cit):
    h = cit
    a = source(h.w, h.aid, 'PRIVATE')
    refs(h, a, [f'arXiv:2609.0001{n}' for n in range(5)])
    assert run(reconcile_batch_size=2, reconcile_max_batches=2) == {
        'batches': 2, 'references': 4, 'links_written': 4, 'bound:batches': 1}
    assert run(reconcile_batch_size=2, reconcile_max_batches=2) == {'batches': 1, 'references': 1, 'links_written': 1}
    assert run(reconcile_batch_size=5, reconcile_max_batches=1) == {}
    for bad in ({'reconcile_batch_size': 0}, {'reconcile_batch_size': 5001}, {'reconcile_max_batches': 0},
                {'reconcile_max_batches': 1001}, {'reconcile_poll_seconds': 29}):
        with pytest.raises(ValidationError):
            CitationPolicy(**bad)
    edge = CitationPolicy(reconcile_batch_size=5000, reconcile_max_batches=1000, reconcile_poll_seconds=30)
    assert (edge.reconcile_batch_size, edge.reconcile_max_batches) == (5000, 1000)


# --- concurrency ---------------------------------------------------------------------------------

def test_two_concurrent_passes_one_row_and_one_counted_write_per_reference(cit, monkeypatch):
    h = cit
    page(h, SEED)
    a, b = source(h.w, h.aid, 'A'), source(h.w, h.bid, 'B')
    ids = refs(h, a, [raw for raw, *_ in MD_REFS]) + refs(h, b, [raw for raw, *_ in MD_REFS])
    barrier = Barrier(2, timeout=20)
    real = citations._lock

    def lock(db, *, exclusive):
        if not exclusive and threading.current_thread().name.startswith('pass'):
            barrier.wait()  # both passes hold the same candidates before the citation lock
        return real(db, exclusive=exclusive)
    monkeypatch.setattr(citations, '_lock', lock)
    runs = [thread(lambda: run(reconcile_batch_size=30, reconcile_max_batches=1), f'pass-{n}') for n in range(2)]
    for t, _ in runs:
        t.join(60)
    results = [box.get('result', box.get('error')) for _, box in runs]
    assert all(isinstance(r, dict) and not any(k.startswith(('halted', 'busy')) for k in r) for r in results), results
    assert [r['references'] for r in results] == [30, 30]  # both examined every candidate
    assert sum(r.get('links_written', 0) for r in results) == 30  # an identical row is never counted twice
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 30 == len(set(ids))


def test_revoke_first_holds_item_backfill_skips_then_never_writes(cit, monkeypatch):
    h = cit
    copy = common_copy(h.w, indexed=False)
    from app import publication
    pause, real = Pause(), publication._cancel_jobs

    def cancel(db, item_id, at):
        pause()  # the revoke (its executor thread) holds the common item FOR UPDATE here
        return real(db, item_id, at)
    monkeypatch.setattr(publication, '_cancel_jobs', cancel)
    t, box = thread(lambda: runtime_revoke(h.w, copy.item), 'revoke')
    assert pause.reached.wait(20)
    started = time.monotonic()
    assert run() == {'skipped:busy': 1}  # SKIP LOCKED: no wait, no fence error, nothing written
    assert time.monotonic() - started < 2
    pause.go.set()
    t.join(30)
    assert 'error' not in box, box
    assert q(h, 'SELECT publication_state FROM library_items WHERE id=:i', i=copy.item) == 'revoked'
    assert run() == {}  # closed: no longer a candidate
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 0


def test_backfill_first_holds_key_share_revoke_waits_then_links_precede_t_rev(cit, monkeypatch):
    h = cit
    copy = common_copy(h.w, indexed=False)
    pause, real = Pause(), citations._lock

    def lock(db, *, exclusive):
        if threading.current_thread().name == 'backfill':
            pause()  # items KEY SHARE and references SHARE are held; citation lock not yet
        return real(db, exclusive=exclusive)
    monkeypatch.setattr(citations, '_lock', lock)
    b, bbox = thread(run, 'backfill')
    assert pause.reached.wait(20)
    r, rbox = thread(lambda: runtime_revoke(h.w, copy.item), 'revoke')
    eventually(lambda: lock_waiters(h, 'FOR UPDATE'), 'revoke did not wait for the item held by the backfill')
    assert not advisory(h)  # nobody holds the citation lock while the revoke waits on the item
    pause.go.set()
    b.join(30)
    r.join(30)
    assert bbox.get('result') == {'batches': 1, 'references': 1, 'links_written': 1}, bbox
    assert 'error' not in rbox, rbox
    # The batch committed before t_rev; from t_rev the 0024 fence refuses any further link write.
    assert q(h, 'SELECT publication_state FROM library_items WHERE id=:i', i=copy.item) == 'revoked'
    assert q(h, 'SELECT count(*) FROM reference_citation_links') == 1
    with pytest.raises(Exception, match='revoked common copy'):
        with h.w.runtime.begin() as c:
            c.execute(text("UPDATE reference_citation_links SET reconciled_at=now()"))


@pytest.mark.parametrize('first', ['retarget', 'revoke'])
def test_discovery_retarget_and_revoke_both_orders_item_before_citation_lock(cit, monkeypatch, first):
    h = cit
    copy = common_copy(h.w, indexed=False)  # reference 'arXiv:2609.00011'
    a = source(h.w, h.aid, 'PRIVATE')
    private = refs(h, a, ['arXiv:2609.00012'])
    assert run() == {'batches': 1, 'references': 2, 'links_written': 2}  # both no_discovery_record
    from app import publication
    pause = Pause()
    real_lock, real_cancel = citations._lock, publication._cancel_jobs

    def lock(db, *, exclusive):
        result = real_lock(db, exclusive=exclusive)
        if exclusive and first == 'retarget' and threading.current_thread().name == 'discovery':
            pause()  # holds the common item KEY SHARE and the exclusive citation lock
        return result

    def cancel(db, item_id, at):
        if first == 'revoke':
            pause()  # the revoke executor thread holds the common item FOR UPDATE
        return real_cancel(db, item_id, at)
    monkeypatch.setattr(citations, '_lock', lock)
    monkeypatch.setattr(publication, '_cancel_jobs', cancel)
    work = lambda: page(h, SEED, at=NOW + timedelta(minutes=1))  # noqa: E731
    if first == 'retarget':
        d, dbox = thread(work, 'discovery')
        assert pause.reached.wait(20)
        r, rbox = thread(lambda: runtime_revoke(h.w, copy.item), 'revoke')
        eventually(lambda: lock_waiters(h, 'FOR UPDATE'), 'revoke did not wait for the retarget item lock')
    else:
        r, rbox = thread(lambda: runtime_revoke(h.w, copy.item), 'revoke')
        assert pause.reached.wait(20)
        d, dbox = thread(work, 'discovery')
        waiter = eventually(lambda: lock_waiters(h, 'FOR KEY SHARE'), 'discovery did not wait for the item')
        # Waiting for the item WITHOUT the citation lock: extraction/backfill stay free meanwhile.
        assert all(pid not in {p for p, *_ in advisory(h)} for pid, _ in waiter)
        assert run() == {}  # private link already fresh; shared lock was available (no busy)
    pause.go.set()
    d.join(30)
    r.join(30)
    assert 'error' not in dbox and 'error' not in rbox, (dbox, rbox)
    status = {rid: row.match_status for rid, row in links(h).items()}
    assert status[private[0]] == 'linked'
    common_status = q(h, """SELECT l.match_status FROM reference_citation_links l JOIN reference_entries r
        ON r.id=l.reference_id WHERE r.item_id=:i""", i=copy.item)
    # Retarget first: committed before t_rev. Revoke first: the closed copy is never re-targeted.
    assert common_status == ('linked' if first == 'retarget' else 'no_discovery_record')
    assert q(h, 'SELECT publication_state FROM library_items WHERE id=:i', i=copy.item) == 'revoked'


def test_extraction_shape_holding_common_item_and_discovery_never_deadlock(cit, monkeypatch):
    """Extraction contract: item FOR UPDATE (worker) -> shared citation lock in the hook.

    The discovery page must wait for that item before its exclusive lock; the old
    order (exclusive lock, then the 0024 fence's KEY SHARE) deadlocks here.
    """
    h = cit
    copy = common_copy(h.w, indexed=False)
    assert run() == {'batches': 1, 'references': 1, 'links_written': 1}
    with Session(get_engine()) as worker:  # extraction transaction shape, runtime login
        worker.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'), {'i': copy.item})
        d, dbox = thread(lambda: page(h, SEED, at=NOW + timedelta(minutes=1)), 'discovery')
        eventually(lambda: lock_waiters(h, 'FOR KEY SHARE'), 'discovery did not wait for the item first')
        assert not advisory(h)
        assert citations.reconcile_references(worker, source_file_id=copy.file) == 1  # takes the shared lock
        worker.commit()
    d.join(30)
    assert 'error' not in dbox, dbox
    assert q(h, """SELECT l.match_status FROM reference_citation_links l JOIN reference_entries r
        ON r.id=l.reference_id WHERE r.item_id=:i""", i=copy.item) == 'linked'


def test_revoke_committing_between_hint_and_lock_is_rechecked_under_lock(cit, monkeypatch):
    h = cit
    copy = common_copy(h.w, indexed=False)
    a = source(h.w, h.aid, 'PRIVATE')
    private = refs(h, a, ['arXiv:2609.00012'])
    pause, real = Pause(), citations._key_share

    def key_share(db, item_ids, **kw):
        if kw.get('skip') and threading.current_thread().name == 'backfill':
            pause()  # the hint already chose the common reference; no lock taken yet
        return real(db, item_ids, **kw)
    monkeypatch.setattr(citations, '_key_share', key_share)
    t, box = thread(run, 'backfill')
    assert pause.reached.wait(20)
    runtime_revoke(h.w, copy.item)  # t_rev commits while the backfill holds no item lock
    pause.go.set()
    t.join(30)
    # The stale hint is dropped under the item lock: no fence error, the private batch still commits.
    assert box.get('result') == {'batches': 1, 'references': 1, 'links_written': 1, 'skipped:revoked': 1}, box
    assert list(links(h)) == private


def test_late_common_item_under_exclusive_lock_is_nowait_busy(cit, monkeypatch):
    h = cit
    copy = common_copy(h.w, indexed=False)
    real = citations._lock
    holder = {}

    def lock(db, *, exclusive):
        result = real(db, exclusive=exclusive)
        if exclusive:
            # Labelled: a link appeared after the hint (committed out of band) and its item is now held.
            with h.w.engine.begin() as o:
                o.execute(text("""INSERT INTO reference_citation_links(reference_id,matcher_version,raw_citation_sha256,
                    match_status,match_method,id_form,arxiv_id,id_start_offset,id_end_offset)
                    SELECT id,:m,encode(sha256(convert_to(raw_citation,'UTF8')),'hex'),'no_discovery_record',
                    'arxiv_id_exact','arxiv_prefix','2609.00011',6,16 FROM reference_entries WHERE item_id=:i"""),
                    {'m': citations.MATCHER_VERSION, 'i': copy.item})
            c = holder['c'] = h.w.engine.connect()
            c.execute(text('SELECT id FROM library_items WHERE id=:i FOR UPDATE'), {'i': copy.item})
        return result
    monkeypatch.setattr(citations, '_lock', lock)
    from app.config import DiscoveryPolicy
    from app.discovery import run_sync
    before = q(h, "SELECT row(window_from, window_until, resumption_token, pages_seen)::text FROM discovery_sources"
                  " WHERE topic='cs.AI'")
    started = time.monotonic()
    result = run_sync(get_engine(), DiscoveryPolicy(topics=['cs.AI'], max_pages_per_topic=1),
                      fetcher=lambda *_: oai_page(SEED), now=NOW, force=True)
    assert time.monotonic() - started < 5  # NOWAIT: never waited under the exclusive lock
    holder['c'].rollback()
    holder['c'].close()
    assert result == {'busy': False, 'pages': 0, 'records': 0, 'requests': 1, 'error:citation_retarget_busy': 1}
    # Nothing partial: no paper, no link update, and the page cursor did not advance.
    assert q(h, 'SELECT count(*) FROM discovery_papers') == 0
    assert q(h, "SELECT match_status FROM reference_citation_links") == 'no_discovery_record'
    assert q(h, "SELECT last_error FROM discovery_sources WHERE topic='cs.AI'") == 'citation_retarget_busy'
    after = q(h, "SELECT row(window_from, window_until, resumption_token, pages_seen)::text FROM discovery_sources"
                 " WHERE topic='cs.AI'")
    assert after.split(',')[2:] == before.split(',')[2:]  # same token and page counter
