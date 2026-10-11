"""Hard page cap including candidates that appear between the hint and citation lock."""
import pytest
import threading
from sqlalchemy import text

from app import citations, discovery
from app.config import CitationPolicy, DiscoveryPolicy
from tests.resource_fixtures import installation, resource_world, resource_api, source, common_copy, runtime_revoke  # noqa: F401
from tests.test_c2d5f1_citation_runtime import cit, refs  # noqa: F401
from tests.test_discovery import PAGE_ONE
from tests.test_uploads import upload_api  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work  # noqa: F401


@pytest.mark.parametrize('n,cap,allowed', [(1, 1, True), (2, 1, False), (2, 2, True)])
def test_cap_exact_boundary_is_atomic(cit, n, cap, allowed):
    h = cit
    ids = source(h.w, h.aid, 'cap private')
    reference_ids = refs(h, ids, ['arXiv:2609.00001'] * n)
    assert citations.reconcile_pending(h.w.runtime, CitationPolicy())['links_written'] == n
    result = discovery.run_sync(h.w.runtime, DiscoveryPolicy(topics=['cs.AI'], max_pages_per_topic=1,
                                 retarget_limit=cap), fetcher=lambda *_: PAGE_ONE)
    with h.w.engine.connect() as conn:
        states = list(conn.scalars(text('SELECT match_status FROM reference_citation_links')))
        count = conn.scalar(text('SELECT count(*) FROM discovery_papers'))
        row = conn.execute(text('SELECT pages_seen,resumption_token,last_error FROM discovery_sources')).one()
    if allowed:
        assert result['pages'] == 1 and states == ['linked'] * n and count == 1
        assert tuple(row) == (1, 'page-two', None)
    else:
        assert 'error:retarget_limit_exceeded' in result
        assert result['error:retarget_limit_exceeded'] == 1 and states == ['no_discovery_record'] * n and count == 0
        assert tuple(row) == (0, None, 'retarget_limit_exceeded')


def test_late_fanout_over_cap_is_rechecked_under_exclusive(cit, monkeypatch):
    h = cit
    ids = source(h.w, h.aid, 'late cap private')
    refs(h, ids, ['arXiv:2609.00001'])
    citations.reconcile_pending(h.w.runtime, CitationPolicy())
    real = citations._lock
    inserted = []
    def lock(db, *, exclusive):
        if exclusive and not inserted:
            inserted.extend(refs(h, ids, ['arXiv:2609.00001'], provenance='late'))
            citations.reconcile_pending(h.w.runtime, CitationPolicy())
        return real(db, exclusive=exclusive)
    monkeypatch.setattr(citations, '_lock', lock)
    result = discovery.run_sync(h.w.runtime, DiscoveryPolicy(topics=['cs.AI'], retarget_limit=1),
                                fetcher=lambda *_: PAGE_ONE)
    assert result['error:retarget_limit_exceeded'] == 1
    with h.w.engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM discovery_papers')) == 0
        assert conn.scalar(text("SELECT count(*) FROM reference_citation_links WHERE match_status='no_discovery_record'")) == 2
        assert conn.scalar(text('SELECT pages_seen FROM discovery_sources')) == 0


def test_revoke_can_commit_while_http_waits(cit):
    h = cit
    copy = common_copy(h.w, indexed=False)
    entered, release = threading.Event(), threading.Event()
    results = []
    def fetch(*_):
        assert h.w.runtime.pool.checkedout() == 0
        entered.set()
        assert release.wait(20)
        return PAGE_ONE
    thread = threading.Thread(target=lambda:results.append(discovery.run_sync(h.w.runtime,
        DiscoveryPolicy(topics=['cs.AI'],max_pages_per_topic=1),fetcher=fetch)))
    thread.start()
    try:
        assert entered.wait(10)
        runtime_revoke(h.w, copy.item)
        with h.w.engine.connect() as conn:
            assert conn.scalar(text('SELECT publication_state FROM library_items WHERE id=:i'),{'i':copy.item}) == 'revoked'
    finally:
        release.set(); thread.join(25)
    assert not thread.is_alive() and results[0]['pages'] == 1


def test_real_extraction_commits_during_http(work):
    from app.db import get_engine
    from app.ingest import worker
    b = work
    entered, release = threading.Event(), threading.Event()
    results = []
    def fetch(*_):
        assert get_engine().pool.checkedout() == 0
        entered.set()
        assert release.wait(30)
        return PAGE_ONE
    thread = threading.Thread(target=lambda:results.append(discovery.run_sync(get_engine(),
        DiscoveryPolicy(topics=['cs.AI'],max_pages_per_topic=1),fetcher=fetch)))
    thread.start()
    try:
        assert entered.wait(10)
        assert worker.process_job(b.job, storage=b.storage) == 'awaiting_index'
    finally:
        release.set(); thread.join(35)
    assert not thread.is_alive() and results[0]['pages'] == 1
