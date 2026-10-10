"""C2d-5f1 hooks with the real worker: extraction/discovery/backfill/revoke/cleanup lock graph.

Real runtime PG, MinIO and journal; only business cutover is scoped. The
worker is ``process_job`` (C2d-5d2a) with its own item -> job locks; the hook
is paused at a named point so the test can observe real lock waits. Discovery
pages are synthetic OAI XML through ``_write_page`` (no network).
"""
import functools
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import citations
from app.config import get_settings
from app.db import get_engine
from app.discovery import _write_page, parse_page
from app.ingest import cleanup as cleanup_boundary, worker
from app.models import DiscoverySource
from tests.resource_fixtures import runtime_revoke, source
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes  # noqa: F401
from tests.test_c2d5d2a_common_extraction import common  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work  # noqa: F401
from tests.test_c2d5f1_citation_runtime import Pause, advisory, eventually, lock_waiters, settle, thread
from tests.test_citations import SEED, oai_page
from tests.test_uploads import upload_api  # noqa: F401


def settling(test):
    """Release pauses/join threads right after the test body, even when it fails, so the world
    fixtures tear down without a paused thread still holding a connection."""
    @functools.wraps(test)
    def body(*args, **kwargs):
        try:
            return test(*args, **kwargs)
        finally:
            settle()
    return body


def topic(engine):
    with Session(engine) as db:  # labelled setup: the topic discovery pages hang off
        db.add(DiscoverySource(topic='cs.AI', window_from=NOW.date()))
        db.commit()


def page(records=SEED, at=NOW):
    with Session(get_engine()) as db:  # runtime login, as the discovery worker
        _write_page(db, 'cs.AI', parse_page(oai_page(records), 10**6), at)
        db.commit()


def status(owner, item):
    with owner.connect() as c:  # labelled owner read
        return c.execute(text("""SELECT l.match_status FROM reference_citation_links l JOIN reference_entries r
            ON r.id=l.reference_id WHERE r.item_id=:i ORDER BY r.ordinal"""), {'i': item}).scalars().all()


def observer(owner):
    return SimpleNamespace(w=SimpleNamespace(engine=owner))


def advisory_waiting(h):
    return [r for r in advisory(h) if not r.granted]


def run_pending():
    return citations.reconcile_pending(get_engine(), get_settings().citations)


@pytest.mark.parametrize('first', ['extraction', 'discovery'])
@settling
def test_real_extraction_and_discovery_both_arrival_orders_converge(work, monkeypatch, first):
    b = work
    topic(b.api.engine)
    h = observer(b.owner)
    if first == 'extraction':
        pause, real = Pause(), worker.reconcile_references

        def hook(db, **kw):
            n = real(db, **kw)  # link written under the shared citation lock, not yet committed
            pause()
            return n
        monkeypatch.setattr(worker, 'reconcile_references', hook)
        t, box = thread(lambda: worker.process_job(b.job, storage=b.storage), 'worker')
        assert pause.reached.wait(30)
        d, dbox = thread(page, 'discovery')
        eventually(lambda: advisory_waiting(h), 'discovery did not wait for the shared citation lock')
        pause.go.set()
        t.join(60)
        d.join(60)
    else:
        # Discovery page transaction holding the exclusive lock: runtime login on its own pool, so the
        # fixture's "no worker connection during storage IO" guard is not tripped by the test itself.
        with Session(b.api.engine) as d:
            _write_page(d, 'cs.AI', parse_page(oai_page(SEED), 10**6), NOW)
            t, box = thread(lambda: worker.process_job(b.job, storage=b.storage), 'worker')
            eventually(lambda: advisory_waiting(h), 'extraction did not wait for the exclusive citation lock')
            d.commit()
        dbox = {}
        t.join(60)
    assert box.get('result') == 'awaiting_index', box
    assert 'error' not in dbox, dbox
    assert status(b.owner, b.item) == ['linked']  # neither arrival order misses the other
    assert run_pending() == {}  # nothing left for the backfill


@settling
def test_real_extraction_holds_item_and_shared_lock_backfill_continues(work, monkeypatch):
    b = work
    topic(b.api.engine)
    page()
    other = source(SimpleNamespace(engine=b.owner), b.uid, 'OTHER PRIVATE')
    with b.owner.begin() as c:  # labelled setup: an existing reference of another item
        c.execute(text("""INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,raw_citation,fields,
            confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version)
            VALUES(:i,:v,:f,0,'arXiv:2609.00012','{}','raw_only','synthetic','markdown',0,16,'synthetic')"""),
            {'i': other.item, 'v': other.version, 'f': other.file})
    pause, real = Pause(), worker.reconcile_references

    def hook(db, **kw):
        n = real(db, **kw)
        pause()  # item FOR UPDATE + shared citation lock held, link uncommitted
        return n
    monkeypatch.setattr(worker, 'reconcile_references', hook)
    t, box = thread(lambda: worker.process_job(b.job, storage=b.storage), 'worker')
    assert pause.reached.wait(30)
    # Shared with the extraction: the backfill neither waits nor sees the uncommitted references.
    assert run_pending() == {'batches': 1, 'references': 1, 'links_written': 1}
    pause.go.set()
    t.join(60)
    assert box.get('result') == 'awaiting_index', box
    assert status(b.owner, b.item) == ['linked'] and status(b.owner, other.item) == ['linked']
    assert run_pending() == {}


@settling
def test_real_common_extraction_holding_item_and_discovery_retarget_never_deadlock(common, monkeypatch):
    """The C2d-5f1 lock order: a discovery page waits for the common item BEFORE the exclusive lock."""
    b = common
    topic(b.w.runtime)
    h = observer(b.w.engine)
    with b.w.engine.begin() as c:  # labelled setup: an existing bibliography row of the copy
        c.execute(text("""INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,raw_citation,fields,
            confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version)
            VALUES(:i,:v,:f,0,'arXiv:2609.00011','{}','raw_only','synthetic','markdown',0,16,'synthetic')"""),
            {'i': b.copy.item, 'v': b.copy.version, 'f': b.copy.file})
    assert run_pending() == {'batches': 1, 'references': 1, 'links_written': 1}
    assert status(b.w.engine, b.copy.item) == ['no_discovery_record']
    pause, real = Pause(), worker.reconcile_references

    def hook(db, **kw):
        pause()  # the real worker holds the common item FOR UPDATE (lock_item_then_job), no citation lock yet
        return real(db, **kw)
    monkeypatch.setattr(worker, 'reconcile_references', hook)
    t, box = thread(lambda: worker.process_job(b.job, storage=b.raw_storage), 'worker')
    assert pause.reached.wait(30)
    d, dbox = thread(page, 'discovery')
    waiting = eventually(lambda: lock_waiters(h, 'FOR KEY SHARE'), 'discovery did not wait for the item first')
    assert not {pid for pid, _ in waiting} & {r.pid for r in advisory(h)}  # no exclusive lock while waiting
    pause.go.set()
    t.join(60)
    d.join(60)
    assert box.get('result') == 'awaiting_index', box
    assert 'error' not in dbox, dbox  # no deadlock_detected on either side
    assert status(b.w.engine, b.copy.item) == ['linked']


def test_batch_before_revoke_then_runtime_cleanup_removes_links_and_nothing_returns(indexes):
    b = indexes
    topic(b.w.runtime)
    page()
    with b.w.engine.begin() as c:  # labelled setup: bibliography rows of the common copy
        for n, raw in enumerate(['arXiv:2609.00011', 'arXiv:2609.00012v2']):
            c.execute(text("""INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,raw_citation,
                fields,confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version)
                VALUES(:i,:v,:f,:n,:r,'{}','raw_only','synthetic','markdown',0,16,'synthetic')"""),
                {'i': b.copy.item, 'v': b.copy.version, 'f': b.copy.file, 'n': n, 'r': raw})
    private = b.own  # same bytes, private owner: must keep its own state
    with b.w.engine.begin() as c:
        c.execute(text("""INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,raw_citation,fields,
            confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version)
            VALUES(:i,:v,:f,0,'arXiv:2609.00011','{}','raw_only','synthetic','markdown',0,16,'synthetic')"""),
            {'i': private.item, 'v': private.version, 'f': private.file})
    assert run_pending() == {'batches': 1, 'references': 3, 'links_written': 3}
    runtime_revoke(b.w, b.copy.item, clock=b.clock)
    result = cleanup_boundary.cleanup_revoked(get_engine(), b.raw_storage, get_settings())
    assert result == {'deleted': 1}, result
    with b.w.engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM reference_entries WHERE item_id=:i'), {'i': b.copy.item}) == 0
        assert c.scalar(text('SELECT ras_common_copy_derived_left(:i)'), {'i': b.copy.item}) == []
    assert status(b.w.engine, private.item) == ['linked']
    # Nothing is written back after t_rev/purge: backfill and re-target find no candidate of the copy.
    assert run_pending() == {}
    page(at=NOW + timedelta(minutes=5))
    with b.w.engine.connect() as c:
        assert c.scalar(text("""SELECT count(*) FROM reference_citation_links l JOIN reference_entries r
            ON r.id=l.reference_id WHERE r.item_id=:i"""), {'i': b.copy.item}) == 0
