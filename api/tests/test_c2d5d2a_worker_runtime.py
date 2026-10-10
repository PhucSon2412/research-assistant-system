"""Real runtime PG, MinIO, journal, clocks/locks. Only cutover is scoped."""
import hashlib
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app import auth_safety, recovery
from app.config import get_settings
from app.db import get_engine
from app.ingest import worker, checkpoints, runtime
from app.models import IngestionJob, SourceFile
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_uploads import upload_api  # noqa: F401
from tests.test_ingest_worker import upload
from tests.test_auth_safety_foundation import NOW, append_changes
from tests.test_pdf_ingest import full_pdf, partial_pdf, pdf_bytes, upload_pdf

BODY = b'# Worker runtime\n\n## Methods\nA measured result.\n\n## References\n[1] arXiv:2609.00011\n'


@pytest.fixture
def work(upload_api, installation, monkeypatch):
    api, storage = upload_api
    uid = api.create_user('worker@example.test')
    browser = api.user_browser('worker@example.test')
    ids = upload(browser, BODY)
    job, item, file = (uuid.UUID(ids[k]) for k in ('job_id','item_id','file_id'))
    with Session(api.engine) as db:
        key = db.get(SourceFile, file).storage_key
    b = SimpleNamespace(api=api, storage=storage, uid=uid, browser=browser, job=job,
        item=item, file=file, key=key, owner=installation.engine, path=installation.path, opened=[])
    original = storage.open
    def observed(key):
        assert get_engine().pool.checkedout() == 0, 'connection retained during storage IO'
        b.opened.append(key)
        return original(key)
    monkeypatch.setattr(storage, 'open', observed)
    monkeypatch.setattr(worker, 'utcnow', lambda: NOW)
    return b


def sql(b, query, **params):
    with b.owner.begin() as conn:  # labelled fixture/adversary, never worker fallback
        result = conn.execute(text(query), params)
        return result.scalar() if result.returns_rows else None


def run(b):
    return worker.process_job(b.job, storage=b.storage)


def owner_status(b, decision):
    w = SimpleNamespace(uid=b.uid, engine=b.owner, path=b.path, settings=get_settings())
    append_changes(w, [auth_safety.Change(b.uid, 'status', 'admin_disable' if decision == 'disabled'
        else 'admin_enable', NOW+timedelta(seconds=1), decision=decision)], engine=b.api.engine)


def test_actual_runtime_no_provider_and_exact_locators(work, monkeypatch):
    b = work
    with Session(b.api.engine) as db:
        runtime.require_runtime(db)
        assert db.scalar(text('SELECT current_user')) == 'c1_runtime'
        assert not db.scalar(text("SELECT has_table_privilege('users','UPDATE')"))
    from app.ingest import indexer
    monkeypatch.setattr(indexer, 'process_index', lambda *a, **kw: pytest.fail('legacy indexer'))
    original_extract = worker.extract
    def checked_extract(original):
        assert get_engine().pool.checkedout() == 0
        return original_extract(original)
    monkeypatch.setattr(worker, 'extract', checked_extract)
    assert run(b) == 'awaiting_index'
    assert len(b.opened) == 3
    assert sql(b, 'SELECT finished_at FROM ingestion_jobs WHERE id=:i', i=b.job) is None
    assert sql(b, 'SELECT extraction_finished_at IS NOT NULL FROM ingestion_jobs WHERE id=:i', i=b.job)
    assert sql(b, 'SELECT lease_token FROM ingestion_jobs WHERE id=:i', i=b.job) is None
    assert sql(b, 'SELECT content_status FROM library_items WHERE id=:i', i=b.item) == 'metadata_only'
    for table in ('index_entries','index_generations','embedding_calls'):
        assert sql(b, f'SELECT count(*) FROM {table}') == 0
    with b.owner.connect() as c:
        chunks = c.execute(text('SELECT text,start_offset,end_offset,source_file_id FROM chunks')).all()
    assert chunks and all(BODY.decode()[a:z] == t and f == b.file for t,a,z,f in chunks)
    with b.owner.connect() as c:
        evidence = c.scalars(text("SELECT c.text FROM chunks c JOIN sections s ON s.id=c.section_id WHERE s.kind IN ('body','abstract')")).all()
    assert evidence and all('arXiv:' not in t for t in evidence)


@pytest.mark.parametrize('state,resume', [('awaiting_index',None),('indexing',None),('retryable_failed','indexing')])
def test_index_states_untouched_even_with_key_and_price(work, state, resume):
    b = work
    sql(b, 'UPDATE ingestion_jobs SET state=:s,resume_state=:r WHERE id=:i', s=state,r=resume,i=b.job)
    settings = get_settings().model_copy(deep=True)
    from pydantic import SecretStr
    settings.embedding.api_key = SecretStr('test-never-dispatch')
    settings.embedding.pricing_verified = True  # local copy only, not environment/production config
    with Session(b.api.engine) as db:
        assert b.job not in worker.extraction_due_job_ids(db, settings)
    # The extraction boundary still rejects index states; document dispatch is
    # now separate. Missing price keeps the durable state/attempt untouched.
    settings.embedding.pricing_verified = False
    assert worker.process_job(b.job, settings, b.storage) == 'embedding_pricing_unverified'
    assert not b.opened
    assert sql(b, 'SELECT state FROM ingestion_jobs WHERE id=:i', i=b.job) == state
    assert sql(b, 'SELECT attempt_count FROM ingestion_jobs WHERE id=:i', i=b.job) == 0


@pytest.mark.parametrize('gate', ['pending','missing','hold'])
def test_gate_before_claim_no_side_effect(work, monkeypatch, gate):
    b = work
    if gate == 'pending': monkeypatch.setattr(auth_safety, 'cutover', lambda db:'auth_safety_pending')
    elif gate == 'missing': b.path.unlink()
    else: recovery.hold(b.owner, get_settings(), NOW)
    assert run(b) == 'recovery_gate_closed'
    assert not b.opened
    assert sql(b, 'SELECT attempt_count FROM ingestion_jobs WHERE id=:i', i=b.job) == 0
    from app.ingest import celery_app as tasks
    monkeypatch.setattr(tasks, 'settings', get_settings())
    assert tasks.discover.run() == 0
    assert tasks.ingest_job.run(str(b.job)) == 'recovery_gate_closed'
    assert tasks.sync_arxiv.run() == {'skipped':'recovery_gate_closed'}
    assert tasks.reconcile_citation_links.run() == {'skipped':'recovery_gate_closed'}
    assert tasks.cleanup_revoked_copies.run() == {'skipped':'recovery_gate_closed'}
    assert b.browser.get('/healthz').status_code == 200
    assert b.browser.get('/readyz').status_code == 503


@pytest.mark.parametrize('role', ['owner','recovery','missing_grant'])
def test_wrong_runtime_role_no_fallback(work, monkeypatch, role):
    from app.db import make_engine
    b = work
    engine = b.owner
    if role == 'recovery':
        import os
        engine = make_engine(b.owner.url.set(username='c1_recovery',password=os.environ['RAS_C1_RECOVERY_PASSWORD']))
    elif role == 'missing_grant':
        sql(b, 'REVOKE EXECUTE ON FUNCTION ras_auth_status_live(uuid) FROM c1_runtime')
        engine = b.api.engine
    monkeypatch.setattr(worker, 'get_sessionmaker', lambda: lambda: Session(engine))
    assert run(b) == 'worker_runtime_required'
    assert not b.opened
    assert sql(b, 'SELECT attempt_count FROM ingestion_jobs WHERE id=:i', i=b.job) == 0
    if role == 'recovery': engine.dispose()


@pytest.mark.parametrize('phase', ['storage','parser'])
@pytest.mark.parametrize('change', ['hold','disable','epoch','lease','cancel','binding'])
def test_change_during_unlocked_io_discards_result(work, monkeypatch, phase, change):
    b = work
    hit = False
    original_open, original_extract = b.storage.open, worker.extract
    def alter():
        nonlocal hit
        if hit: return
        hit = True
        assert get_engine().pool.checkedout() == 0
        if change == 'hold': recovery.hold(b.owner, get_settings(), NOW)
        elif change == 'disable': owner_status(b, 'disabled')
        elif change == 'epoch': sql(b, 'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',i=b.item)
        elif change == 'lease': sql(b, 'UPDATE ingestion_jobs SET lease_token=:t WHERE id=:i', t=uuid.uuid4(),i=b.job)
        elif change == 'cancel': sql(b, 'UPDATE ingestion_jobs SET cancel_requested=true,cancel_requested_at=:at WHERE id=:i',at=NOW,i=b.job)
        else:
            # Labelled owner corruption: ordinary writers cannot change pinned source fields.
            with b.owner.begin() as c:
                c.execute(text('ALTER TABLE source_files DISABLE TRIGGER USER'))
                c.execute(text("UPDATE source_files SET storage_key=storage_key||'-changed' WHERE id=:i"),{'i':b.file})
                c.execute(text('ALTER TABLE source_files ENABLE TRIGGER USER'))
    def opened(key):
        if len(b.opened) == 1: alter()  # second read, extraction
        return original_open(key)
    def parsed(original):
        alter()
        return original_extract(original)
    monkeypatch.setattr(b.storage, 'open', opened)
    if phase == 'parser':
        monkeypatch.setattr(b.storage, 'open', original_open)
        monkeypatch.setattr(worker, 'extract', parsed)
    expected = {'hold':'recovery_gate_closed','disable':'owner_status_paused','epoch':'cancelled',
        'cancel':'cancelled','lease':'lease_lost','binding':'worker_binding_changed'}[change]
    assert run(b) == expected
    assert hit
    assert sql(b, 'SELECT count(*) FROM sections WHERE item_id=:i',i=b.item) == 0
    assert sql(b, 'SELECT count(*) FROM chunks WHERE item_id=:i',i=b.item) == 0
    assert sql(b, 'SELECT finished_at FROM ingestion_jobs WHERE id=:i',i=b.job) is None
    assert len(b.opened) == 2


def test_disabled_owner_pauses_then_resumes_without_worker_enabling(work):
    b = work
    owner_status(b, 'disabled')
    assert run(b) == 'owner_status_paused' and not b.opened
    with Session(b.api.engine) as db: assert b.job not in worker.due_job_ids(db,get_settings())
    assert sql(b,'SELECT status FROM users WHERE id=:i',i=b.uid) == 'disabled'
    owner_status(b, 'active')  # trusted status-writer fixture, never extraction
    assert run(b) == 'awaiting_index'


@pytest.mark.parametrize('stage', ['claim','begin','finish','failure'])
@pytest.mark.parametrize('landed', [False,True])
def test_uncertain_commit_stops_without_failure_or_following_io(work, monkeypatch, stage, landed):
    b = work
    commit_number = {'claim':1,'begin':2,'finish':3,'failure':3}[stage]
    observed = []
    original_commit = runtime.commit
    def record(db, outcome):
        try: return original_commit(db,outcome)
        finally: observed.append(outcome)
    monkeypatch.setattr(runtime,'commit',record)
    class Fault(Session):
        commits = 0
        def commit(self):
            self.commits += 1
            if self.commits == commit_number:
                super().commit() if landed else super().rollback()
                raise RuntimeError('lost acknowledgement')
            return super().commit()
    monkeypatch.setattr(worker,'get_sessionmaker',lambda:lambda:Fault(b.api.engine,expire_on_commit=False))
    if stage == 'failure':
        def unavailable(key):
            b.opened.append(key)
            raise OSError('storage error with private marker')
        monkeypatch.setattr(b.storage,'open',unavailable)
    assert run(b) == 'worker_commit_uncertain'
    assert observed[-1].commit_started and not observed[-1].committed
    assert len(b.opened) == (0 if stage in ('claim','begin') else 1)
    assert sql(b,'SELECT count(*) FROM chunks') == 0
    assert sql(b,"SELECT count(*) FROM ingest_step_attempts WHERE outcome='success'") == (1 if stage=='finish' and landed else 0)


@pytest.mark.parametrize('fault', ['rollback','close','both','invalidate'])
def test_known_commit_release_fault_stops_before_io(work, monkeypatch, fault):
    b = work
    outcomes, invalidated = [], []
    original = runtime.commit
    def record(db, outcome):
        result = original(db,outcome)
        outcomes.append(outcome)
        return result
    monkeypatch.setattr(runtime,'commit',record)
    class Fault(Session):
        def rollback(self):
            super().rollback()
            if outcomes and fault in ('rollback','both','invalidate'): raise RuntimeError('release failure')
        def close(self):
            super().close()
            if outcomes and fault in ('close','both'): raise RuntimeError('close failure')
        def invalidate(self):
            invalidated.append(True)
            super().invalidate()
            if fault == 'invalidate': raise RuntimeError('invalidate failure')
    monkeypatch.setattr(worker,'get_sessionmaker',lambda:lambda:Fault(b.api.engine,expire_on_commit=False))
    assert run(b) == 'worker_release_unavailable'
    assert len(outcomes)==1 and outcomes[0].committed and outcomes[0].commit_started
    assert invalidated and not b.opened
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==1


@pytest.mark.parametrize('checkpoint', ['begin','finish','failure'])
@pytest.mark.parametrize('expiry', ['equal','after'])
def test_fresh_lease_clock_after_final_sql(work, monkeypatch, checkpoint, expiry):
    b=work
    with Session(b.api.engine,expire_on_commit=False) as db:
        token=worker.claim_job(db,b.job,get_settings())
        if checkpoint!='begin': source=worker._begin(db,b.job,token,'validate')
        deadline=sql(b,'SELECT lease_until FROM ingestion_jobs WHERE id=:i',i=b.job)
        def late(c,cur,stmt,params,ctx,many):
            if 'ras_auth_status_live' in stmt:
                monkeypatch.setattr(worker,'utcnow',lambda:deadline+timedelta(microseconds=expiry=='after'))
        # Arm only after the final journal boundary, so initial lease proof is valid.
        original=auth_safety.require_final
        def arm(db,**kw):
            original(db,**kw)
            event.listen(b.api.engine,'after_cursor_execute',late)
        monkeypatch.setattr(auth_safety,'require_final',arm)
        try:
            with pytest.raises(runtime.Halt,match='lease_lost'):
                if checkpoint=='begin': worker._begin(db,b.job,token,'validate')
                elif checkpoint=='finish': worker._validate(db,b.job,token,source,b.storage,get_settings())
                else: worker._fail(db,b.job,token,'validate',worker.StepError('storage_unavailable',True),get_settings())
        finally: event.remove(b.api.engine,'after_cursor_execute',late)
    assert sql(b,'SELECT state FROM ingestion_jobs WHERE id=:i',i=b.job)=='validating'
    assert sql(b,'SELECT finished_at FROM ingestion_jobs WHERE id=:i',i=b.job) is None


def test_same_size_overwrite_hash_fences(work):
    b=work
    b.storage.put(b.key,io.BytesIO(BODY.replace(b'measured',b'altered!')),'text/markdown')
    assert run(b)=='checksum_mismatch'
    assert sql(b,'SELECT count(*) FROM sections')==0


def test_dispatch_rechecks_between_select_send_and_broker_failure(work, monkeypatch):
    from app.ingest import celery_app as tasks
    b=work
    monkeypatch.setattr(tasks,'settings',get_settings())
    sent=[]
    monkeypatch.setattr(tasks.ingest_job,'delay',lambda *args:sent.append(args))
    original=tasks.due_job_ids
    def close_after_select(db, settings):
        ids=original(db,settings)
        recovery.hold(b.owner,get_settings(),NOW)
        return ids
    monkeypatch.setattr(tasks,'due_job_ids',close_after_select)
    assert tasks.discover.run()==0 and sent==[]
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==0


def test_duplicate_dispatch_payload_and_single_claim(work, monkeypatch):
    from app.ingest import celery_app as tasks
    b=work
    monkeypatch.setattr(tasks,'settings',get_settings())
    sent=[]
    monkeypatch.setattr(tasks.ingest_job,'delay',lambda *args:sent.append(args))
    assert tasks.discover.run()==1 and sent==[(str(b.job),)]
    def claim():
        with Session(b.api.engine) as db:return worker.claim_job(db,b.job,get_settings())
    with ThreadPoolExecutor(2) as pool: tokens=list(pool.map(lambda _:claim(),range(2)))
    assert sum(t is not None for t in tokens)==1
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==1


@pytest.mark.parametrize('kind,expected', [('full','awaiting_index'),('partial','awaiting_index'),('scan','needs_review'),('uncertain','needs_review')])
def test_pdf_real_parser_and_coverage(work, kind, expected):
    b=work
    data={'full':full_pdf,'partial':partial_pdf,'scan':lambda:pdf_bytes([[]]),
          'uncertain':lambda:pdf_bytes([['Introduction']+['A substantial observation. '*4]*25+['References to Earlier Work','[1] Citation.']])}[kind]()
    ids=upload_pdf(b.browser,data)
    assert worker.process_job(uuid.UUID(ids['job_id']),storage=b.storage)==expected
    assert sql(b,'SELECT content_status FROM library_items WHERE id=:i',i=uuid.UUID(ids['item_id']))=='metadata_only'


def test_final_gate_after_io_no_derived_commit(work,monkeypatch):
    b=work
    original=worker.extract
    def parsed(text):
        result=original(text)
        monkeypatch.setattr(auth_safety,'cutover',lambda db:'auth_safety_pending')
        return result
    monkeypatch.setattr(worker,'extract',parsed)
    assert run(b)=='recovery_gate_closed'
    assert sql(b,'SELECT count(*) FROM sections')==0


def test_gate_closes_after_dml_before_final_boundary(work):
    b=work; held=False
    def hold(c,cur,stmt,params,ctx,many):
        nonlocal held
        if stmt=='SET CONSTRAINTS ALL IMMEDIATE' and not held:
            held=True
            recovery.hold(b.owner,get_settings(),NOW)
    event.listen(get_engine(),'after_cursor_execute',hold)
    try:assert run(b)=='recovery_gate_closed'
    finally:event.remove(get_engine(),'after_cursor_execute',hold)
    assert held and not b.opened
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==0


def test_owner_lock_wait_past_lease_cannot_renew(work,monkeypatch):
    b=work
    from app.identity.predicates import lock_users
    with Session(b.api.engine) as db:token=worker.claim_job(db,b.job,get_settings())
    deadline=sql(b,'SELECT lease_until FROM ingestion_jobs WHERE id=:i',i=b.job)
    def begin():
        with Session(b.api.engine) as db:
            try:worker._begin(db,b.job,token,'validate')
            except runtime.Halt as exc:return exc.code
    from tests.test_c2d5a_review_writes import wait_for
    with Session(b.api.engine) as blocker,ThreadPoolExecutor(1) as pool:
        lock_users(blocker,[b.uid])
        future=pool.submit(begin)
        try:
            assert wait_for(b.owner,'transactionid')
            monkeypatch.setattr(worker,'utcnow',lambda:deadline)
        finally:blocker.rollback()
        assert future.result(10)=='lease_lost'
    assert sql(b,'SELECT count(*) FROM ingest_step_attempts')==0 and not b.opened


def test_stale_token_cannot_finish_or_fail_after_replacement(work,monkeypatch):
    b=work
    with Session(b.api.engine) as old:
        token=worker.claim_job(old,b.job,get_settings())
        source=worker._begin(old,b.job,token,'validate')
        deadline=sql(b,'SELECT lease_until FROM ingestion_jobs WHERE id=:i',i=b.job)
        monkeypatch.setattr(worker,'utcnow',lambda:deadline)
        with Session(b.api.engine) as new:
            replacement=worker.claim_job(new,b.job,get_settings())
        assert replacement and replacement!=token
        for action in (lambda:worker._validate(old,b.job,token,source,b.storage,get_settings()),
            lambda:worker._fail(old,b.job,token,'validate',worker.StepError('storage_unavailable',True),get_settings())):
            with pytest.raises(runtime.Halt,match='lease_lost'):action()
    assert sql(b,'SELECT lease_token FROM ingestion_jobs WHERE id=:i',i=b.job)==replacement
    assert sql(b,'SELECT state FROM ingestion_jobs WHERE id=:i',i=b.job)=='validating'
    assert sql(b,'SELECT count(*) FROM sections')==0
    assert not b.opened


def test_retry_reuses_exact_source_without_duplicate_rows(work,monkeypatch):
    b=work
    original=runtime.commit
    commits=[]
    def lose_extract_ack(db,outcome):
        commits.append(outcome)
        if len(commits)==5:
            db.commit()
            outcome.commit_started=True
            raise runtime.Halt('worker_commit_uncertain')
        return original(db,outcome)
    monkeypatch.setattr(runtime,'commit',lose_extract_ack)
    assert run(b)=='worker_commit_uncertain'
    before=sql(b,'SELECT count(*) FROM sections')
    assert before>0 and sql(b,'SELECT state FROM ingestion_jobs WHERE id=:i',i=b.job)=='chunking'
    monkeypatch.setattr(runtime,'commit',original)
    monkeypatch.setattr(worker,'utcnow',lambda:NOW+timedelta(seconds=get_settings().ingest.lease_seconds))
    assert run(b)=='awaiting_index'
    assert sql(b,'SELECT count(*) FROM sections')==before
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==2


def test_exhausted_extraction_is_gated_and_does_not_touch_index_ledger(work):
    b=work
    sql(b,'UPDATE ingestion_jobs SET attempt_count=:n WHERE id=:i',n=get_settings().ingest.max_attempts,i=b.job)
    assert run(b)=='not_claimed' and not b.opened
    assert sql(b,'SELECT last_error_code FROM ingestion_jobs WHERE id=:i',i=b.job)=='attempts_exhausted'
    assert sql(b,'SELECT finished_at FROM ingestion_jobs WHERE id=:i',i=b.job) is not None
    assert sql(b,'SELECT count(*) FROM embedding_calls')==0


def test_broker_send_fault_keeps_durable_job_for_next_tick(work,monkeypatch):
    from app.ingest import celery_app as tasks
    b=work;sent=[]
    monkeypatch.setattr(tasks,'settings',get_settings())
    def failed(*a):raise OSError('broker down')
    monkeypatch.setattr(tasks.ingest_job,'delay',failed)
    assert tasks.discover.run()==0
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==0
    monkeypatch.setattr(tasks.ingest_job,'delay',lambda *args:sent.append(args))
    assert tasks.discover.run()==1 and sent==[(str(b.job),)]


@pytest.mark.parametrize('corrupt',['held','head','receipt'])
def test_private_status_requires_latest_head_and_receipt(work,corrupt):
    b=work
    with b.owner.begin() as conn:  # labelled adversary snapshot, not a runtime status writer
        table={'held':'users','head':'auth_safety_heads','receipt':'auth_change_receipts'}[corrupt]
        conn.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER ALL'))
        if corrupt=='held':conn.execute(text("UPDATE users SET status_safety='held' WHERE id=:i"),{'i':b.uid})
        elif corrupt=='head':conn.execute(text('UPDATE auth_safety_heads SET status_generation=:g WHERE user_id=:i'),{'i':b.uid,'g':uuid.uuid4()})
        else:conn.execute(text("DELETE FROM auth_change_receipts WHERE user_id=:i AND domain='status'"),{'i':b.uid})
        conn.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER ALL'))
    assert run(b)=='owner_status_paused' and not b.opened
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==0


def test_wrong_password_is_controlled_without_fallback(work,monkeypatch):
    from app.db import make_engine
    b=work;bad=make_engine(b.api.engine.url.set(password=uuid.uuid4().hex))
    monkeypatch.setattr(worker,'get_sessionmaker',lambda:lambda:Session(bad))
    try:assert run(b)=='worker_runtime_unavailable'
    finally:bad.dispose()
    assert not b.opened and sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==0


def test_missing_worker_connection_parts_are_controlled(work,monkeypatch):
    from app.ingest import celery_app as tasks
    monkeypatch.setattr(tasks,'settings',get_settings().model_copy(update={'db':None}))
    with pytest.raises(SystemExit,match='worker_runtime_configuration_required'):
        tasks.require_runtime_startup()
    assert not work.opened


def test_cancelled_storage_read_closes_body_without_failure_checkpoint(work,monkeypatch):
    b=work;closed=[]
    class Body:
        def read(self,*args):raise KeyboardInterrupt()
        def close(self):closed.append(True)
    monkeypatch.setattr(b.storage,'open',lambda key:Body())
    with pytest.raises(KeyboardInterrupt):run(b)
    assert closed==[True] and get_engine().pool.checkedout()==0
    assert sql(b,'SELECT state FROM ingestion_jobs WHERE id=:i',i=b.job)=='validating'
    assert sql(b,'SELECT last_error_code FROM ingestion_jobs WHERE id=:i',i=b.job) is None


def test_parser_child_timeout_is_killed_waited_and_temp_removed(work,monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from app.ingest import pdf
    b=work;ids=upload_pdf(b.browser,full_pdf());children=[];directories=[]
    real_run,real_popen=subprocess.run,subprocess.Popen
    def spawn(*args,**kw):
        child=real_popen(*args,**kw);children.append(child);return child
    def timeout(args,**kw):
        assert get_engine().pool.checkedout()==0
        directories.append(Path(args[4]).parent)
        return real_run([sys.executable,'-c','import time;time.sleep(10)'],timeout=.05,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    monkeypatch.setattr(subprocess,'Popen',spawn)
    monkeypatch.setattr(pdf.subprocess,'run',timeout)
    assert worker.process_job(uuid.UUID(ids['job_id']),storage=b.storage)=='pdf_parser_timeout'
    assert children and all(child.poll() is not None for child in children)
    assert directories and all(not p.exists() for p in directories)
    assert sql(b,'SELECT state FROM ingestion_jobs WHERE id=:i',i=uuid.UUID(ids['job_id']))=='retryable_failed'
