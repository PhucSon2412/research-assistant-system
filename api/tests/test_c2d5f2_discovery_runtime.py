"""Discovery against real PostgreSQL runtime LOGIN, integrity and journal.

Only cutover readiness is scoped. Owner SQL below is labelled setup/adversary.
Synthetic feeds never contact arXiv/providers; HTTP transport tests use loopback.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import threading
import time
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import auth_safety, discovery as d, recovery
from app.config import DiscoveryPolicy, get_settings
from app.ingest import runtime
from tests.resource_fixtures import installation, resource_world, resource_api  # noqa: F401
from tests.test_discovery import PAGE_ONE, PAGE_TWO, PAGE_DELETED, ERROR_TOKEN

REAL_CUTOVER = auth_safety.cutover


@pytest.fixture
def disc(installation, monkeypatch):
    w = installation
    monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
    yield w
    assert w.runtime.pool.checkedout() == 0


def policy(**kw):
    return DiscoveryPolicy(**({'topics': ['cs.AI'], 'max_pages_per_topic': 1} | kw))


def q(w, sql, **params):
    """Labelled owner setup/read/adversary, never the discovery entrypoint."""
    with w.engine.begin() as c:
        result = c.execute(text(sql), params)
        return result.all() if result.returns_rows else None


def due(w):
    q(w, "UPDATE discovery_sources SET next_attempt_at=NULL")


def run(w, feed=PAGE_ONE, **kw):
    return d.run_sync(w.runtime, policy(**kw), fetcher=lambda *_: feed)


def test_two_pages_resume_atomically_and_inclusive_empty_window(disc):
    w = disc
    a = run(w)
    assert a['pages'] == a['records'] == a['requests'] == 1 and a['bound:pages'] == 1
    row = q(w, 'SELECT resumption_token,pages_seen,records_seen,last_success_at FROM discovery_sources')[0]
    assert tuple(row) == ('page-two', 1, 1, None)
    seen = []
    b = d.run_sync(w.runtime, policy(), fetcher=lambda p, _: seen.append(p) or PAGE_TWO)
    assert seen == [{'verb': 'ListRecords', 'resumptionToken': 'page-two'}]
    assert b['pages'] == 1
    row = q(w, 'SELECT window_from,window_until,resumption_token,pages_seen,last_success_at FROM discovery_sources')[0]
    assert row.window_until is row.resumption_token is None and row.pages_seen == 2 and row.last_success_at
    assert row.window_from == row.last_success_at.date()
    assert run(w)['requests'] == 0  # successful poll scheduling
    empty = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch">none</error></OAI-PMH>'
    result = d.run_sync(w.runtime, policy(), force=True, fetcher=lambda p, _: empty)
    assert result['pages'] == 1 and result['records'] == 0
    assert q(w, 'SELECT count(*) FROM discovery_papers')[0][0] == 2


@pytest.mark.parametrize('engine_kind', ['owner', 'recovery'])
def test_privileged_login_refused_before_dml_or_http(disc, engine_kind):
    w = disc
    if engine_kind == 'owner':
        engine = w.engine
    else:
        import os
        from sqlalchemy import create_engine
        engine = create_engine(w.runtime.url.set(username='c1_recovery', password=os.environ['RAS_C1_RECOVERY_PASSWORD']), hide_parameters=True)
    try:
        result = d.run_sync(engine, policy(), fetcher=lambda *_: pytest.fail('HTTP reached'))
        assert result['halted:worker_runtime_required'] == 1
        assert q(w, 'SELECT count(*) FROM discovery_sources')[0][0] == 0
    finally:
        if engine_kind == 'recovery':
            engine.dispose()


@pytest.mark.parametrize('table,priv', d.NEEDED)
def test_each_grant_is_required(disc, table, priv):
    w = disc
    q(w, f'REVOKE {priv} ON {table} FROM c1_runtime')
    try:
        result = d.run_sync(w.runtime, policy(), fetcher=lambda *_: pytest.fail('HTTP reached'))
        assert any(k.startswith('halted:') for k in result)
        assert q(w, 'SELECT count(*) FROM discovery_sources')[0][0] == 0
    finally:
        q(w, f'GRANT {priv} ON {table} TO c1_runtime')


@pytest.mark.parametrize('gate', ['pending', 'hold', 'missing', 'torn'])
@pytest.mark.parametrize('midpass', [False, True])
def test_real_gate_at_entry_and_after_http(disc, monkeypatch, gate, midpass):
    w = disc
    def close():
        if gate == 'pending':
            monkeypatch.setattr(auth_safety, 'cutover', REAL_CUTOVER)
        elif gate == 'hold':
            recovery.hold(w.engine, get_settings(), datetime.now(UTC))
        elif gate == 'missing':
            w.path.unlink()
        else:
            with w.path.open('ab') as file:
                file.write(b'{"torn":')
    calls = []
    def fetch(*_):
        calls.append(1)
        close()
        return PAGE_ONE
    if not midpass:
        close()
    result = d.run_sync(w.runtime, policy(), fetcher=fetch)
    assert result['halted:recovery_gate_closed'] == 1
    assert len(calls) == int(midpass) and result['pages'] == 0
    assert q(w, 'SELECT count(*) FROM discovery_papers')[0][0] == 0


def test_disabled_has_no_connection_or_fetch():
    assert d.run_sync(None, policy(enabled=False), fetcher=lambda *_: pytest.fail())['skipped:disabled'] == 1


@pytest.mark.parametrize('other_topic', ['cs.AI', 'cs.LG'])
def test_global_lease_busy_and_http_owns_no_connection(disc, other_topic):
    w = disc
    entered, release = threading.Event(), threading.Event()
    result = []
    def fetch(*_):
        assert w.runtime.pool.checkedout() == 0
        entered.set()
        assert release.wait(10)
        return PAGE_ONE
    thread = threading.Thread(target=lambda: result.append(d.run_sync(w.runtime, policy(), fetcher=fetch)))
    thread.start()
    try:
        assert entered.wait(10)
        # Independent runtime transaction can commit while discovery is blocked in HTTP.
        with w.runtime.begin() as conn:
            conn.execute(text("INSERT INTO discovery_sources(topic,window_from) VALUES('cs.CL',current_date)"))
        busy = d.run_sync(w.runtime, policy(topics=[other_topic]), fetcher=lambda *_: pytest.fail())
        assert busy['busy'] and busy['busy:discovery_lease_busy'] == 1
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and result[0]['pages'] == 1


def test_old_holder_cannot_write_renew_or_release_new_holder(disc):
    w = disc
    nonce, newer, facts = uuid.uuid4(), uuid.uuid4(), {}
    d._claim(w.runtime, policy(), nonce, facts)
    cursor = d._prepare(w.runtime, policy(), nonce, 'cs.AI', False, facts)
    q(w, "UPDATE discovery_request_clock SET lease_until=clock_timestamp()-interval '1 second'")
    d._claim(w.runtime, policy(), newer, {})
    for action in (
        lambda: d._page_checkpoint(w.runtime, policy(), nonce, cursor, d.parse_page(PAGE_ONE, 100000), {}),
        lambda: d._reserve_request(w.runtime, policy(), nonce, cursor, {}),
        lambda: d._release_lease(w.runtime, policy(), nonce, {})):
        with pytest.raises(runtime.Halt, match='discovery_lease_lost'):
            action()
    assert q(w, 'SELECT lease_token FROM discovery_request_clock')[0][0] == newer
    assert q(w, 'SELECT pages_seen FROM discovery_sources')[0][0] == 0


def test_cursor_binding_rejects_changed_topic_window_token(disc):
    w = disc
    nonce, facts = uuid.uuid4(), {}
    d._claim(w.runtime, policy(), nonce, facts)
    cursor = d._prepare(w.runtime, policy(), nonce, 'cs.AI', False, facts)
    for change in ("resumption_token='new'", "window_from=window_from-1", "window_until=window_until+1"):
        with w.engine.begin() as conn:
            conn.execute(text('UPDATE discovery_sources SET ' + change))
        with pytest.raises(runtime.Halt, match='discovery_cursor_changed'):
            d._page_checkpoint(w.runtime, policy(), nonce, cursor, d.parse_page(PAGE_ONE, 100000), {})
        q(w, 'UPDATE discovery_sources SET resumption_token=:t,window_from=:f,window_until=:u',
          t=cursor.token, f=cursor.start, u=cursor.until)


def test_pacing_wait_is_outside_db_and_rechecks_clock(disc, monkeypatch):
    w = disc
    original = d.time.sleep
    sleeps, starts = [], []
    def sleep(seconds):
        sleeps.append(seconds)
        assert w.runtime.pool.checkedout() == 0
        original(seconds)
    monkeypatch.setattr(d.time, 'sleep', sleep)
    def feed(params, _):
        starts.append(time.monotonic())
        return PAGE_ONE if len(starts) == 1 else PAGE_TWO
    result = d.run_sync(w.runtime, policy(max_pages_per_topic=2), fetcher=feed)
    assert result['pages'] == 2 and sleeps and starts[1] - starts[0] >= 3


def test_pass_time_bound_and_clock_argument_is_not_authority(disc):
    w = disc
    q(w, "UPDATE discovery_request_clock SET next_request_at=clock_timestamp()+interval '1 hour'")
    result = d.run_sync(w.runtime, policy(max_pass_seconds=1), now=datetime(2099, 1, 1, tzinfo=UTC),
                        force=True, fetcher=lambda *_: pytest.fail())
    assert result['bound:time'] == 1 and result['requests'] == 0
    assert q(w, 'SELECT lease_token FROM discovery_request_clock')[0][0] is None
    assert q(w, 'SELECT window_until FROM discovery_sources')[0][0] == datetime.now(UTC).date()


@pytest.mark.parametrize('error', ['http_406', 'http_429', 'http_503', 'invalid_xml', 'http_deadline', 'sensitive token?secret'])
def test_error_is_sanitized_backoff_force_cannot_bypass_and_resume(disc, error):
    w = disc
    assert run(w)['pages'] == 1
    before = q(w, 'SELECT resumption_token,pages_seen,records_seen FROM discovery_sources')[0]
    def fail(*_):
        raise d.DiscoveryError(error, 120)
    result = d.run_sync(w.runtime, policy(), fetcher=fail)
    code = error if error in d.ERROR_CODES else 'fetch_error'
    assert result['error:' + code] == 1
    assert q(w, 'SELECT resumption_token,pages_seen,records_seen FROM discovery_sources')[0] == before
    row = q(w, 'SELECT last_error,next_attempt_at,clock_timestamp() AS now FROM discovery_sources')[0]
    assert row.last_error == code and (row.next_attempt_at - row.now).total_seconds() > 110
    assert d.run_sync(w.runtime, policy(), force=True, fetcher=lambda *_: pytest.fail())['requests'] == 0
    due(w)
    q(w, 'UPDATE discovery_request_clock SET next_request_at=NULL')  # labelled setup: advance global Retry-After too
    assert run(w, PAGE_TWO)['pages'] == 1
    assert q(w, 'SELECT count(*) FROM discovery_papers')[0][0] == 2


def test_expired_token_keeps_window_and_replays_idempotently(disc):
    w = disc
    run(w)
    before = q(w, 'SELECT window_from,window_until FROM discovery_sources')[0]
    assert run(w, ERROR_TOKEN)['error:badResumptionToken'] == 1
    assert q(w, 'SELECT window_from,window_until FROM discovery_sources')[0] == before
    assert q(w, 'SELECT resumption_token FROM discovery_sources')[0][0] is None
    due(w)
    run(w)
    assert q(w, 'SELECT count(*) FROM discovery_papers')[0][0] == 1


def test_retry_after_is_installation_wide_even_with_force_other_topic(disc):
    def fail(*_):
        raise d.DiscoveryError('http_429', 120)
    result = d.run_sync(disc.runtime, policy(), fetcher=fail)
    assert result['error:http_429'] == 1
    result = d.run_sync(disc.runtime, policy(topics=['cs.LG'], max_pass_seconds=1),
                        force=True, fetcher=lambda *_: pytest.fail('Retry-After bypassed by another topic'))
    assert result['bound:time'] == 1 and result['requests'] == 0


def test_parser_has_no_pool_connection(disc, monkeypatch):
    original = d.parse_page
    def parse(*args):
        assert disc.runtime.pool.checkedout() == 0
        return original(*args)
    monkeypatch.setattr(d, 'parse_page', parse)
    assert run(disc)['pages'] == 1


def test_tombstone_and_old_metadata_do_not_resurrect(disc):
    w = disc
    run(w)
    run(w, PAGE_DELETED)
    due(w)
    run(w)
    assert q(w, "SELECT count(*) FROM discovery_papers p WHERE NOT EXISTS (SELECT 1 FROM discovery_deleted_records d WHERE d.remote_id=p.remote_id AND d.oai_datestamp>=p.oai_datestamp)")[0][0] == 0


def test_older_version_same_day_does_not_replace_newer_metadata(disc):
    newer = PAGE_ONE.replace(b'version="v1"', b'version="v2"').replace(b'Discovery Alpha', b'Newer title')
    run(disc, newer)
    run(disc, PAGE_ONE)
    assert q(disc, 'SELECT title,latest_version FROM discovery_papers')[0] == ('Newer title', 'v2')


@pytest.mark.parametrize('fault', ['rollback', 'close'])
def test_known_page_commit_release_fault_retains_rows_without_counting_or_next_http(disc, monkeypatch, fault):
    real = getattr(Session, fault)
    real_commit = Session.commit
    def commit(db):
        source = next((o for o in db.identity_map.values() if isinstance(o, d.DiscoverySource)), None)
        page = source is not None and source.pages_seen == 1
        real_commit(db)
        db.info['page_committed'] = page
    def release(db):
        failed = db.info.get('page_committed', False)
        real(db)
        if failed:
            raise OSError('post commit release fault')
    monkeypatch.setattr(Session, fault, release)
    monkeypatch.setattr(Session, 'commit', commit)
    result = run(disc, max_pages_per_topic=2)
    assert result['halted:discovery_release_unavailable'] == 1 and result['committed_unreleased'] == 1
    assert result['pages'] == 0 and result['requests'] == 1
    assert q(disc, 'SELECT pages_seen,resumption_token,last_error FROM discovery_sources')[0] == (1,'page-two',None)


@pytest.mark.parametrize('landed', [False, True])
def test_page_commit_exception_is_uncertain_without_error_write_or_next_http(disc, monkeypatch, landed):
    w = disc
    real = Session.commit
    def commit(db):
        # Detect page DML in identity map, not claim/prepare/reservation.
        source = next((o for o in db.identity_map.values() if isinstance(o, d.DiscoverySource)), None)
        if source is not None and source.pages_seen == 1:
            if landed:
                real(db)
            raise OSError('private exception canary')
        return real(db)
    monkeypatch.setattr(Session, 'commit', commit)
    result = run(w, max_pages_per_topic=2)
    assert result['halted:discovery_commit_uncertain'] == 1 and result['pages'] == 0 and result['requests'] == 1
    row = q(w, 'SELECT pages_seen,resumption_token,last_error FROM discovery_sources')[0]
    assert row.pages_seen == int(landed) and row.last_error is None
    assert row.resumption_token == ('page-two' if landed else None)


@pytest.mark.parametrize('action', ['rollback', 'close'])
def test_release_failure_stops_before_http(disc, monkeypatch, action):
    w = disc
    real = getattr(Session, action)
    def fail(db):
        real(db)
        if db.get_bind() is w.runtime:
            raise OSError('release fault')
    monkeypatch.setattr(Session, action, fail)
    result = run(w)
    assert result['halted:discovery_release_unavailable'] == 1 and result['requests'] == 0


def test_failed_page_rolls_back_metadata_and_cursor_then_error_checkpoint(disc, monkeypatch):
    w = disc
    original = d.reconcile_arxiv_ids
    def fail(*a, **kw):
        original(*a, **kw)
        raise d.RetargetLimit()
    monkeypatch.setattr(d, 'reconcile_arxiv_ids', fail)
    result = run(w)
    assert result['error:retarget_limit_exceeded'] == 1
    assert q(w, 'SELECT count(*) FROM discovery_papers')[0][0] == 0
    row = q(w, 'SELECT pages_seen,records_seen,resumption_token,last_error FROM discovery_sources')[0]
    assert tuple(row) == (0, 0, None, 'retarget_limit_exceeded')


def test_final_gate_closing_during_page_rolls_back_everything(disc, monkeypatch):
    original = d._write_page
    def write(*args, **kw):
        original(*args, **kw)
        recovery.hold(disc.engine, get_settings(), datetime.now(UTC))
    monkeypatch.setattr(d, '_write_page', write)
    result = run(disc)
    assert result['halted:recovery_gate_closed'] == 1 and result['requests'] == 1
    assert q(disc, 'SELECT count(*) FROM discovery_papers')[0][0] == 0
    assert q(disc, 'SELECT pages_seen,resumption_token,last_error FROM discovery_sources')[0] == (0,None,None)


def test_retry_after_http_date_seconds_bounds_and_oai_allowlist():
    p = policy(retry_after_max_seconds=100)
    assert d.retry_after('9999999999', p) == 100
    assert d.retry_after('3', p) == 3
    assert d.retry_after('-1', p) == 0
    assert 80 < d.retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=90)), p) <= 90
    assert d.retry_after('secret', p) == 0
    data = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="secret-token">secret</error></OAI-PMH>'
    with pytest.raises(d.DiscoveryError, match='^oai_error$'):
        d.parse_page(data, 100000)
    with pytest.raises(d.DiscoveryError, match='record_limit_exceeded'):
        d.parse_page(PAGE_ONE.replace(b'<resumptionToken>page-two</resumptionToken>', b'')
                     .replace(b'</ListRecords>', PAGE_TWO.split(b'<ListRecords>')[1].split(b'<resumptionToken')[0] + b'</ListRecords>'), 100000, 1)


def test_cli_celery_same_boundary_and_disabled(disc, monkeypatch, capsys):
    from app.cli.sync_arxiv import main
    from app.ingest import celery_app as tasks
    w = disc
    get_settings().discovery = policy()
    tasks.settings.discovery = policy()
    monkeypatch.setattr(d, 'fetch_page', lambda *_: PAGE_ONE)
    assert main([]) == 0
    result = tasks.sync_arxiv.run()
    assert result['pages'] == 1
    assert q(w, 'SELECT count(*) FROM discovery_papers')[0][0] == 1
    output = capsys.readouterr().out
    assert 'Discovery:' in output and 'page-two' not in output


@pytest.mark.parametrize('entry', ['cli', 'celery'])
def test_cli_celery_owner_login_is_refused_before_http(disc, monkeypatch, entry):
    from app.cli import sync_arxiv as cli
    from app.ingest import celery_app as tasks
    get_settings().discovery = policy()
    tasks.settings.discovery = policy()
    monkeypatch.setattr(d, 'fetch_page', lambda *_: pytest.fail('HTTP reached'))
    if entry == 'cli':
        monkeypatch.setattr(cli, 'get_engine', lambda: disc.engine)
        assert cli.main([]) == 1
    else:
        monkeypatch.setattr(tasks, 'get_engine', lambda: disc.engine)
        assert tasks.sync_arxiv.run()['halted:worker_runtime_required'] == 1
    assert q(disc, 'SELECT count(*) FROM discovery_sources')[0][0] == 0


@pytest.mark.parametrize('kw', [{'lease_seconds': 40}, {'retarget_limit': 0}, {'retarget_limit': 50001},
                               {'max_records_per_page': 0}, {'max_pass_seconds': 0}, {'http_deadline_seconds': 181}])
def test_config_new_bounds(kw):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        policy(**kw)


@pytest.fixture
def local_http(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    state = SimpleNamespace(status=200, body=PAGE_ONE, delay=0, headers={}, starts=[], closed=threading.Event())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            state.starts.append(time.monotonic())
            self.send_response(state.status)
            self.send_header('Content-Length', str(len(state.body)))
            for k, v in state.headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                for n in range(0, len(state.body), 100):
                    self.wfile.write(state.body[n:n+100]); self.wfile.flush()
                    if state.delay:
                        time.sleep(state.delay)
            except (ConnectionError, OSError):
                state.closed.set()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(d, 'OAI_URL', f'http://127.0.0.1:{server.server_port}/oai')
    yield state
    server.shutdown(); server.server_close(); thread.join(5)


@pytest.mark.parametrize('status', [406, 429, 503, 302, 500])
def test_local_http_status_and_retry_after_without_body_or_redirect(disc, local_http, status):
    local_http.status = status
    local_http.headers = {'Retry-After': '120', 'Location': 'http://forbidden.test/secret'}
    result = d.run_sync(disc.runtime, policy())
    code = f'http_{status}' if status in {406, 429, 503} else 'http_error'
    assert result['error:' + code] == 1 and len(local_http.starts) == 1
    row = q(disc, 'SELECT last_error,next_attempt_at,clock_timestamp() AS now FROM discovery_sources')[0]
    assert row.last_error == code
    if status in {429, 503}:
        assert (row.next_attempt_at - row.now).total_seconds() > 110


def test_slow_body_deadline_cancels_transport_without_late_write(disc, local_http):
    local_http.delay = .3  # each chunk arrives inside operation timeout, total exceeds deadline
    start = time.monotonic()
    result = d.run_sync(disc.runtime, policy(timeout_seconds=2, http_deadline_seconds=1))
    assert result['error:http_deadline'] == 1 and time.monotonic() - start < 4
    assert disc.runtime.pool.checkedout() == 0
    assert local_http.closed.wait(3)
    assert q(disc, 'SELECT count(*) FROM discovery_papers')[0][0] == 0
    assert q(disc, 'SELECT pages_seen FROM discovery_sources')[0][0] == 0


@pytest.mark.parametrize('kind', ['oversize', 'malformed', 'encoding'])
def test_local_http_body_limits(disc, local_http, kind):
    if kind == 'oversize':
        local_http.body = b'x' * 2000
    elif kind == 'malformed':
        local_http.body = b'not xml secret'
    else:
        local_http.headers = {'Content-Encoding': 'gzip'}
    result = d.run_sync(disc.runtime, policy(max_response_bytes=1024))
    code = {'oversize': 'response_too_large', 'malformed': 'invalid_xml', 'encoding': 'unsupported_encoding'}[kind]
    assert result['error:' + code] == 1


def test_lease_lock_timeout_has_busy_code(disc):
    db = Session(disc.engine)
    try:
        db.execute(text('SELECT * FROM discovery_request_clock FOR UPDATE'))
        result = run(disc)
        assert result['busy:discovery_lock_busy'] == 1 and result['requests'] == 0
    finally:
        db.rollback(); db.close()


def test_0046_upgrade_preserves_existing_coordinator_cursor_and_metadata(empty_db_url):
    from sqlalchemy import create_engine
    from tests.conftest import upgrade
    upgrade(empty_db_url, '0045')
    engine = create_engine(empty_db_url, hide_parameters=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO discovery_sources(topic,window_from,window_until,resumption_token,pages_seen,records_seen) VALUES('cs.AI','2026-09-28','2026-09-29','token-secret',7,13)"))
            conn.execute(text("UPDATE discovery_request_clock SET next_request_at='2026-10-11T00:00:00Z'"))
            before = tuple(conn.execute(text('SELECT * FROM discovery_sources')).one())
        upgrade(empty_db_url)
        with engine.connect() as conn:
            assert tuple(conn.execute(text('SELECT * FROM discovery_sources')).one()) == before
            clock = conn.execute(text('SELECT next_request_at,lease_token,lease_until FROM discovery_request_clock')).one()
            assert clock.next_request_at == datetime(2026, 10, 11, tzinfo=UTC)
            assert clock.lease_token is clock.lease_until is None
    finally:
        engine.dispose()
