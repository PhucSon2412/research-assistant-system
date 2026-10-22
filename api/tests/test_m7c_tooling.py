"""M7c offline: task ceiling, key source, licence policy, gold locator, judge transport, settlement pairing.

No key, provider, arXiv or network: HTTP is a local MockTransport and the key is a synthetic string.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # CI runs pytest from api/
from scripts import m7c_live, m7c_papers, m7c_runtime  # noqa: E402

SECRET = 'sk-or-v1-synthetic-m7c-not-a-real-key-0123456789'
CC_BY = 'http://creativecommons.org/licenses/by/4.0/'


# --- ceiling -----------------------------------------------------------------------------------

def _settle(ledger, seq, cost):
    ledger.append({'event': 'outcome', 'seq': seq, 'route': 'x', 'status': 'completed', 'provider_cost_usd': cost})


def test_ledger_stops_new_calls_at_80_percent_of_requests(tmp_path):
    ledger = m7c_live.Ledger(tmp_path / 'state')
    for _ in range(320):
        _settle(ledger, ledger.admit('embedding', Decimal('0.0001'), 'test'), 0.0)
    assert ledger.state()['requests'] == 320
    with pytest.raises(SystemExit, match='80%'):
        ledger.admit('embedding', Decimal('0.0001'), 'test')


def test_ledger_stops_at_80_percent_of_usd_and_counts_open_calls_at_ceiling(tmp_path):
    ledger = m7c_live.Ledger(tmp_path / 'state')
    seq = ledger.admit('generation', Decimal('3.90'), 'test')  # open: full ceiling stays committed
    assert ledger.state()['committed_usd'] == Decimal('3.90')
    with pytest.raises(SystemExit, match='80%'):
        ledger.admit('judge', Decimal('0.11'), 'test')
    _settle(ledger, seq, 0.5)
    ledger.admit('judge', Decimal('0.11'), 'test')
    assert ledger.limits() == (Decimal('4.000'), 320)


@pytest.mark.parametrize('cost', [-1.0, float('nan'), float('inf'), True, '0.01', None])
def test_invalid_reported_cost_keeps_the_ceiling(tmp_path, cost):
    ledger = m7c_live.Ledger(tmp_path / 'state')
    seq = ledger.admit('generation', Decimal('0.05'), 'test')
    _settle(ledger, seq, cost)
    assert ledger.state()['committed_usd'] == Decimal('0.05')


def test_ledger_requires_external_state(tmp_path):
    with pytest.raises(SystemExit):
        m7c_live.Ledger(m7c_live.REPO / 'evaluation')


# --- key source ----------------------------------------------------------------------------------

def test_key_only_from_the_owner_file_never_env_or_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-or-env-must-not-be-used')
    monkeypatch.setenv('RAS_EMBEDDING__API_KEY', 'sk-or-env-must-not-be-used')
    with pytest.raises(SystemExit, match='absent'):
        m7c_live.load_key(tmp_path / 'missing')
    (tmp_path / '.env').write_text('OPENROUTER_API_KEY=' + SECRET, encoding='utf-8')
    with pytest.raises(SystemExit, match='.env'):
        m7c_live.load_key(tmp_path / '.env')
    key = tmp_path / 'openrouter_api_key'
    key.write_text(SECRET + '\r\n', encoding='utf-8')
    assert m7c_live.load_key(key) == SECRET
    key.write_text('two words', encoding='utf-8')
    with pytest.raises(SystemExit, match='malformed'):
        m7c_live.load_key(key)


def test_default_key_path_is_the_owner_secret_file():
    assert m7c_live.KEY_FILE.parts[-3:] == ('ras-pilot', 'secrets', 'openrouter_api_key')


# --- licence policy ---------------------------------------------------------------------------------

@pytest.mark.parametrize('abs_url,oai_url,versions,version,accepted,reason', [
    (CC_BY, CC_BY, [1], 1, True, None),
    ('https://creativecommons.org/licenses/by-sa/4.0', 'http://creativecommons.org/licenses/by-sa/4.0/', [1, 2], 2,
     True, None),
    ('http://creativecommons.org/publicdomain/zero/1.0/', 'http://creativecommons.org/publicdomain/zero/1.0/', [3], 3,
     True, None),
    ('http://arxiv.org/licenses/nonexclusive-distrib/1.0/', 'http://arxiv.org/licenses/nonexclusive-distrib/1.0/', [1],
     1, False, 'abs_licence_not_open'),
    ('http://creativecommons.org/licenses/by-nc/4.0/', 'http://creativecommons.org/licenses/by-nc/4.0/', [1], 1, False,
     'abs_licence_not_open'),
    ('http://creativecommons.org/licenses/by-nd/4.0/', 'http://creativecommons.org/licenses/by-nd/4.0/', [1], 1, False,
     'oai_licence_not_open'),
    ('http://creativecommons.org/licenses/by-nc-sa/4.0/', CC_BY, [1], 1, False, 'licence_sources_disagree'),
    (CC_BY, None, [1], 1, False, 'oai_licence_unknown'),
    (None, CC_BY, [1], 1, False, 'abs_licence_unknown'),
    (CC_BY, CC_BY, [1, 2], 1, False, 'not_latest_version'),
    (CC_BY, CC_BY, [], 1, False, 'not_latest_version'),
])
def test_licence_policy_accepts_only_open_licences_of_the_exact_latest_version(abs_url, oai_url, versions, version,
                                                                                accepted, reason):
    verdict = m7c_papers.licence_verdict('2601.00001', version, abs_url, oai_url, versions)
    assert verdict['accepted'] is accepted
    assert (reason in verdict['reasons']) if reason else verdict['reasons'] == []
    assert (verdict['licence'] is None) is (not accepted)


def test_committed_manifest_lists_only_accepted_licences_and_no_pdf_in_repo():
    manifest = json.loads(m7c_papers.MANIFEST.read_text(encoding='utf-8'))
    assert len(manifest['papers']) >= 10
    for paper in manifest['papers']:
        assert m7c_papers.normalise_licence(paper['licence_url']) in m7c_papers.ACCEPTED
        assert paper['licence'] == m7c_papers.ACCEPTED[m7c_papers.normalise_licence(paper['licence_url'])]
        assert len(paper['sha256']) == 64 and paper['bytes'] > 0 and paper['attribution']
    assert not list(m7c_papers.MANIFEST.parent.rglob('*.pdf'))


def test_arxiv_client_refuses_other_hosts():
    client = m7c_papers.Arxiv()
    try:
        with pytest.raises(SystemExit, match='non-arXiv'):
            client.get('https://openrouter.ai/api/v1/models')
    finally:
        client.close()


def test_outside_refuses_repository_paths():
    with pytest.raises(SystemExit):
        m7c_papers.outside(m7c_papers.REPO / 'evaluation')


# --- gold locator ------------------------------------------------------------------------------------

PAGE1 = 'Abstract\n  We   release a bench-\n    mark of 666 tasks.\nReferences\n[1] 666 tasks.'
PAGE2 = '1 Introduction\nWe release a bench- mark of 666 tasks again.'
PARSED = {'parser': 'pypdf-test', 'pages': [{'number': 1, 'text': PAGE1}, {'number': 2, 'text': PAGE2}],
          'segments': [{'page': 1, 'kind': 'abstract', 'start': 9, 'end': PAGE1.index('References')},
                       {'page': 2, 'kind': 'body', 'start': 15, 'end': len(PAGE2)}]}


def test_gold_locator_is_whitespace_insensitive_and_returns_the_exact_parser_substring():
    hits = m7c_papers.locate_gold(PARSED, 'We release a bench- mark of 666 tasks')
    assert [(h['page'], h['segment']) for h in hits] == [(1, 'abstract'), (2, 'body')]
    first = hits[0]
    assert PARSED['pages'][0]['text'][first['start']:first['end']] == first['quote']
    assert first['quote'] == 'We   release a bench-\n    mark of 666 tasks'
    reference = [h for h in m7c_papers.locate_gold(PARSED, '[1] 666 tasks') if h['segment']]
    assert reference == []  # bibliography text is never an indexed gold region


def test_committed_questions_are_agent_authored_and_annotated():
    data = json.loads(m7c_papers.QUESTIONS.read_text(encoding='utf-8'))
    assert data['authored_by'] == 'ai_agent' and 30 <= len(data['questions']) <= 40
    assert {q['group'] for q in data['questions']} >= {'single_paper', 'comparison', 'no_answer', 'partial', 'injection'}
    assert any(q['split'] == 'holdout' for q in data['questions'])
    for q in data['questions']:
        assert q['authored_by'] == 'ai_agent'
        assert bool(q['gold']) == (q['expected']['status'] == 'answered')
        for gold in q['gold']:
            assert {'page', 'start', 'end', 'segment', 'parser'} <= set(gold) and gold['segment'] in ('body', 'abstract')
            assert gold['end'] - gold['start'] == len(gold['quote']) <= 200


# --- judge transport ---------------------------------------------------------------------------------

ROUTE = m7c_live.JudgeRoute('anthropic/judge-test', 'amazon-bedrock', 'Amazon Bedrock', Decimal('2'), Decimal('10'),
                            max_input_tokens=5000, max_output_tokens=500)


def _judge_reply(provider='Amazon Bedrock', finish='stop', cost=0.0012, status=200):
    body = {'id': 'gen-judge-1', 'model': 'anthropic/judge-test', 'provider': provider, 'object': 'chat.completion',
            'choices': [{'finish_reason': finish, 'message': {'content': '{"ok": true}'}}],
            'usage': {'prompt_tokens': 300, 'completion_tokens': 20, 'cost': cost,
                      'prompt_tokens_details': {'cached_tokens': 0}},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': provider, 'selected': True}]}}}
    return httpx.Response(status, json=body)


def _transport(monkeypatch, handler):
    original = httpx.Client.__init__
    seen = []

    def init(self, *args, **kwargs):
        def record(request):
            seen.append(request)
            return handler(request)
        kwargs['transport'] = httpx.MockTransport(record)
        original(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, '__init__', init)
    return seen


def test_judge_call_pins_zdr_provider_and_settles_from_reported_cost(tmp_path, monkeypatch):
    seen = _transport(monkeypatch, lambda request: _judge_reply())
    transport = m7c_live.JudgeTransport(tmp_path / 'state', SECRET, ROUTE)
    out = transport([{'role': 'user', 'content': 'judge this'}], 'unit')
    body = json.loads(seen[0].content)
    assert body['provider'] == {'only': ['amazon-bedrock'], 'allow_fallbacks': False, 'data_collection': 'deny',
                                'zdr': True, 'require_parameters': True, 'max_price': {'prompt': '2', 'completion': '10'}}
    assert 'temperature' not in body and body['response_format'] == {'type': 'json_object'}
    assert out['content'] == '{"ok": true}' and out['cost_usd'] == 0.0012
    state = transport.ledger.state()
    assert state['requests'] == 1 and state['committed_usd'] == Decimal('0.0012')
    assert SECRET not in (tmp_path / 'state' / 'ledger.jsonl').read_text(encoding='utf-8')


@pytest.mark.parametrize('reply,committed', [
    (lambda r: _judge_reply(provider='Anthropic'), Decimal('0.0012')),       # served outside the pin: no content
    (lambda r: _judge_reply(finish='length'), Decimal('0.0012')),
    (lambda r: _judge_reply(cost=-1), None),                                 # invalid cost: ceiling stays
    (lambda r: httpx.Response(502, json={'error': {'code': 502}}), None),
])
def test_judge_failures_record_no_content_and_keep_cost_conservative(tmp_path, monkeypatch, reply, committed):
    _transport(monkeypatch, reply)
    transport = m7c_live.JudgeTransport(tmp_path / 'state', SECRET, ROUTE)
    out = transport([{'role': 'user', 'content': 'judge this'}], 'unit')
    assert out['content'] is None
    assert transport.ledger.state()['committed_usd'] == (committed if committed is not None else ROUTE.ceiling())


def test_judge_prompt_above_bound_is_not_sent(tmp_path, monkeypatch):
    seen = _transport(monkeypatch, lambda request: _judge_reply())
    transport = m7c_live.JudgeTransport(tmp_path / 'state', SECRET, ROUTE)
    with pytest.raises(SystemExit, match='bound'):
        transport([{'role': 'user', 'content': 'x' * 6000}], 'unit')
    assert not seen and transport.ledger.state()['requests'] == 0


# --- settlement pairing and machine checks ----------------------------------------------------------------

def _action(label, receipts, rows):
    return {'label': label, 'receipts': receipts, 'rows': rows, 'changed_rows': []}


def _row(task, tokens, cost, out=None):
    return {'task': task, 'input_tokens': tokens, 'output_tokens': out, 'cost_usd': cost, 'status': 'completed',
            'cost_status': 'usage_price_estimate', 'reserved_usd': '0.05'}


def test_settlement_pairing_flags_a_settlement_below_the_charge():
    receipts = [{'path': '/api/v1/chat/completions', 'id': 'gen-1',
                 'usage': {'prompt_tokens': 1400, 'completion_tokens': 200, 'cost': 0.0058}}]
    covered = m7c_runtime.pair_settlement([_action('rag-SP-01', receipts, [_row('generation', 1400, '0.0065', 200)])])
    assert covered[0]['settlement_covers_charge'] is True
    low = m7c_runtime.pair_settlement([_action('rag-SP-01', receipts, [_row('generation', 1400, '0.0048', 200)])])
    assert low[0]['settlement_covers_charge'] is False
    unpaired = m7c_runtime.pair_settlement([_action('rag-SP-01', receipts, [_row('generation', 999, '1', 200)])])
    assert unpaired[0]['app_cost_usd'] is None and unpaired[0]['settlement_covers_charge'] is False
    refused = {'task': 'generation', 'input_tokens': None, 'output_tokens': None, 'cost_usd': None,
               'status': 'uncertain', 'cost_status': 'unknown_reserved_ceiling', 'reserved_usd': '0.04512'}
    kept = m7c_runtime.pair_settlement([_action('rag-CMP-03', receipts, [refused])])[0]
    assert kept['app_basis'] == 'uncertain_reservation_kept' and kept['settlement_covers_charge'] is True


def test_settlement_pairing_maps_document_query_and_rerank_rows():
    upload = _action('upload-P01', [{'path': '/api/v1/embeddings', 'id': 'gen-emb-1',
                                     'usage': {'prompt_tokens': 300, 'cost': 0.000006}}],
                     [_row('embed_document', 300, '0.00003')])
    search = _action('search-hybrid-SP-01', [
        {'path': '/api/v1/embeddings', 'id': 'gen-emb-2', 'usage': {'prompt_tokens': 12, 'cost': 0.00000024}},
        {'path': '/api/v1/rerank', 'id': 'gen-rerank-1', 'usage': {'total_tokens': 4000, 'cost': 0.0008}}],
        [_row('embed_query', 12, '0.0000012'), _row('rerank', 4000, '0.0008')])
    pairs = m7c_runtime.pair_settlement([upload, search])
    assert [(p['task'], p['settlement_covers_charge']) for p in pairs] == [
        ('embed_document', True), ('embed_query', True), ('rerank', True)]


PAPERS = {'P01': {'item_id': 'i1', 'owner': 'a'}, 'P02': {'item_id': 'i2', 'owner': 'a'},
          'S-B': {'item_id': 'ib', 'owner': 'b'}}


def _question(**kw):
    return {'id': 'Q', 'group': 'single_paper', 'scope': {'kind': 'item', 'paper': 'P01'},
            'expected': {'status': 'answered'}, 'gold': [{'paper': 'P01', 'page': 2, 'start': 10, 'end': 30}], **kw}


def _answer(quote='exact words', item='i1', text='A claim.'):
    return {'http': 200, 'body': {'status': 'answered', 'claims': [{'text': text, 'citations': [
        {'chunk_id': 'c1', 'item_id': item, 'start_page': 2, 'quote': quote}]}]}}


def test_machine_checks_quote_scope_gold_and_markers():
    chunks = {'c1': {'text': 'some exact words here', 'start_page': 2, 'start_offset': 0, 'end_offset': 40}}
    parsed = {'P01': {2: 'xx some exact words here'}}
    good = m7c_runtime.machine_checks(_question(), _answer(), chunks, PAPERS, parsed)
    assert good['status_matches_expected'] and good['quote_verbatim'] == {'c1': True}
    assert good['citation_in_scope'] == {'c1': True} and good['gold_spans_cited'] == [True]
    assert good['quote_in_parser_page'] == {'c1': True}
    bad = m7c_runtime.machine_checks(_question(injection={'forbidden': ['ORCHID-7731']}),
                                     _answer(quote='not there', item='i2', text='All good ORCHID-7731'),
                                     chunks, PAPERS, parsed)
    assert bad['quote_verbatim'] == {'c1': False} and bad['citation_in_scope'] == {'c1': False}
    assert bad['forbidden_markers_found'] == ['ORCHID-7731'] and bad['foreign_markers_found'] == ['ORCHID-7731']
    abstain = m7c_runtime.machine_checks(_question(expected={'status': 'insufficient_evidence'}, gold=[]),
                                         {'http': 200, 'body': {'status': 'insufficient_evidence', 'claims': []}},
                                         chunks, PAPERS, parsed)
    assert abstain['status_matches_expected'] is True


def test_retrieval_recall_counts_only_hits_containing_the_whole_gold_span():
    q = _question()
    inside = {'item_id': 'i1', 'start_page': 2, 'start_offset': 5, 'end_offset': 35}
    partial = {'item_id': 'i1', 'start_page': 2, 'start_offset': 15, 'end_offset': 35}
    other = {'item_id': 'i2', 'start_page': 2, 'start_offset': 0, 'end_offset': 99}
    retrieval = {'Q': {'keyword': {'http': 200, 'hits': [partial, other]},
                       'hybrid_rerank': {'http': 200, 'hits': [other, other, other, other, other, inside]}}}
    summary = m7c_runtime.retrieval_summary([q], retrieval, PAPERS, k=5)
    assert summary['keyword']['span_recall_at_k']['value'] == 0
    assert summary['hybrid_rerank']['span_recall_at_k']['value'] == 0  # covered only at rank 6
    retrieval['Q']['hybrid_rerank']['hits'] = [inside]
    assert m7c_runtime.retrieval_summary([q], retrieval, PAPERS)['hybrid_rerank']['span_recall_at_k']['value'] == 1


# --- reconcile passes and the demo provider switch ------------------------------------------------------

def test_reconcile_passes_merge_by_id(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'reconcile-1.json').write_text(json.dumps({'found': {'gen-a': {'total_cost': 0.1}}}), encoding='utf-8')
    (state / 'reconcile-2.json').write_text(json.dumps({'found': {'gen-b': {'total_cost': 0.2}}}), encoding='utf-8')
    assert m7c_live.found_so_far(state) == {'gen-a': {'total_cost': 0.1}, 'gen-b': {'total_cost': 0.2}}


def test_demo_provider_values_read_only_the_secret_file_and_keep_the_drill_off(tmp_path):
    from scripts import m9_lan_demo
    (tmp_path / 'secrets').mkdir()
    state = {'root': str(tmp_path)}
    off = m9_lan_demo.provider_values(state, 'pilot')
    assert off == {'OPENROUTER_API_KEY': '', 'RAS_EMBEDDING__PRICING_VERIFIED': 'false',
                   'RAS_RERANK__PRICING_VERIFIED': 'false', 'RAS_GENERATION__PRICING_VERIFIED': 'false'}
    state['provider'] = {'routes': ['embedding', 'generation']}
    with pytest.raises(SystemExit, match='absent'):
        m9_lan_demo.provider_values(state, 'pilot')
    (tmp_path / 'secrets' / 'openrouter_api_key').write_text(SECRET + '\n', encoding='utf-8')
    on = m9_lan_demo.provider_values(state, 'pilot')
    assert on['OPENROUTER_API_KEY'] == SECRET and on['RAS_EMBEDDING__PRICING_VERIFIED'] == 'true'
    assert on['RAS_RERANK__PRICING_VERIFIED'] == 'false' and on['RAS_GENERATION__PRICING_VERIFIED'] == 'true'
    assert m9_lan_demo.provider_values(state, 'drill') == off  # the restore drill never gets the key


# (prompt_tokens, completion_tokens, cache_write_tokens, cached_tokens, provider-billed USD) of the 26 settled
# M7c live generations (Azure, openai/gpt-6-sol-20260922): cache writes billed at $2.5/M, one cache read.
M7C_LIVE_GENERATIONS = [
    (1861, 329, 1858, 0, '0.007941'), (1561, 93, 1558, 0, '0.004831'), (2175, 114, 2172, 0, '0.006576'),
    (3399, 254, 3396, 0, '0.011036'), (1750, 295, 1747, 0, '0.0073235'), (3330, 157, 3327, 0, '0.0098935'),
    (1616, 344, 1613, 0, '0.0074785'), (3191, 333, 3188, 0, '0.011306'), (1073, 283, 1070, 0, '0.005511'),
    (2048, 142, 2045, 0, '0.0065385'), (3297, 301, 3294, 0, '0.011251'), (1223, 258, 1220, 0, '0.005636'),
    (3839, 134, 3836, 0, '0.010936'), (1176, 205, 1173, 0, '0.0049885'), (2181, 265, 2178, 0, '0.008101'),
    (2416, 430, 2413, 0, '0.0103385'), (3875, 161, 3872, 0, '0.011296'), (1193, 27, 1190, 0, '0.003251'),
    (1084, 416, 1081, 0, '0.0068685'), (2153, 121, 2150, 0, '0.006591'), (601, 100, 0, 0, '0.002202'),
    (743, 125, 0, 0, '0.002736'), (753, 125, 0, 0, '0.002756'), (753, 147, 0, 0, '0.002976'),
    (1007, 57, 0, 0, '0.002584'), (3875, 224, 0, 3872, '0.0030204')]


def test_generation_settlement_covers_every_m7c_live_charge():
    from app.config import Settings
    from app.rag import money
    p = Settings(db=None, database_url='postgresql+psycopg://test@localhost/test').generation
    settled = [money(i, p.input_usd_per_million_ceiling) + money(o, p.max_output_usd_per_million_tokens)
               for i, o, *_ in M7C_LIVE_GENERATIONS]
    billed = [Decimal(b) for *_, b in M7C_LIVE_GENERATIONS]
    assert all(s >= b for s, b in zip(settled, billed))
    # The live split behind the charge: uncached input $2/M, cache write $2.5/M, cache read $0.2/M, output $10/M.
    for (i, o, write, read, b) in M7C_LIVE_GENERATIONS:
        split = (Decimal(i - write - read) * 2 + Decimal(write) * Decimal('2.5') + Decimal(read) * Decimal('0.2')
                 + Decimal(o) * 10) / 1_000_000
        assert abs(split - Decimal(b)) <= Decimal('0.0000005')


def test_demo_generation_limit_keeps_the_admission_ceiling_admissible(tmp_path):
    from scripts import m9_lan_demo
    from app.config import Settings
    from app.rag import money
    limits = m9_lan_demo.generation_limits(1024)
    assert limits == {'max_output_tokens': 1024, 'max_request_usd': '0.06', 'admission_ceiling_usd': 0.05024}
    assert m9_lan_demo.generation_limits(512)['max_request_usd'] == '0.05'  # the code default stays admissible
    p = Settings(db=None, database_url='postgresql+psycopg://test@localhost/test',
                 generation={'max_output_tokens': 1024, 'max_request_usd': 0.06}).generation
    ceiling = money(p.max_input_tokens_estimate, p.input_usd_per_million_ceiling) + money(
        p.max_output_tokens, p.max_output_usd_per_million_tokens)
    assert ceiling == Decimal('0.05024') and ceiling <= Decimal(limits['max_request_usd'])
    with pytest.raises(SystemExit):
        m9_lan_demo.generation_limits(8192)
    (tmp_path / 'secrets').mkdir()
    (tmp_path / 'secrets' / 'openrouter_api_key').write_text(SECRET, encoding='utf-8')
    state = {'root': str(tmp_path), 'provider': {'routes': ['generation'], 'generation': limits}}
    values = m9_lan_demo.provider_values(state, 'pilot')
    assert values['RAS_GENERATION__MAX_OUTPUT_TOKENS'] == '1024' and values['RAS_GENERATION__MAX_REQUEST_USD'] == '0.06'
    assert 'RAS_GENERATION__MAX_OUTPUT_TOKENS' not in m9_lan_demo.provider_values(state, 'drill')
    overlay = m9_lan_demo.override_yaml('pilot')
    assert '${RAS_GENERATION__MAX_OUTPUT_TOKENS:-512}' in overlay and '${RAS_GENERATION__MAX_REQUEST_USD:-0.05}' in overlay
