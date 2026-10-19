"""PR #64 review fixes: receipt cost validation and bounded reconcile polling (offline only)."""
import json
import math
from decimal import Decimal

import httpx
import pytest

from scripts import m7a_live

SECRET = 'sk-or-v1-synthetic-not-a-real-key-0123456789'
CEILING = Decimal(16_384) * Decimal('0.1') / Decimal(1_000_000)  # embedding probe ceiling


def _embed_probe(tmp_path, monkeypatch, usage):
    def respond(request):
        body = {'id': 'gen-emb-synthetic', 'model': 'text-embedding-3-small', 'provider': 'Azure',
                'data': [{'embedding': [0.1] * 8}], 'usage': usage}
        return httpx.Response(200, content=json.dumps(body).encode(), headers={'content-type': 'application/json'})
    monkeypatch.setattr(m7a_live.httpx, 'HTTPTransport', lambda: httpx.MockTransport(respond))
    return m7a_live.probe(tmp_path / 'state', SECRET, 'embedding-document')


@pytest.mark.parametrize('cost,valid', [(0.001, True), (0, True), (0.0, True), (-1, False), (-0.0001, False),
    (float('nan'), False), (float('inf'), False), (True, False), (False, False), ('0.001', False),
    (None, False), ('missing', False)])
def test_probe_receipt_cost_contract_never_lowers_committed(tmp_path, monkeypatch, cost, valid):
    usage = {'prompt_tokens': 3, 'total_tokens': 3}
    if cost != 'missing':
        usage['cost'] = cost
    record = _embed_probe(tmp_path, monkeypatch, usage)
    assert record['status'] == 'completed'  # the adapter itself ignores cost
    state = m7a_live.Ledger(tmp_path / 'state').state()  # re-read from disk
    if valid:
        assert state['committed_usd'] == Decimal(str(cost))
    else:
        assert state['committed_usd'] == CEILING, state
        assert record['provider_cost_usd'] is None


@pytest.mark.parametrize('stored', [-1, -0.5, 'NaN', 'Infinity', True, '0.001', [0.001], {'usd': 0}])
def test_resumed_ledger_line_with_invalid_cost_keeps_ceiling(tmp_path, stored):
    ledger = m7a_live.Ledger(tmp_path / 'state')
    seq = ledger.admit('generation', Decimal('0.05'), 'test')
    # Written by an older/compromised producer; read back by a new process.
    ledger.append({'event': 'outcome', 'seq': seq, 'status': 'completed', 'provider_cost_usd': stored})
    state = m7a_live.Ledger(tmp_path / 'state').state()
    assert state['committed_usd'] == Decimal('0.05') and state['requests'] == 1


def test_valid_zero_cost_releases_ceiling(tmp_path):
    ledger = m7a_live.Ledger(tmp_path / 'state')
    seq = ledger.admit('rerank', Decimal('0.01'), 'test')
    ledger.append({'event': 'outcome', 'seq': seq, 'status': 'completed', 'provider_cost_usd': 0})
    assert m7a_live.Ledger(tmp_path / 'state').state()['committed_usd'] == 0


class Clock:
    def __init__(self):
        self.now, self.events = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.events.append(('sleep', self.now, seconds))
        self.now += seconds


def _poll(tmp_path, monkeypatch, ids, found_after=None, per_get=30.0):
    ledger = m7a_live.Ledger(tmp_path / 'state')
    ledger.append({'event': 'outcome', 'seq': 1, 'observed': [{'id': i} for i in ids]})
    clock = Clock()

    def fake_get(client, path, key, params=None, timeout=30.0):
        start = clock.now
        if path == '/generation' and (found_after is None or start < found_after):
            spent = min(per_get, timeout)  # a slow GET uses all the time it is granted
            clock.now += spent
            clock.events.append(('get', start, path, timeout))
            return 404, None
        clock.now += 0.01
        clock.events.append(('get', start, path, timeout))
        if path == '/key':
            return 200, {'data': {'usage': 1.0, 'usage_daily': 0.01, 'limit_remaining': 49}}
        return 200, {'data': {'total_cost': 0.001, 'api_type': 'embeddings'}}
    monkeypatch.setattr(m7a_live, '_get', fake_get)
    monkeypatch.setattr(m7a_live.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(m7a_live.time, 'sleep', clock.sleep)
    return m7a_live.reconcile(tmp_path / 'state', SECRET, 300), clock


def test_reconcile_deadline_covers_every_get_sleep_and_key(tmp_path, monkeypatch):
    ids = [f'gen-synthetic-{n:02d}' for n in range(19)]
    out, clock = _poll(tmp_path, monkeypatch, ids)
    gets = [e for e in clock.events if e[0] == 'get']
    assert clock.now <= 300, clock.now  # total elapsed including the last GET
    assert all(start < 300 and start + timeout <= 300 + 1e-9 for _, start, _, timeout in gets)
    assert not any(e[2] == '/key' for e in gets)  # no /key after the deadline
    assert out['pending'] and out['deadline_reached'] is True and out['key_status'] == 'skipped_deadline'


def test_reconcile_sleep_is_cut_to_remaining_and_found_ids_kept(tmp_path, monkeypatch):
    ids = ['gen-a', 'gen-b']
    out, clock = _poll(tmp_path, monkeypatch, ids, found_after=295, per_get=146)
    assert clock.now <= 300
    assert all(e[1] + e[2] <= 300 + 1e-9 for e in clock.events if e[0] == 'sleep')
    assert set(out['found']) | set(out['pending']) == set(ids)


def test_reconcile_all_found_control_reads_key(tmp_path, monkeypatch):
    ids = [f'gen-synthetic-{n:02d}' for n in range(19)]
    out, clock = _poll(tmp_path, monkeypatch, ids, found_after=-1)
    assert out['pending'] == [] and len(out['found']) == 19
    assert out['key_status'] == 200 and not out['deadline_reached'] and clock.now < 1


# ---- P2-1: runtime permits, serialization and fail-closed settlement ----
import threading  # noqa: E402

from scripts import m7a_observer, m7a_runtime_drill as drill  # noqa: E402

LIVE = drill.m7a_live  # the module object the drill itself uses


@pytest.fixture
def gate(tmp_path, monkeypatch):
    fixture = tmp_path / 'fixture'
    g = drill.Gate(LIVE.Ledger(tmp_path / 'state'), fixture / 'permits', fixture / 'receipts.jsonl',
                   grace_s=0.5, poll_s=0.05)
    g.prepare()
    monkeypatch.setenv('M7A_PERMITS', str(fixture / 'permits'))
    monkeypatch.setenv('M7A_RECEIPTS', str(fixture / 'receipts.jsonl'))
    g.delegated = []
    g.cost = 0.001

    def delegate(self, request):
        g.delegated.append(request.url.path)
        body = {'id': f'gen-emb-{len(g.delegated)}', 'model': 'text-embedding-3-small', 'provider': 'Azure',
                'usage': {'prompt_tokens': 3, 'cost': g.cost}}
        return httpx.Response(200, content=json.dumps(body).encode(), request=request,
                              headers={'content-type': 'application/json'})
    monkeypatch.setattr(m7a_observer, '_original', delegate)
    g.settings = LIVE.settings_for('unused-key-for-ceiling')
    return g


def call(path='/api/v1/embeddings'):
    return m7a_observer.observed(httpx.HTTPTransport(), httpx.Request('POST', 'https://openrouter.ai' + path, json={}))


def seed(g, n):
    for _ in range(n):
        seq = g.ledger.admit('seed', Decimal('0.001'), 'seed')
        g.ledger.append({'event': 'outcome', 'seq': seq, 'status': 'completed', 'provider_cost_usd': 0.001})


def test_claimed_permit_without_receipt_is_uncertain_and_21st_refused_before_delegate(gate, monkeypatch):
    seed(gate, 19)
    hold, held = threading.Event(), threading.Event()
    real_append = m7a_observer._append

    def append(row):
        if row['event'] == 'sending' and not held.is_set():
            held.set()
            assert hold.wait(10)  # permit already claimed, marker not yet written
        real_append(row)
    monkeypatch.setattr(m7a_observer, '_append', append)
    late = threading.Thread(target=call)

    def action():
        late.start()
        assert held.wait(10)  # the client side returns while the handler owns the permit
    gate.run('twentieth', ['embedding'], action, gate.settings)
    state = gate.ledger.state()
    assert state['requests'] == 20 and state['open'] == []
    outcome = [r for r in gate.ledger.records() if r['event'] == 'outcome'][-1]
    assert outcome['status'] == 'uncertain' and outcome['error_code'] == 'permit_claimed_no_receipt'
    with pytest.raises(SystemExit, match='task ceiling'):
        gate.run('twenty-first', ['embedding'], call, gate.settings)
    assert gate.delegated == []  # refused before any delegate
    hold.set()
    late.join(10)
    assert gate.delegated == ['/api/v1/embeddings']  # 19 seeded + 1 = 20 actual, ledger says 20
    assert m7a_observer.claim('embeddings') is None  # nothing left to claim
    # The late response is bound to its own permit and never re-settles another slot.
    assert LIVE.Ledger(gate.ledger.path.parent).state()['requests'] == 20


def test_second_action_cannot_start_or_open_permits_while_first_runs(gate):
    busy, release = threading.Event(), threading.Event()
    first = threading.Thread(target=lambda: gate.run('a', ['embedding'], lambda: (busy.set(), release.wait(10)),
                                                     gate.settings))
    first.start()
    assert busy.wait(10)
    opened_before = sorted(p.name for p in (gate.permits / 'open').iterdir())
    with pytest.raises(SystemExit, match='lock present'):
        gate.run('b', ['embedding'], lambda: pytest.fail('overlapping action ran'), gate.settings)
    assert sorted(p.name for p in (gate.permits / 'open').iterdir()) == opened_before  # untouched
    release.set()
    first.join(10)
    assert gate.ledger.state()['slots'] == 1


def test_control_used_permit_completes_and_unused_permit_is_revoked_not_sent(gate):
    result, responses = gate.run('mixed', ['embedding', 'generation'], call, gate.settings)
    assert result.status_code == 200 and len(responses) == 1
    outcomes = {r['seq']: r for r in gate.ledger.records() if r['event'] == 'outcome'}
    assert [o['status'] for o in outcomes.values()] == ['completed', 'not_sent']
    assert outcomes[2]['proof'] == 'permit_revoked_by_host'
    assert gate.ledger.state() == {'requests': 1, 'committed_usd': Decimal('0.001'), 'slots': 2, 'open': []}
    with pytest.raises(httpx.ConnectError):
        call('/api/v1/chat/completions')  # the revoked generation token can no longer be claimed


def test_family_without_permit_is_refused_before_delegate(gate):
    def action():
        with pytest.raises(httpx.ConnectError):
            call('/api/v1/chat/completions')
    gate.run('embed-only', ['embedding'], action, gate.settings)
    assert gate.delegated == []
    assert gate.ledger.state()['requests'] == 0  # the embedding token was revoked unused


def test_restart_with_stale_permit_or_unsettled_slot_fails_closed(gate):
    seq = gate.ledger.admit('runtime:crashed:embedding-document', Decimal('0.0016384'), 'crash')
    with pytest.raises(SystemExit, match='unsettled'):
        gate.run('after-crash', ['embedding'], call, gate.settings)
    gate.ledger.append({'event': 'outcome', 'seq': seq, 'status': 'uncertain', 'provider_cost_usd': None})
    (gate.permits / 'open' / 'oldbatch.0.embeddings').write_text('1', encoding='utf-8')
    with pytest.raises(SystemExit, match='stale open permits'):
        gate.run('after-crash', ['embedding'], call, gate.settings)
    assert not any((gate.permits / 'open').iterdir()) and gate.delegated == []
    assert gate.ledger.state()['committed_usd'] == Decimal('0.0016384')


@pytest.mark.parametrize('cost', [-1, float('nan'), True, '0.001', None])
def test_runtime_receipt_with_invalid_cost_keeps_ceiling(gate, cost):
    gate.cost = cost
    gate.run('bad-cost', ['embedding'], call, gate.settings)
    outcome = [r for r in gate.ledger.records() if r['event'] == 'outcome'][-1]
    assert outcome['status'] == 'uncertain' and outcome['provider_cost_usd'] is None
    assert LIVE.Ledger(gate.ledger.path.parent).state()['committed_usd'] == Decimal('0.0016384')
