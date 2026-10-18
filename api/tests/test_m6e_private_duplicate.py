"""M6e exact-byte private duplicates and retry; real runtime PG/MinIO/journal/C4 readiness.

Embedding uses the labelled fake HTTP transport of ``indexed`` (test-only, no
real provider, pricing only on that settings copy). SQL mutations through
``sql`` are labelled fixtures/adversaries, never a worker or API fallback.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import private_cleanup
from app.api.routes import library
from app.config import get_settings
from app.db import get_engine
from app.ingest import duplicates, worker
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_uploads import intent, send, upload_api  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work, sql, BODY  # noqa: F401
from tests.test_c2d5d2b_document_runtime import indexed  # noqa: F401
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import full_pdf, upload_pdf
from tests.test_auth_safety_foundation import NOW

TABLES = ('library_items', 'paper_versions', 'source_files', 'ingestion_jobs', 'upload_intents',
          'sections', 'pdf_pages', 'chunks', 'metadata_candidates', 'reference_entries',
          'reference_citation_links', 'index_generations', 'index_entries', 'embedding_calls')


def counts(b):
    return {t: sql(b, f'SELECT count(*) FROM {t}') for t in TABLES}


def process(b, job):
    return worker.process_job(uuid.UUID(str(job)), b.settings, b.storage)


def ids(raw):
    return {k: uuid.UUID(raw[k]) for k in ('item_id', 'version_id', 'file_id', 'job_id')}


def detail(browser, item):
    response = browser.get(f'/library/items/{item}')
    assert response.status_code == 200, response.text
    return response.json()['item']


def copy_of(browser, item):
    jobs = detail(browser, item)['jobs']
    assert len(jobs) == 1
    return jobs[0]['existing_copy']


def remove(b, item, browser=None):
    response = (browser or b.browser).post(f'/library/items/{item}/delete', {'confirm_delete': True})
    assert response.status_code == 200, response.text


def purge(b):
    return private_cleanup.cleanup_private(b.api.engine, b.storage, get_settings(), clock=lambda: b.api.clock.now)


def no_derived(b, item):
    for table in ('sections', 'pdf_pages', 'chunks', 'metadata_candidates', 'reference_entries',
                  'index_generations'):
        assert sql(b, f'SELECT count(*) FROM {table} WHERE item_id=:i', i=item) == 0, table
    assert sql(b, 'SELECT count(*) FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id'
                  ' WHERE f.item_id=:i', i=item) == 0
    assert sql(b, 'SELECT count(*) FROM embedding_calls c JOIN ingestion_jobs j ON j.id=c.reservation_job_id'
                  ' WHERE j.item_id=:i', i=item) == 0


def other_user(b, email='other@example.test'):
    b.api.create_user(email)
    return b.api.user_browser(email)


@pytest.fixture
def ready(indexed):
    b = indexed
    assert process(b, b.job) == 'ready'
    b.ready_calls = len(b.calls)
    return b


@pytest.mark.parametrize('kind', ['markdown', 'pdf'])
def test_new_intent_same_bytes_after_ready_is_duplicate_without_reprocessing(indexed, kind):
    b = indexed
    if kind == 'pdf':
        original = full_pdf()
        first = ids(upload_pdf(b.browser, original))
        assert process(b, first['job_id']) == 'awaiting_index'
    else:
        original, first = BODY, dict(item_id=b.item, job_id=b.job)
    assert process(b, first['job_id']) == 'ready'
    assert detail(b.browser, first['item_id'])['content_status'] == 'indexed'
    calls, before = len(b.calls), counts(b)
    again = ids((upload_pdf if kind == 'pdf' else upload)(b.browser, original))
    assert process(b, again['job_id']) == 'duplicate'
    after = counts(b)
    # A new intent is a separate operation: exactly one new item/version/file/job/intent.
    for table in TABLES:
        grown = 1 if table in ('library_items', 'paper_versions', 'source_files', 'ingestion_jobs', 'upload_intents') else 0
        assert after[table] == before[table] + grown, table
    assert len(b.calls) == calls
    no_derived(b, again['item_id'])
    new = detail(b.browser, again['item_id'])
    assert new['content_status'] == 'metadata_only' and not new['fulltext_search_available']
    assert [f['id'] for f in new['files']] == [str(again['file_id'])]
    job = new['jobs'][0]
    assert job['state'] == 'duplicate' and job['warnings'] == [duplicates.WARNING]
    assert job['existing_copy'] == {'item_id': str(first['item_id']), 'title': detail(b.browser, first['item_id'])['title'],
                                    'content_status': 'indexed'}
    assert b.browser.get(f'/files/{again["file_id"]}/original').status_code == 200
    assert detail(b.browser, first['item_id'])['content_status'] == 'indexed'
    assert sql(b, "SELECT count(*) FROM collection_items WHERE item_id=:i", i=again['item_id']) == 0
    assert sql(b, "SELECT visibility FROM library_items WHERE id=:i", i=again['item_id']) == 'private'


def test_controls_other_owner_common_and_different_bytes_stay_independent(ready):
    b = ready
    other = other_user(b)
    theirs = ids(upload(other, BODY))
    assert process(b, theirs['job_id']) == 'awaiting_index'
    assert copy_of(other, theirs['item_id']) is None
    # Labelled fixture: a common listed copy with the same checksum and a common job.
    checksum = sql(b, 'SELECT checksum_sha256 FROM source_files WHERE id=:f', f=b.file)
    with b.owner.begin() as conn:
        item = conn.scalar(text("INSERT INTO library_items (visibility, publication_state, title)"
                                " VALUES ('common','listed','COMMON-M6E-SECRET') RETURNING id"))
        version = conn.scalar(text('INSERT INTO paper_versions (item_id) VALUES (:i) RETURNING id'), {'i': item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs (origin, scope_visibility, item_id, version_id,"
                               " idempotency_key, rights_epoch_at_start, state) VALUES ('discovery','common',:i,:v,:k,0,'stored')"
                               " RETURNING id"), {'i': item, 'v': version, 'k': str(uuid.uuid4())})
        conn.execute(text("INSERT INTO source_files (item_id, version_id, job_id, storage_key, verified_mime, byte_size,"
                          " checksum_sha256) VALUES (:i,:v,:j,:k,'text/markdown',:n,:h)"),
                     {'i': item, 'v': version, 'j': job, 'k': 'originals/' + uuid.uuid4().hex, 'n': len(BODY), 'h': checksum})
    owner_b = sql(b, 'SELECT owner_user_id FROM library_items WHERE id=:i', i=theirs['item_id'])
    owner_a = sql(b, 'SELECT owner_user_id FROM library_items WHERE id=:i', i=b.item)
    with Session(b.api.engine) as db:  # runtime role: grants for every predicate table
        found = [r.item_id for r in db.execute(duplicates.targets(owner_a, checksum, exclude_file_id=uuid.uuid4()))]
        assert found == [b.item]
        found = [r.item_id for r in db.execute(duplicates.targets(owner_b, checksum, exclude_file_id=theirs['file_id']))]
    assert found == []
    changed = ids(upload(b.browser, BODY + b'\n'))
    assert process(b, changed['job_id']) == 'awaiting_index'
    assert copy_of(b.browser, changed['item_id']) is None
    # B's own new upload sees B's live copy, never A's or the common copy.
    again = ids(upload(other, BODY))
    assert process(b, again['job_id']) == 'duplicate'
    copy = copy_of(other, again['item_id'])
    assert copy['item_id'] == str(theirs['item_id'])
    text_out = str(detail(other, again['item_id']))
    assert str(b.item) not in text_out and str(item) not in text_out and 'COMMON-M6E-SECRET' not in text_out
    b.api.bootstrap_admin()
    admin = b.api.admin_browser()
    assert admin.get(f'/library/items/{again["item_id"]}').status_code == 404


def prepare_target(b, state):
    """Labelled fixture: move the real awaiting_index target A into one table row."""
    resume, finished, cancel, epoch = None, False, False, False
    if isinstance(state, tuple):
        state, resume = state
    if state == 'cancel_requested':
        state, cancel = 'awaiting_index', True
    if state == 'epoch_drift':
        state, epoch = 'awaiting_index', True
    if state in ('duplicate', 'cancelled', 'terminal_failed', 'fulltext_rejected', 'needs_review'):
        finished = True
    sql(b, "UPDATE ingestion_jobs SET state=:s, resume_state=:r, lease_token=NULL, lease_until=NULL,"
           " finished_at=CASE WHEN :f THEN now() END, cancel_requested=:c, cancel_requested_at=CASE WHEN :c THEN now() END"
           " WHERE id=:j", s=state, r=resume, f=finished, c=cancel, j=b.job)
    if epoch:
        sql(b, 'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i', i=b.item)


ELIGIBLE = ['stored', 'extracting', 'chunking', 'awaiting_index', 'indexing', ('retryable_failed', 'stored'),
            ('retryable_failed', 'extracting'), ('retryable_failed', 'chunking'), ('retryable_failed', 'indexing')]
INELIGIBLE = ['received', 'validating', ('retryable_failed', 'validating'), 'duplicate', 'cancelled',
              'terminal_failed', 'fulltext_rejected', 'needs_review', 'cancel_requested', 'epoch_drift']


@pytest.mark.parametrize('state', ELIGIBLE + INELIGIBLE, ids=str)
def test_eligibility_table_by_target_state_binding_and_epoch(work, state):
    b = work
    assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    prepare_target(b, state)
    new = ids(upload(b.browser, BODY))
    expected = 'duplicate' if state in ELIGIBLE else 'awaiting_index'
    assert worker.process_job(new['job_id'], storage=b.storage) == expected
    copy = copy_of(b.browser, new['item_id'])
    if expected == 'duplicate':
        assert copy == {'item_id': str(b.item), 'title': detail(b.browser, b.item)['title'], 'content_status': 'metadata_only'}
    else:
        assert copy is None
        assert sql(b, 'SELECT count(*) FROM chunks WHERE item_id=:i', i=new['item_id']) > 0


@pytest.mark.parametrize('broken', ['generation_superseded', 'generation_other_epoch', 'item_metadata_only'])
def test_ready_target_requires_its_active_generation(ready, broken):
    b = ready
    if broken == 'generation_superseded':
        sql(b, "UPDATE index_generations SET state='superseded' WHERE item_id=:i", i=b.item)
    elif broken == 'generation_other_epoch':
        sql(b, 'UPDATE index_generations SET rights_epoch=rights_epoch+1 WHERE item_id=:i', i=b.item)
    else:
        sql(b, "UPDATE library_items SET content_status='metadata_only', active_index_generation_id=NULL WHERE id=:i", i=b.item)
    new = ids(upload(b.browser, BODY))
    assert process(b, new['job_id']) == 'awaiting_index'


def test_multiple_candidates_stable_earliest_and_duplicate_of_duplicate(ready, monkeypatch):
    b = ready
    # Labelled legacy fixture: pre-M6e validation skipped a ready target, so B became stored too.
    real = duplicates.targets
    monkeypatch.setattr(duplicates, 'targets', lambda *a, **kw: real(*a, **kw).where(text('false')))
    legacy = ids(upload(b.browser, BODY))
    assert process(b, legacy['job_id']) == 'awaiting_index'
    monkeypatch.setattr(duplicates, 'targets', real)
    first_dup = ids(upload(b.browser, BODY))
    assert process(b, first_dup['job_id']) == 'duplicate'
    assert copy_of(b.browser, first_dup['item_id'])['item_id'] == str(b.item)
    second_dup = ids(upload(b.browser, BODY))
    assert process(b, second_dup['job_id']) == 'duplicate'
    assert copy_of(b.browser, second_dup['item_id'])['item_id'] == str(b.item)
    remove(b, b.item)
    # Read-time navigation: the next live same-owner copy, never the deleted one or a duplicate.
    for dup in (first_dup, second_dup):
        assert copy_of(b.browser, dup['item_id']) == {'item_id': str(legacy['item_id']),
            'title': detail(b.browser, legacy['item_id'])['title'], 'content_status': 'metadata_only'}
    remove(b, legacy['item_id'])
    for dup in (first_dup, second_dup):
        assert copy_of(b.browser, dup['item_id']) is None
    fresh = ids(upload(b.browser, BODY))
    assert process(b, fresh['job_id']) == 'awaiting_index'


def test_all_same_bytes_deleted_and_purged_new_upload_processes_independently(ready):
    b = ready
    dup = ids(upload(b.browser, BODY))
    assert process(b, dup['job_id']) == 'duplicate'
    remove(b, b.item)
    assert copy_of(b.browser, dup['item_id']) is None
    response = b.browser.get(f'/library/items/{b.item}')
    assert response.status_code == 404
    remove(b, dup['item_id'])
    for _ in range(4):
        if sql(b, 'SELECT count(*) FROM private_item_deletions WHERE purged_at IS NULL') == 0:
            break
        purge(b)
    calls = len(b.calls)
    fresh = ids(upload(b.browser, BODY))
    assert process(b, fresh['job_id']) == 'awaiting_index'
    assert process(b, fresh['job_id']) == 'ready'
    assert len(b.calls) > calls
    assert sql(b, 'SELECT count(*) FROM private_item_deletions WHERE purged_at IS NOT NULL') == 2
    assert copy_of(b.browser, fresh['item_id']) is None


def test_deleted_but_unpurged_target_is_never_target(ready):
    b = ready
    remove(b, b.item)
    assert sql(b, 'SELECT purged_at IS NULL FROM private_item_deletions WHERE item_id=:i', i=b.item)
    fresh = ids(upload(b.browser, BODY))
    assert process(b, fresh['job_id']) == 'awaiting_index'
    assert copy_of(b.browser, fresh['item_id']) is None


@pytest.mark.parametrize('decision', ['private_item_deletion', 'deletion_tombstone'])
def test_decision_or_tombstone_alone_excludes_otherwise_live_target(ready, decision):
    b = ready
    owner = sql(b, 'SELECT owner_user_id FROM library_items WHERE id=:i', i=b.item)
    checksum = sql(b, 'SELECT checksum_sha256 FROM source_files WHERE id=:f', f=b.file)
    query = duplicates.targets(owner, checksum, exclude_file_id=uuid.uuid4())
    insert = {
        'private_item_deletion': "INSERT INTO private_item_deletions (item_id, owner_user_id, rights_epoch, deleted_at,"
                                 " purge_due_at, manifest) VALUES (:i, :o, 1, now(), now(), '{}'::jsonb)",
        'deletion_tombstone': "INSERT INTO deletion_tombstones (target_kind, item_id, reason_code, prior_publication_state,"
                              " rights_epoch, recorded_at, recorded_by_user_id, purge_due_at)"
                              " VALUES ('common_copy', :i, 'rights_withdrawn', 'listed', 1, now(), :o, now() + interval '1 day')",
    }[decision]
    # Labelled adversary, always rolled back: a decision row while the item still looks live
    # (e.g. restored snapshot before recover). Triggers are bypassed only inside this transaction.
    with b.owner.connect() as conn:
        with conn.begin() as tx:
            before = [r.item_id for r in conn.execute(query)]
            conn.execute(text('SET LOCAL session_replication_role = replica'))
            conn.execute(text(insert), {'i': b.item, 'o': owner})
            after = [r.item_id for r in conn.execute(query)]
            tx.rollback()
    assert before == [b.item] and after == []
    assert sql(b, 'SELECT count(*) FROM private_item_deletions') == 0
    assert sql(b, 'SELECT count(*) FROM deletion_tombstones') == 0


def barrier_validate(monkeypatch, parties):
    gate = threading.Barrier(parties, timeout=30)
    real = worker._finish

    def finish(db, job_id, token, step, save):
        if step == 'validate':
            gate.wait()
        return real(db, job_id, token, step, save)
    monkeypatch.setattr(worker, '_finish', finish)


def blocked_pids(b):
    return sql(b, "SELECT count(*) FROM pg_locks WHERE NOT granted")


def waiting_kinds(b):
    return sql(b, "SELECT coalesce(string_agg(DISTINCT locktype, ','), '') FROM pg_locks WHERE NOT granted")


def plain_storage(b, monkeypatch):
    # Concurrent workers: the fixture's single-thread "no connection during IO" observer
    # would see the other worker's connection, so use the real storage object directly.
    monkeypatch.setattr(b.storage, 'open', type(b.storage).open.__get__(b.storage))


@pytest.mark.parametrize('existing_target', [False, True])
def test_two_validations_same_owner_hash_consistent_without_deadlock(work, monkeypatch, existing_target):
    b = work
    plain_storage(b, monkeypatch)
    if existing_target:
        assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    one, two = ids(upload(b.browser, BODY)), ids(upload(b.browser, BODY))
    barrier_validate(monkeypatch, 2)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda x: worker.process_job(x['job_id'], storage=b.storage), (one, two)))
    if existing_target:
        assert results == ['duplicate', 'duplicate']
        assert {copy_of(b.browser, x['item_id'])['item_id'] for x in (one, two)} == {str(b.item)}
    else:
        assert sorted(results) == ['awaiting_index', 'duplicate']
        winner, loser = (one, two) if results[0] == 'awaiting_index' else (two, one)
        assert copy_of(b.browser, loser['item_id'])['item_id'] == str(winner['item_id'])
        assert copy_of(b.browser, winner['item_id']) is None
    assert sql(b, 'SELECT count(*) FROM ingest_step_attempts WHERE error_code IS NOT NULL') == 0


def test_target_deleted_before_validation_runs_independently(work):
    b = work
    assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    new = ids(upload(b.browser, BODY))
    remove(b, b.item)
    assert worker.process_job(new['job_id'], storage=b.storage) == 'awaiting_index'


def test_target_deleted_between_storage_read_and_checkpoint(work, monkeypatch):
    b = work
    assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    new = ids(upload(b.browser, BODY))
    real, fired = b.storage.open, []

    def opened(key):
        result = real(key)
        if not fired:
            fired.append(True)
            remove(b, b.item)
        return result
    monkeypatch.setattr(b.storage, 'open', opened)
    assert worker.process_job(new['job_id'], storage=b.storage) == 'awaiting_index'
    assert fired


def test_target_delete_waits_for_validation_checkpoint_then_link_disappears(work, monkeypatch):
    b = work
    assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    new = ids(upload(b.browser, BODY))
    real, inside, release, observed = duplicates.targets, threading.Event(), threading.Event(), {}

    def targets(*a, **kw):
        inside.set()
        assert release.wait(30)
        return real(*a, **kw)
    monkeypatch.setattr(duplicates, 'targets', targets)
    with ThreadPoolExecutor(2) as pool:
        validation = pool.submit(worker.process_job, new['job_id'], storage=b.storage)
        assert inside.wait(30)
        deletion = pool.submit(remove, b, b.item)
        deadline = time.monotonic() + 30
        while not blocked_pids(b) and time.monotonic() < deadline:
            time.sleep(0.05)
        observed['blocked'] = blocked_pids(b) > 0
        observed['deleted_early'] = deletion.done()
        release.set()
        assert validation.result(60) == 'duplicate'
        deletion.result(60)
    assert observed == {'blocked': True, 'deleted_early': False}
    assert copy_of(b.browser, new['item_id']) is None
    assert sql(b, 'SELECT count(*) FROM chunks WHERE item_id=:i', i=new['item_id']) == 0
    assert worker.process_job(new['job_id'], storage=b.storage) == 'not_claimed'


def test_lease_takeover_and_redelivery_never_repeat_validation_outcome(work, monkeypatch):
    b = work
    plain_storage(b, monkeypatch)
    assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    new = ids(upload(b.browser, BODY))
    real, inside, release = worker._read_markdown, threading.Event(), threading.Event()
    stalled = []

    def read(source, storage, settings):
        if not stalled:
            stalled.append(True)
            inside.set()
            assert release.wait(30)
        return real(source, storage, settings)
    monkeypatch.setattr(worker, '_read_markdown', read)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(worker.process_job, new['job_id'], storage=b.storage)
        assert inside.wait(30)
        # Labelled adversary: the first lease expires while its holder is stalled in storage IO.
        sql(b, 'UPDATE ingestion_jobs SET lease_until=:t WHERE id=:j', t=NOW - timedelta(days=1), j=new['job_id'])
        assert worker.process_job(new['job_id'], storage=b.storage) == 'duplicate'
        release.set()
        assert first.result(60) == 'lease_lost'
    before = counts_for(b, new['item_id'])
    for _ in range(2):
        assert worker.process_job(new['job_id'], storage=b.storage) == 'not_claimed'
        assert worker.process_job(b.job, storage=b.storage) in ('not_claimed', 'embedding_key_missing')
    assert counts_for(b, new['item_id']) == before
    assert sql(b, "SELECT count(*) FROM ingest_step_attempts WHERE job_id=:j AND step='validate' AND outcome='success'",
               j=new['job_id']) == 1


def counts_for(b, item):
    return {t: sql(b, f'SELECT count(*) FROM {t} WHERE item_id=:i', i=item)
            for t in ('paper_versions', 'source_files', 'ingestion_jobs', 'sections', 'chunks', 'reference_entries')}


def test_repeat_put_same_intent_and_ready_redelivery_change_nothing(ready):
    b = ready
    created = intent(b.browser, BODY)
    assert created.status_code == 201, created.text
    token = created.json()['intent_token']
    first = send(b.browser, token, BODY)
    assert first.status_code == 201, first.text
    before = counts(b)
    for _ in range(2):
        repeat = send(b.browser, token, BODY)
        assert repeat.status_code in (404, 409), repeat.text
    assert counts(b) == before
    calls = len(b.calls)
    for _ in range(2):
        assert process(b, b.job) == 'not_claimed'
    assert process(b, first.json()['job_id']) == 'duplicate'
    assert process(b, first.json()['job_id']) == 'not_claimed'
    after = counts(b)
    assert {t: after[t] - before[t] for t in TABLES} == dict.fromkeys(TABLES, 0)
    assert len(b.calls) == calls
    assert sql(b, 'SELECT count(*) FROM embedding_calls WHERE reservation_job_id=:j',
               j=uuid.UUID(first.json()['job_id'])) == 0


def test_read_link_concurrent_with_target_delete(ready, monkeypatch):
    b = ready
    dup = ids(upload(b.browser, BODY))
    assert process(b, dup['job_id']) == 'duplicate'
    real, calls = library._read_get_item, []

    def read(**kw):
        result = real(**kw)
        calls.append(result['item']['jobs'][0]['existing_copy'])
        if len(calls) == 1:
            # Before the boundary's item locks: the delete commits; the fresh observation drops the link.
            with ThreadPoolExecutor(1) as pool:
                pool.submit(remove, b, b.item).result(60)
        return result
    monkeypatch.setattr(library, '_read_get_item', read)
    assert copy_of(b.browser, dup['item_id']) is None
    assert calls[0]['item_id'] == str(b.item) and calls[-1] is None
    assert get_engine().pool.checkedout() == 0


def test_read_link_holds_target_share_lock_delete_waits(ready, monkeypatch):
    b = ready
    dup = ids(upload(b.browser, BODY))
    assert process(b, dup['job_id']) == 'duplicate'
    real, calls, box = library._read_get_item, [], {}
    pool = ThreadPoolExecutor(1)

    def read(**kw):
        calls.append(1)
        if len(calls) == 2:
            # Under the boundary's SHARE locks: a delete must wait for this read.
            box['delete'] = pool.submit(remove, b, b.item)
            deadline = time.monotonic() + 30
            while not blocked_pids(b) and time.monotonic() < deadline:
                time.sleep(0.05)
            box['blocked'] = blocked_pids(b) > 0 and not box['delete'].done()
            # The delete waits on the target row SHARE lock, before its journal step.
            box['kinds'] = waiting_kinds(b)
        return real(**kw)
    monkeypatch.setattr(library, '_read_get_item', read)
    try:
        copy = copy_of(b.browser, dup['item_id'])
        box['delete'].result(60)
    finally:
        pool.shutdown()
    assert box['blocked'] and copy['item_id'] == str(b.item)
    assert box['kinds'] and 'advisory' not in box['kinds'], box['kinds']
    assert b.browser.get(f'/library/items/{b.item}').status_code == 404
    assert copy_of(b.browser, dup['item_id']) is None
