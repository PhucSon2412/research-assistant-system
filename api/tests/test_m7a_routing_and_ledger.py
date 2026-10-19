"""M7a offline: provider routing policy, adapter pinning and the live-experiment ledger.

Every HTTP exchange here is a local MockTransport; no key, provider or network.
"""
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.config import ProviderRouting, Settings
from app.ingest.embedding import EmbeddingError, embed
from app.search import RerankError, rerank
from scripts import m7a_live

SECRET = 'sk-or-v1-synthetic-not-a-real-key-0123456789'


def settings(**overrides):
    s = Settings(db=None, database_url='postgresql+psycopg://test@localhost/test', **overrides)
    s.embedding.api_key = SecretStr(SECRET)
    return s


@pytest.mark.parametrize('bad', [dict(only=()), dict(only=('Azure',)), dict(only=('azure', 'azure')),
    dict(only=('a b',)), dict(only=tuple(f'p{i}' for i in range(9))), dict(only=('azure',), zdr=False),
    dict(only=('azure',), allow_fallbacks=True), dict(only=('azure',), data_collection='allow')])
def test_routing_policy_rejects_open_or_malformed_values(bad):
    with pytest.raises(ValidationError):
        ProviderRouting(**bad)


def test_routing_defaults_pin_reviewed_zdr_providers_and_pricing_stays_off():
    s = settings()
    assert (s.embedding.routing.only, s.rerank.routing.only, s.generation.routing.only) == (
        ('azure',), ('fireworks',), ('azure',))
    assert not (s.embedding.pricing_verified or s.rerank.pricing_verified or s.generation.pricing_verified)
    variant = settings(generation={'routing': {'only': ['azure', 'azure/eu']}})
    body = variant.generation.routing.body({'prompt': 2.0, 'completion': 10.0}, require_parameters=True)
    assert body == {'only': ['azure', 'azure/eu'], 'allow_fallbacks': False, 'data_collection': 'deny',
                    'zdr': True, 'max_price': {'prompt': '2.000000', 'completion': '10.000000'},
                    'require_parameters': True}


def _client(monkeypatch, target, payload):
    original = httpx.Client
    monkeypatch.setattr(target, lambda timeout: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), timeout=timeout))


def test_embedding_rejects_allowlisted_identity_from_unpinned_provider(monkeypatch):
    s = settings(embedding={'routing': {'only': ['fireworks']}})
    s.embedding.api_key = SecretStr(SECRET)
    _client(monkeypatch, 'app.ingest.embedding.httpx.Client', {'model': 'text-embedding-3-small',
            'provider': 'Azure', 'data': [{'embedding': [0.1]}], 'usage': {'prompt_tokens': 1}})
    with pytest.raises(EmbeddingError) as error:
        embed('synthetic', s)
    assert error.value.code == 'provider_response_invalid' and error.value.response_provider == 'Azure'


def test_rerank_rejects_allowlisted_identity_from_unpinned_provider(monkeypatch):
    s = settings(rerank={'routing': {'only': ['azure']}})
    s.embedding.api_key = SecretStr(SECRET)
    _client(monkeypatch, 'app.search.httpx.Client', {'model': 'accounts/fireworks/models/qwen3-reranker-8b',
            'provider': 'Fireworks', 'results': [{'index': 0, 'relevance_score': .5}]})
    with pytest.raises(RerankError) as error:
        rerank('q', ['d'], s)
    assert error.value.code == 'rerank_response_invalid'


def test_ledger_caps_requests_and_usd_and_survives_crash(tmp_path):
    ledger = m7a_live.Ledger(tmp_path / 'state')
    with ledger.exclusive():
        first = ledger.admit('generation', Decimal('0.05'), 'test')
    # Process "dies" before an outcome: a fresh ledger still counts the full ceiling.
    again = m7a_live.Ledger(tmp_path / 'state')
    assert again.state() == {'requests': 1, 'committed_usd': Decimal('0.05'), 'slots': 1, 'open': [first]}
    again.append({'event': 'outcome', 'seq': first, 'status': 'completed', 'provider_cost_usd': 0.001})
    assert again.state()['committed_usd'] == Decimal('0.001')
    for _ in range(9):
        again.admit('generation', Decimal('0.05'), 'test')
    with pytest.raises(SystemExit, match='task ceiling'):
        again.admit('generation', Decimal('0.05'), 'test')  # 0.451 + 0.05 > 0.50
    for _ in range(10):
        again.admit('embedding-query', Decimal('0.0001'), 'test')
    with pytest.raises(SystemExit, match='task ceiling'):
        again.admit('embedding-query', Decimal('0.0001'), 'test')  # 21st request
    slot = m7a_live.Ledger(tmp_path / 'other')
    unused = slot.admit('generation', Decimal('0.05'), 'runtime allowance')
    slot.append({'event': 'outcome', 'seq': unused, 'status': 'not_sent', 'provider_cost_usd': None})
    # PR #64 P2-1: not_sent inferred from silence (no host revocation proof) stays committed.
    assert slot.state()['requests'] == 1 and slot.state()['committed_usd'] == Decimal('0.05')
    proven = slot.admit('generation', Decimal('0.05'), 'runtime allowance')
    slot.append({'event': 'outcome', 'seq': proven, 'status': 'not_sent', 'proof': 'permit_revoked_by_host'})
    assert slot.state()['requests'] == 1 and slot.state()['slots'] == 2
    for bad in (Decimal(0), Decimal('NaN')):
        with pytest.raises(SystemExit, match='ceiling unknown'):
            again.admit('rerank', bad, 'test')


def test_ledger_lock_blocks_concurrent_runs_and_requires_external_state(tmp_path):
    ledger = m7a_live.Ledger(tmp_path / 'state')
    with ledger.exclusive():
        with pytest.raises(SystemExit, match='lock present'):
            with m7a_live.Ledger(tmp_path / 'state').exclusive():
                pass
    with pytest.raises(SystemExit, match='outside the repository'):
        m7a_live.Ledger(m7a_live.REPO / 'docs')


def test_key_line_with_spaces_is_read_alone_and_never_written(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    (tmp_path / '.env').write_text(f'OTHER_SECRET=x\nOPENROUTER_API_KEY = {SECRET}\n', encoding='utf-8')
    monkeypatch.setattr(m7a_live, 'REPO', tmp_path)
    assert m7a_live.load_key() == SECRET
    assert m7a_live.redact(f'Bearer {SECRET}', SECRET) == 'Bearer <redacted>'


def _probe(tmp_path, monkeypatch, respond, route='embedding-document'):
    monkeypatch.setattr(m7a_live.httpx, 'HTTPTransport', lambda: httpx.MockTransport(respond))
    return m7a_live.probe(tmp_path / 'state', SECRET, route)


def test_probe_delegates_records_bounded_receipt_and_redacts(tmp_path, monkeypatch, capsys):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        assert request.headers['authorization'] == f'Bearer {SECRET}'
        return httpx.Response(200, json={'id': 'embd-synthetic', 'object': 'list', 'model': 'text-embedding-3-small',
            'provider': 'Azure', 'data': [{'embedding': [0.25] * 8}],
            'usage': {'prompt_tokens': 30, 'total_tokens': 30, 'cost': 6e-07}})
    record = _probe(tmp_path, monkeypatch, respond)
    assert record['status'] == 'completed' and record['provider_cost_usd'] == 6e-07
    assert record['checks']['dimensions'] == 8 and record['checks']['response_provider'] == 'Azure'
    assert bodies[0]['provider']['only'] == ['azure'] and bodies[0]['input_type'] == 'search_document'
    saved = (tmp_path / 'state' / 'ledger.jsonl').read_text(encoding='utf-8')
    assert SECRET not in saved and '0.25' not in saved  # no key, no vector values
    assert record['observed'][0]['id'] == 'embd-synthetic'


@pytest.mark.parametrize('status,expected', [(500, 'uncertain'), (429, 'rejected')])
def test_probe_failures_keep_ceiling_committed(tmp_path, monkeypatch, status, expected):
    record = _probe(tmp_path, monkeypatch, lambda request: httpx.Response(status, json={'error': {'code': status}}))
    assert record['status'] == expected and record['provider_cost_usd'] is None
    state = m7a_live.Ledger(tmp_path / 'state').state()
    assert state['requests'] == 1 and state['committed_usd'] > 0


def test_probe_identity_outside_pin_is_uncertain_but_billed_cost_counts(tmp_path, monkeypatch):
    record = _probe(tmp_path, monkeypatch, lambda request: httpx.Response(200, json={
        'id': 'embd-x', 'model': 'text-embedding-3-small', 'provider': 'OpenAI',
        'data': [{'embedding': [0.1]}], 'usage': {'prompt_tokens': 3, 'cost': 1e-07}}))
    assert record['status'] == 'uncertain' and record['error_code'] == 'provider_response_invalid'
    assert record['provider_cost_usd'] == 1e-07


def test_reconcile_is_read_only_and_bounded(tmp_path, monkeypatch):
    ledger = m7a_live.Ledger(tmp_path / 'state')
    ledger.append({'event': 'outcome', 'seq': 1, 'observed': [{'id': 'gen-synthetic-1'}]})
    calls = []

    def fake_get(client, path, key, params=None, timeout=30.0):
        calls.append(path)
        return (404, None) if path == '/generation' else (200, {'data': {'usage': 1.0, 'usage_daily': 0.01}})
    monkeypatch.setattr(m7a_live, '_get', fake_get)
    monkeypatch.setattr(m7a_live.time, 'sleep', lambda s: None)
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(m7a_live.time, 'monotonic', lambda: next(clock))
    out = m7a_live.reconcile(tmp_path / 'state', SECRET, 9999)
    assert out['pending'] == ['gen-synthetic-1'] and out['found'] == {}
    # PR #64 P2-3: the deadline also bounds /key; it is skipped once time ran out.
    assert set(calls) == {'/generation'} and len(calls) <= 5
    assert out['deadline_reached'] is True and out['key_status'] == 'skipped_deadline'


def _chat(selected_model, provider='Azure'):
    s = settings()
    return s, {'object': 'chat.completion', 'model': s.generation.model_id,
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{"claims":[]}'}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
        'openrouter_metadata': {'requested': s.generation.model_id, 'endpoints': {'available': [
            {'model': selected_model, 'provider': provider, 'selected': True}]}}}


@pytest.mark.parametrize('selected,provider,ok', [
    ('openai/gpt-6-sol-20260922', 'Azure', True),   # observed live dated endpoint
    ('openai/gpt-6-sol', 'Azure', True),
    ('openai/gpt-6-sol-20250101', 'Azure', False),  # other snapshot: not reviewed
    ('openai/gpt-6-sol-pro-20260922', 'Azure', False),
    ('openai/gpt-6-sol-20260922', 'OpenAI', False),  # outside the pinned ZDR provider
    (None, 'Azure', False)])
def test_generation_dated_endpoint_mapping_is_finite(monkeypatch, selected, provider, ok):
    from app.rag import GenerationError, generate
    s, payload = _chat(selected, provider)
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **kw: httpx.Response(200, json=payload))
    if ok:
        assert generate([{'role': 'user', 'content': 'synthetic'}], s).response_provider == 'Azure'
    else:
        with pytest.raises(GenerationError) as error:
            generate([{'role': 'user', 'content': 'synthetic'}], s)
        assert error.value.code == 'rag_provider_response_invalid'


# (prompt_tokens, completion_tokens, provider-billed USD) observed live in M7a; two
# prompts above the cache threshold were billed at the $2.5/M cache-write price.
LIVE_GENERATION_CHARGES = [(365, 193, '0.00266'), (376, 157, '0.002322'), (405, 120, '0.00201'),
                           (1399, 217, '0.005666'), (637, 11, '0.001384'), (1456, 144, '0.0050785')]


def test_generation_estimate_never_below_observed_live_charge():
    from app.rag import money
    p = settings().generation
    assert p.input_usd_per_million_ceiling == 2.5 and p.max_input_usd_per_million_tokens == 2.0
    old = [money(i, p.max_input_usd_per_million_tokens) + money(o, p.max_output_usd_per_million_tokens)
           for i, o, _ in LIVE_GENERATION_CHARGES]
    new = [money(i, p.input_usd_per_million_ceiling) + money(o, p.max_output_usd_per_million_tokens)
           for i, o, _ in LIVE_GENERATION_CHARGES]
    billed = [Decimal(b) for *_, b in LIVE_GENERATION_CHARGES]
    assert any(e < b for e, b in zip(old, billed))  # the pre-M7a formula undercounted
    assert all(e >= b for e, b in zip(new, billed))
    ceiling = money(p.max_input_tokens_estimate, p.input_usd_per_million_ceiling) + money(
        p.max_output_tokens, p.max_output_usd_per_million_tokens)
    assert ceiling <= Decimal(str(p.max_request_usd))
