"""Offline M11 safety checks: local binding/certificate, budget retention and API flow.

No demo, arXiv, provider or real credentials. Synthetic credentials stay in tmp.
"""
import json
import sys
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography import x509

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import m11_library as library
import m11_papers as papers
import m9_lan_demo as demo


def test_local_rebind_keeps_ca_provider_env_web_and_recreates_only_edge(tmp_path, monkeypatch):
    root = tmp_path / 'demo'
    monkeypatch.setattr(demo, 'restrict_to_user', lambda p: None)
    demo.make_demo_ca(root / 'ca', root / 'public', 'ras-demo.lan')
    ca_before = (root / 'ca/demo-ca.crt').read_bytes()
    state = {'root': str(root), 'host': 'ras-demo.lan', 'port': 8443,
             'lan_ip': '192.168.1.7', 'web_image': 'ras-pilot-web:ui2', 'provider': {'routes': ['embedding']}}
    demo.save_state(root, state)
    original = b'RAS_EDGE_BIND=192.168.1.7\nRAS_EMBEDDING__API_KEY=synthetic-value\nRAS_WEB_IMAGE=ras-pilot-web:ui2\n'
    demo.write(root / 'pilot/pilot.env', original)
    calls = []
    monkeypatch.setattr(demo, 'run', lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(demo, 'check_lan_ip', lambda ip: pytest.fail('local mode must not require Ethernet'))
    demo.rebind(Namespace(root=str(root), local=True, lan_ip=None, apply=True))
    saved = demo.load_state(root)
    assert saved['access_mode'] == 'local'
    assert saved['provider'] == state['provider'] and saved['web_image'] == state['web_image']
    assert demo.bind_address(saved) == '127.0.0.1'
    assert (root / 'pilot/pilot.env').read_bytes() == original.replace(b'192.168.1.7', b'127.0.0.1')
    assert (root / 'ca/demo-ca.crt').read_bytes() == ca_before
    cert = x509.load_pem_x509_certificate((root / 'tls.d/site.crt').read_bytes())
    ca = x509.load_pem_x509_certificate(ca_before)
    cert.verify_directly_issued_by(ca)
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ['ras-demo.lan']
    assert names.get_values_for_type(x509.IPAddress) == []
    assert len(calls) == 1
    assert calls[0][-8:] == ['up', '-d', '--wait', '--no-deps', '--pull', 'never', '--force-recreate', 'edge']


def test_rebind_refuses_ambiguous_env_before_changing_certificate_or_state(tmp_path, monkeypatch):
    demo.save_state(tmp_path, {'host': 'ras-demo.lan', 'lan_ip': '192.168.1.7'})
    demo.write(tmp_path / 'pilot/pilot.env', 'RAS_EDGE_BIND=a\nRAS_EDGE_BIND=b\n')
    monkeypatch.setattr(demo, 'make_site_cert', lambda *a: pytest.fail('must validate before writing'))
    with pytest.raises(SystemExit, match='exactly one'):
        demo.rebind(Namespace(root=str(tmp_path), local=True, lan_ip=None, apply=False))
    assert 'access_mode' not in demo.load_state(tmp_path)


def test_regenerating_provider_env_preserves_persisted_local_mode_and_web_image(tmp_path):
    demo.write(tmp_path / 'secrets/openrouter_api_key', 'synthetic-m11-key')
    state = {'root': str(tmp_path), 'host': 'ras-demo.lan', 'port': 8443,
             'lan_ip': '192.168.1.7', 'access_mode': 'local', 'subnet': '172.30.64.0/24',
             'web_image': 'ras-pilot-web:ui2', 'provider': {'routes': ['embedding']},
             'passwords': {name: 'synthetic-password' for name in ('pg', 'runtime', 'recovery', 'redis', 'minio')}}
    env = demo.pilot_env(state, 'pilot', 'test@example.invalid')
    assert env['RAS_EDGE_BIND'] == '127.0.0.1'
    assert env['RAS_WEB_IMAGE'] == 'ras-pilot-web:ui2'
    assert env['OPENROUTER_API_KEY'] == 'synthetic-m11-key'
    assert env['RAS_EMBEDDING__PRICING_VERIFIED'] == 'true'


def test_ledger_keeps_full_ceiling_after_operation_finishes_without_receipt(tmp_path):
    ledger = library.Ledger(tmp_path / 'cost')
    seq = ledger.admit('upload:x', Decimal('23.99'), 'test')
    ledger.append({'event': 'outcome', 'seq': seq, 'status': 'uncertain'})
    assert ledger.state()['committed_usd'] == Decimal('23.99')
    with pytest.raises(RuntimeError, match='80%'):
        ledger.admit('upload:y', Decimal('.02'), 'test')


@pytest.mark.parametrize('amount', [Decimal('NaN'), Decimal('Infinity'), Decimal('-1'), Decimal('0'), None])
def test_ledger_refuses_unknown_or_invalid_ceiling(tmp_path, amount):
    ledger = library.Ledger(tmp_path / 'cost')
    with pytest.raises(RuntimeError, match='unknown'):
        ledger.admit('upload:x', amount, 'test')
    assert ledger.records() == []


def test_product_client_uses_https_csrf_origin_and_never_retries_mutations(tmp_path, monkeypatch):
    demo.save_state(tmp_path, {'access_mode': 'local', 'host': 'ras-demo.lan', 'port': 8443})
    demo.write(tmp_path / 'secrets/library_user_email', 'test@example.invalid')
    demo.write(tmp_path / 'secrets/library_user_password', 'synthetic-password')
    calls = []
    def handle(req):
        calls.append(req)
        assert req.headers['origin'] == 'https://ras-demo.lan:8443'
        assert req.extensions['sni_hostname'] == 'ras-demo.lan'
        if req.url.path == '/api/auth/csrf':
            return httpx.Response(200, json={'status': 'ok'}, headers={'Set-Cookie': 'ras_csrf=synthetic; Path=/; Secure'})
        assert req.headers['X-CSRF-Token'] == 'synthetic'
        if req.url.path == '/api/auth/login':
            return httpx.Response(200, json={'user': {'role': 'user'}})
        return httpx.Response(503, json={'error': 'upload_outcome_uncertain'})
    real_client = httpx.Client
    monkeypatch.setattr(library.ssl, 'create_default_context', lambda **kw: True)
    monkeypatch.setattr(library.httpx, 'Client', lambda **kw: real_client(**{**kw, 'transport': httpx.MockTransport(handle)}))
    api = library.Api(tmp_path)
    with pytest.raises(RuntimeError, match='no retry'):
        api.request('PUT', '/uploads/original', content=b'%PDF-test')
    api.close()
    assert len(calls) == 3


def test_uncertain_title_update_is_checkpointed_and_blocks_automatic_replay(tmp_path, monkeypatch):
    import hashlib
    root = tmp_path / 'demo'
    evidence = root / 'evidence/m11'
    papers.save(evidence / 'library-before.json', [])
    papers.save(evidence / 'existing-sha256.json', {})
    content = b'%PDF-synthetic'
    demo.write(root / 'demo-papers/paper.pdf', content)
    manifest = tmp_path / 'manifest.json'
    topic = next(iter(papers.TOPICS))
    papers.save(manifest, {'papers': [{'tagged': '0000.00001v1', 'filename': 'paper.pdf',
        'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content), 'title': 'Test paper', 'topic': topic}]})
    calls = []
    class FakeApi:
        def __init__(self, root): pass
        def inventory(self): return []
        def close(self): pass
        def request(self, method, path, **kw):
            calls.append((method, path))
            if path == '/collections':
                return {'collections': [{'name': t, 'id': str(n)} for n, t in enumerate(papers.TOPICS)]}
            if path == '/uploads/notice': return {'version': 'test-notice'}
            if path == '/uploads/intents': return {'intent_token': 'synthetic-intent'}
            if path == '/uploads/original': return {'item_id': 'item', 'job_id': 'job', 'state': 'received'}
            if method == 'GET': return {'item': {'content_status': 'indexed', 'jobs': [
                {'id': 'job', 'state': 'ready', 'last_error_code': None, 'warnings': []}]}}
            if method == 'PATCH': raise RuntimeError('uncertain metadata response')
            pytest.fail('membership must not run after uncertain title')
    monkeypatch.setattr(library, 'Api', FakeApi)
    monkeypatch.setattr(library, 'config', lambda: {'embedding': {'model_id': 'openai/text-embedding-3-small'}})
    monkeypatch.setattr(library, 'preflight', lambda *a: {'ceiling_usd': '1'})
    with pytest.raises(RuntimeError, match='uncertain metadata'):
        library.run(root, manifest, False, 1)
    saved = json.loads((evidence / 'uploads.json').read_text())
    assert saved['0000.00001v1']['status'] == 'title_sending'
    assert not saved['0000.00001v1'].get('terminal')
    writes_before = [c for c in calls if c[0] != 'GET']
    assert writes_before == [('POST', '/uploads/intents'), ('PUT', '/uploads/original'), ('PATCH', '/library/items/item')]
    with pytest.raises(RuntimeError, match='uncertain prior upload'):
        library.run(root, manifest, False, 1)
    assert [c for c in calls if c[0] != 'GET'] == writes_before
    assert library.Ledger(evidence / 'cost').state()['committed_usd'] == Decimal('1')


def test_preflight_refuses_parser_drift_before_reading_or_uploading_file(tmp_path):
    with pytest.raises(RuntimeError, match='parser version mismatch'):
        library.preflight(tmp_path / 'absent.pdf', {'parser': 'unreviewed-parser'})


def test_fsynced_admission_with_lost_checkpoint_blocks_replay(tmp_path):
    ledger = library.Ledger(tmp_path / 'cost')
    seq = ledger.admit('upload:0000.00001v1', Decimal('1'), 'test')
    with pytest.raises(RuntimeError, match='unreconciled upload admission'):
        library.require_known_uploads(ledger, {})
    with pytest.raises(RuntimeError, match='unreconciled upload admission'):
        library.require_known_uploads(ledger, {'different-paper': {'seq': seq}})
    library.require_known_uploads(ledger, {'0000.00001v1': {'seq': seq, 'status': 'sending'}})
    assert ledger.state()['committed_usd'] == Decimal('1')


@pytest.mark.parametrize('unknown_membership', [False, True])
def test_topic_quarantine_keeps_item_and_original_and_blocks_unknown_replay(tmp_path, monkeypatch, unknown_membership):
    root = tmp_path / 'demo'
    evidence = root / 'evidence/m11'
    topic = next(iter(papers.TOPICS))
    papers.save(evidence / 'uploads.json', {'xv1': {'item_id': 'item', 'topic': topic, 'terminal': True}})
    manifest = tmp_path / 'manifest.json'
    papers.save(manifest.with_name('topic-exclusions.json'), [{'tagged': 'xv1', 'uploaded_item_id': 'item', 'reason': 'outside'}])
    monkeypatch.setattr(papers, 'MANIFEST', manifest)
    original = root / 'demo-papers/original.pdf'
    demo.write(original, b'%PDF-unmodified-original')
    calls, collections = [], {topic: 'old'}
    class FakeApi:
        def __init__(self, root): pass
        def close(self): pass
        def request(self, method, path, **kw):
            calls.append((method, path))
            if method == 'GET': return {'collections': [{'name': n, 'id': i} for n, i in collections.items()]}
            if path == '/collections':
                collections[kw['json']['name']] = 'review'
                return {'collection': {'id': 'review'}}
            if method == 'DELETE':
                assert path == '/collections/old/items/item'
                return None
            assert path == '/collections/review/items'
            if unknown_membership: raise RuntimeError('membership result uncertain')
            return {'item_id': 'item'}
    monkeypatch.setattr(library, 'Api', FakeApi)
    if unknown_membership:
        with pytest.raises(RuntimeError, match='membership result uncertain'):
            library.quarantine_topics(root)
        count = len(calls)
        with pytest.raises(RuntimeError, match='uncertain prior topic'):
            library.quarantine_topics(root)
        assert len(calls) == count
    else:
        library.quarantine_topics(root)
        assert json.loads((evidence / 'uploads.json').read_text())['xv1']['excluded_from_target'] == 'outside'
        count = len([c for c in calls if c[0] != 'GET'])
        library.quarantine_topics(root)
        assert len([c for c in calls if c[0] != 'GET']) == count
    assert original.read_bytes() == b'%PDF-unmodified-original'
    assert all(path.startswith('/collections') for _, path in calls)


@pytest.mark.parametrize('title,abstract,topic', [
    ('Dense retrieval', 'text embeddings for language models', 'Dense retrieval & embeddings'),
    ('Reranking', 'retrieval for language models', 'Reranking & search'),
    ('Cancer biomarkers', 'a clinical investigation', None),
    ('Average scheduling results', 'We evaluate average scheduling performance and benchmarks.', None),
    ('Paragraph grounding', 'An average benchmark of firmware rehosting fidelity.', None),
    ('ALEE', 'We evaluate multilingual sentence embeddings on minimal pairs.', 'Dense retrieval & embeddings'),
])
def test_topic_filter_requires_llm_or_retrieval_context(title, abstract, topic):
    assert papers.topic_for(title, abstract) == topic


def test_manifest_has_requested_unique_open_licence_papers_outside_old_corpus():
    import re
    data = json.loads(papers.MANIFEST.read_text(encoding='utf-8'))
    manifest = data['papers']
    old = json.loads(papers.policy.MANIFEST.read_text(encoding='utf-8'))['papers']
    assert len(manifest) == data['target_new_papers'] >= 300
    assert len({p['arxiv_id'] for p in manifest}) == len(manifest)
    assert len({p['sha256'] for p in manifest}) == len(manifest)
    assert not ({p['arxiv_id'] for p in manifest} & {p['arxiv_id'] for p in old})
    assert not ({p['sha256'] for p in manifest} & {p['sha256'] for p in old})
    for paper in manifest:
        assert paper['tagged'] == f"{paper['arxiv_id']}v{paper['version']}"
        assert paper['filename'] == f"arxiv-{paper['tagged']}.pdf"
        assert paper['title'].strip() and paper['authors'] and all(a.strip() for a in paper['authors'])
        assert 0 < paper['bytes'] <= 30 * 1024 * 1024
        assert re.fullmatch('[0-9a-f]{64}', paper['sha256'])
        assert paper['licence_url'] in papers.policy.ACCEPTED
        assert paper['licence'] == papers.policy.ACCEPTED[paper['licence_url']]
        proof = paper['licence_evidence']
        assert papers.policy.normalise_licence(proof['abs']) == paper['licence_url']
        assert proof['latest_version_required'] is True
        if proof.get('method', 'oai_latest') == 'oai_latest':
            assert papers.policy.normalise_licence(proof['oai']) == paper['licence_url']
        else:
            assert proof['method'] == 'atom_and_live_abs_latest'
            assert 'oai' not in proof and 'oai' not in paper
            assert proof['atom_entry_id'] == paper['atom']['entry_id']
            assert proof['atom_entry_id'].endswith('/' + paper['tagged'])
            assert proof['latest_version'] == paper['version']
            assert papers.policy.normalise_licence(proof['latest_abs']) == paper['licence_url']
            for name in ('latest', 'exact'):
                assert re.fullmatch('[0-9a-f]{64}', proof[name + '_sha256'])
                assert proof[name + '_cache'].endswith('.html')


def test_fetch_recreates_attribution_in_a_fresh_destination(tmp_path, monkeypatch):
    manifest = tmp_path / 'manifest.json'
    dest = tmp_path / 'papers'
    papers.save(manifest, {'papers': [{'title': 'A retrieval paper', 'authors': ['Example Author'],
        'arxiv_id': '0000.00001', 'version': 1, 'licence': 'CC0 1.0',
        'licence_url': 'https://creativecommons.org/publicdomain/zero/1.0/', 'filename': 'paper.pdf'}]})
    calls = []
    monkeypatch.setattr(papers, 'fetch_locked', lambda d, m: calls.append((d, m)) or 0)
    monkeypatch.setattr(sys, 'argv', ['m11_papers.py', 'fetch', '--dest', str(dest), '--manifest', str(manifest)])
    assert papers.main() == 0
    assert calls == [(dest, manifest)]
    credit = (dest / 'ATTRIBUTION.md').read_text(encoding='utf-8')
    assert 'A retrieval paper' in credit and 'Example Author' in credit
    assert 'creativecommons.org/publicdomain/zero/1.0/' in credit


def test_inventory_counts_failed_job_derivatives_separately_from_searchable_chunks(tmp_path):
    import m11_demo
    papers.save(tmp_path / 'library-before.json', [])
    items = [
        {'id': 'a', 'title': 'Indexed paper', 'content_status': 'indexed', 'jobs': [],
         'files': [{'page_count': 1, 'byte_size': 100}]},
        {'id': 'b', 'title': 'Partial failed paper', 'content_status': 'metadata_only',
         'jobs': [{'state': 'terminal_failed', 'last_error_code': 'provider_outcome_uncertain'}],
         'files': [{'page_count': 2, 'byte_size': 200}]},
    ]
    class FakeApi:
        def request(self, method, path, **kw):
            assert method == 'GET' and path.endswith('/chunks')
            return {'chunks': [{'section_kind': 'body'}, {'section_kind': 'references' if '/a/' in path else 'abstract'}]}
    result = m11_demo.inventory(FakeApi(), items, {}, {}, tmp_path)
    assert result['derived_chunks'] == 4
    assert result['indexed_chunks'] == 1
    assert result['pages'] == 3 and result['original_bytes'] == 300


def test_larger_corpus_continues_past_original_650_candidate_stop_without_yesterdays_token(tmp_path, monkeypatch):
    monkeypatch.setattr(papers, 'stamp', lambda: '2026-10-24T00:00:00+00:00')
    cache = tmp_path / 'oai-2026-10-24'
    cache.mkdir()
    (tmp_path / 'oai-batch-000.xml').write_text('old stream must not be reused')
    header = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" xmlns:r="http://arxiv.org/OAI/arXivRaw/"><ListRecords>'
    def record(number):
        return ('<record><metadata><r:arXivRaw><r:id>2609.' + str(number).zfill(5) + '</r:id>'
            '<r:title>Dense retrieval</r:title><r:abstract>Text embeddings for retrieval.</r:abstract>'
            '<r:license>http://creativecommons.org/licenses/by/4.0/</r:license>'
            '<r:authors>Example Author</r:authors><r:version version="v1"/>'
            '</r:arXivRaw></metadata></record>')
    (cache / 'oai-batch-000.xml').write_text(header + ''.join(record(n) for n in range(650))
        + '<resumptionToken>next</resumptionToken></ListRecords></OAI-PMH>', encoding='utf-8')
    (cache / 'oai-batch-001.xml').write_text(header + record(650) + '</ListRecords></OAI-PMH>', encoding='utf-8')
    class CachedArxiv:
        evidence = tmp_path
        def get(self, *args, **kwargs):
            pytest.fail('cached OAI must not be fetched again')
    assert len(papers.bulk_candidates(CachedArxiv(), minimum=651)) == 651


def test_expansion_preserves_selected_papers_and_records_new_target(tmp_path, monkeypatch):
    dest, manifest = tmp_path / 'pdfs', tmp_path / 'manifest.json'
    previous = {'arxiv_id': '2609.99999', 'sha256': 'a' * 64, 'title': 'Previously selected paper',
                'authors': ['Example Author'], 'version': 1, 'filename': 'old.pdf',
                'licence': 'CC BY 4.0', 'licence_url': 'https://creativecommons.org/licenses/by/4.0/'}
    papers.save(manifest, {'task': 'M11', 'target_new_papers': 300, 'papers': [previous]})
    class FakeArxiv:
        def __init__(self, evidence):
            self.evidence = evidence
        def close(self):
            pass
    seen = []
    monkeypatch.setattr(papers, 'Arxiv', FakeArxiv)
    monkeypatch.setattr(papers, 'bulk_candidates', lambda arxiv, minimum: seen.append(minimum) or [])
    assert papers.discover(dest, manifest, 1300) == 2
    after = json.loads(manifest.read_text())
    assert after['target_new_papers'] == 1300 and after['papers'] == [previous]
    assert seen == [2600]
    with pytest.raises(RuntimeError, match='discard'):
        papers.discover(dest, manifest, 0)


def test_post_ingest_smoke_waits_for_expanded_target_not_just_first_300(tmp_path, monkeypatch):
    import m11_demo
    manifest = tmp_path / 'manifest.json'
    partial = [{'tagged': str(n)} for n in range(300)]
    papers.save(manifest, {'target_new_papers': 1300, 'papers': partial})
    papers.save(tmp_path / 'evidence/m11/uploads.json', {p['tagged']: {'terminal': True} for p in partial})
    monkeypatch.setattr(papers, 'MANIFEST', manifest)
    monkeypatch.setattr(library, 'Api', lambda *a: pytest.fail('no API/provider calls before the full target'))
    with pytest.raises(RuntimeError, match='corpus not fully processed'):
        m11_demo.run(tmp_path)


def test_cached_oai_candidates_still_reject_exact_version_abs_licence_mismatch(tmp_path, monkeypatch):
    dest, manifest = tmp_path / 'pdfs', tmp_path / 'manifest.json'
    papers.save(dest / 'm11-evidence/bulk-candidates.json', [{'tagged': '2609.99999v1',
        'title': 'Dense retrieval', 'topic': 'Dense retrieval & embeddings',
        'oai': {'licence': 'http://creativecommons.org/licenses/by/4.0/', 'versions': [1]}}])
    class FakeArxiv:
        def __init__(self, evidence):
            self.evidence = evidence
        def get(self, *args, **kwargs):
            pytest.fail('licence mismatch must not fetch a PDF')
        def close(self):
            pass
    checked = []
    def incompatible_abs(arxiv, aid, version):
        checked.append((aid, version))
        return 'http://creativecommons.org/licenses/by-nc/4.0/'
    monkeypatch.setattr(papers, 'Arxiv', FakeArxiv)
    monkeypatch.setattr(papers, 'bulk_candidates', lambda *a, **k: pytest.fail('use explicit snapshot only'))
    monkeypatch.setattr(papers.policy, 'abs_licence', incompatible_abs)
    assert papers.discover(dest, manifest, 1, cached_candidates=True) == 2
    assert checked == [('2609.99999', 1)]
    assert json.loads(manifest.read_text())['papers'] == []
    assert not json.loads((dest / 'm11-evidence/decisions.json').read_text())['2609.99999v1']['accepted']


@pytest.mark.parametrize('reason', ['file_too_large', 'duplicate_sha256'])
def test_download_exclusions_keep_credit_and_exact_licence_evidence(tmp_path, monkeypatch, reason):
    import hashlib
    dest, manifest, old_manifest = tmp_path / 'pdfs', tmp_path / 'manifest.json', tmp_path / 'old.json'
    content = b'%PDF-test' + (b'0' * (30 * 1024 * 1024) if reason == 'file_too_large' else b'')
    digest = hashlib.sha256(content).hexdigest()
    papers.save(old_manifest, {'papers': [{'arxiv_id': '2609.88888', 'sha256': digest if reason == 'duplicate_sha256' else 'b' * 64}]})
    papers.save(dest / 'm11-evidence/bulk-candidates.json', [{'tagged': '2609.99999v1',
        'title': 'Excluded retrieval paper', 'authors': ['Example Author'], 'topic': 'Dense retrieval & embeddings',
        'oai': {'licence': 'http://creativecommons.org/licenses/by/4.0/', 'versions': [1]}}])
    (dest / 'arxiv-2609.99999v1.pdf').write_bytes(content)
    class FakeArxiv:
        def __init__(self, evidence):
            self.evidence = evidence
        def get(self, *args, **kwargs):
            pytest.fail('existing PDF must not be fetched again')
        def close(self):
            pass
    monkeypatch.setattr(papers, 'Arxiv', FakeArxiv)
    monkeypatch.setattr(papers.policy, 'MANIFEST', old_manifest)
    monkeypatch.setattr(papers.policy, 'abs_licence', lambda *a: 'http://creativecommons.org/licenses/by/4.0/')
    assert papers.discover(dest, manifest, 1, cached_candidates=True) == 2
    assert json.loads(manifest.read_text())['papers'] == []
    excluded = json.loads(manifest.with_name('download-exclusions.json').read_text())[0]
    assert excluded['reason'] == reason and excluded['sha256'] == digest
    assert excluded['licence_evidence']['abs'] == 'http://creativecommons.org/licenses/by/4.0/'
    assert papers.policy.normalise_licence(excluded['licence_evidence']['abs']) == excluded['licence_url']
    credit = (dest / 'ATTRIBUTION.md').read_text(encoding='utf-8')
    assert 'Excluded retrieval paper' in credit and 'Example Author' in credit


def atom_abs_html(aid='2609.99999', selected=1, latest=1,
                  licence='http://creativecommons.org/licenses/by/4.0/', history=True):
    versions = ''.join(f'<strong>[v{v}]</strong>' if v == selected else f'<a>[v{v}]</a>'
                       for v in range(1, latest + 1))
    return (f'<meta name="citation_arxiv_id" content="{aid}">'
            + (f'<div class="submission-history">{versions}</div>' if history else '')
            + f'<div class="abs-license"><a href="{licence}">Licence</a></div>')


@pytest.mark.parametrize('case,accepted,request_count', [
    ('valid', True, 2), ('newer_version', False, 1), ('noncommercial', False, 1),
    ('licence_disagreement', False, 2), ('old_exact_page_with_newer_licence', False, 2),
    ('wrong_paper', False, 1), ('missing_history', False, 1), ('wrong_atom_version', False, 0),
    ('http_failure', False, 1),
])
def test_atom_licence_gate_binds_current_version_and_both_abs_pages(tmp_path, case, accepted, request_count):
    import m11_atom
    latest, exact = {}, {}
    if case == 'newer_version':
        latest.update(selected=2, latest=2)
    if case == 'noncommercial':
        latest['licence'] = 'http://creativecommons.org/licenses/by-nc/4.0/'
    if case == 'licence_disagreement':
        exact['licence'] = 'http://creativecommons.org/licenses/by-sa/4.0/'
    if case == 'old_exact_page_with_newer_licence':
        exact.update(selected=1, latest=2)
    if case == 'wrong_paper':
        latest['aid'] = '2609.88888'
    if case == 'missing_history':
        latest['history'] = False
    responses = [atom_abs_html(**latest), atom_abs_html(**exact)]
    calls = []
    class FakeArxiv:
        evidence = tmp_path
        def get(self, url):
            calls.append(url)
            return httpx.Response(500 if case == 'http_failure' else 200,
                                  text=responses[len(calls) - 1], request=httpx.Request('GET', url))
    row = {'tagged': '2609.99999v1', 'title': 'Grounded LLM',
           'atom': {'entry_id': 'http://arxiv.org/abs/2609.99999v' + ('2' if case == 'wrong_atom_version' else '1')}}
    result = m11_atom.verify(FakeArxiv(), row)
    assert result['accepted'] is accepted and len(calls) == request_count
    if accepted:
        assert result['licence_evidence']['method'] == 'atom_and_live_abs_latest'
        assert result['licence_evidence']['latest_version'] == 1
        assert 'oai' not in result['licence_evidence']
        assert len(list(tmp_path.rglob('*.html'))) == 2
    else:
        assert result['reasons'] and not list(tmp_path.rglob('*.html'))


def test_fetch_can_recheck_original_oai_manifest_without_fabricating_atom_evidence(tmp_path):
    import m11_atom
    calls = []
    class FakeArxiv:
        evidence = tmp_path
        def get(self, url):
            calls.append(url)
            return httpx.Response(200, text=atom_abs_html(), request=httpx.Request('GET', url))
    row = {'tagged': '2609.99999v1', 'title': 'Grounded LLM', 'oai': {'versions': [1]}}
    assert not m11_atom.verify(FakeArxiv(), row)['accepted']
    assert not calls
    result = m11_atom.verify(FakeArxiv(), row, allow_original_oai=True)
    assert result['accepted'] and len(calls) == 2
    assert result['licence_evidence']['method'] == 'live_abs_recheck_original_oai'
    assert 'atom_entry_id' not in result['licence_evidence']


@pytest.mark.parametrize('case', ['fresh_valid', 'fresh_corrupt', 'existing_corrupt'])
def test_fetch_locked_checks_bytes_before_publish_and_preserves_existing_file(tmp_path, monkeypatch, case):
    import hashlib
    import m11_atom
    dest, manifest = tmp_path / 'pdfs', tmp_path / 'manifest.json'
    expected, corrupt = b'%PDF-correct', b'%PDF-corrupt'
    paper = {'arxiv_id': '2609.99999', 'version': 1, 'filename': 'arxiv-2609.99999v1.pdf',
             'title': 'Grounded LLM', 'bytes': len(expected), 'sha256': hashlib.sha256(expected).hexdigest(),
             'licence_url': 'https://creativecommons.org/licenses/by/4.0/'}
    papers.save(manifest, {'papers': [paper]})
    dest.mkdir()
    path = dest / paper['filename']
    if case == 'existing_corrupt':
        path.write_bytes(corrupt)
    class FakeArxiv:
        def __init__(self, evidence):
            self.evidence = evidence
        def get(self, url):
            assert case != 'existing_corrupt'
            return httpx.Response(200, content=expected if case == 'fresh_valid' else corrupt)
        def close(self):
            pass
    monkeypatch.setattr(papers, 'Arxiv', FakeArxiv)
    monkeypatch.setattr(m11_atom, 'verify', lambda *a, **kw: {'accepted': True, 'licence_url': paper['licence_url']})
    assert papers.fetch_locked(dest, manifest) == (0 if case == 'fresh_valid' else 1)
    if case == 'fresh_valid':
        assert path.read_bytes() == expected
    elif case == 'fresh_corrupt':
        assert not path.exists()
    else:
        assert path.read_bytes() == corrupt


def test_atom_candidates_prefer_latest_metadata_and_keep_source_hash(tmp_path):
    import hashlib
    import m11_atom
    def entry(version):
        return (f'<entry><id>http://arxiv.org/abs/2609.99999v{version}</id><title>Grounded LLM</title>'
                '<summary>LLM hallucination with grounding.</summary><author><name>Example Author</name></author></entry>')
    raw = ('<feed xmlns="http://www.w3.org/2005/Atom">' + entry(1) + entry(2) + '</feed>').encode()
    class FakeArxiv:
        evidence = tmp_path
        def get(self, url, **params):
            assert url == 'https://export.arxiv.org/api/query' and params['max_results'] == 150
            return httpx.Response(200, content=raw, request=httpx.Request('GET', url, params=params))
    rows = m11_atom.candidates(FakeArxiv(), 1300, {'Grounding & hallucination': 'abs:hallucination'},
                               papers.topic_for, papers.save, pages=1)
    assert len(rows) == 1 and rows[0]['tagged'] == '2609.99999v2'
    assert rows[0]['authors'] == ['Example Author']
    assert rows[0]['atom']['batch_sha256'] == hashlib.sha256(raw).hexdigest()


def test_atom_reuses_retained_primary_xml_with_truthful_original_cache_path(tmp_path):
    import m11_atom
    raw = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
           '<id>http://arxiv.org/abs/2609.99999v1</id><title>Grounded LLM</title>'
           '<summary>LLM hallucination with grounding.</summary>'
           '<author><name>Example Author</name></author></entry></feed>').encode()
    for order in ('relevance', 'submittedDate'):
        (tmp_path / f'query-0-{order}.xml').write_bytes(raw)
    class FakeArxiv:
        evidence = tmp_path
        def get(self, *a, **kw):
            pytest.fail('reuse retained metadata, live licence gate remains separate')
    rows = m11_atom.candidates(FakeArxiv(), 1300, {'Grounding & hallucination': 'abs:hallucination'},
                               papers.topic_for, papers.save, pages=1)
    assert rows[0]['atom']['batch'] == 'query-0-relevance.xml'
    assert 'licence' not in rows[0]


@pytest.mark.parametrize('failure', ['timeout', 'invalid_xml', 'atom_error'])
def test_atom_failed_query_retains_other_metadata_without_accepting_failed_page(tmp_path, failure):
    import m11_atom
    calls = []
    raw = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
           '<id>http://arxiv.org/abs/2609.99999v1</id><title>Grounded LLM</title>'
           '<summary>LLM hallucination with grounding.</summary>'
           '<author><name>Example Author</name></author></entry></feed>').encode()
    class FakeArxiv:
        evidence = tmp_path
        def get(self, url, **params):
            calls.append(params)
            assert params['attempts'] == 1
            if len(calls) == 1:
                if failure == 'timeout':
                    raise httpx.ReadTimeout('synthetic timeout')
                if failure == 'atom_error':
                    return httpx.Response(200, content=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#test</id></entry></feed>', request=httpx.Request('GET', url))
                return httpx.Response(200, content=b'not XML', request=httpx.Request('GET', url))
            return httpx.Response(200, content=raw, request=httpx.Request('GET', url))
    rows = m11_atom.candidates(FakeArxiv(), 1300, {'Grounding & hallucination': 'abs:hallucination'},
                               papers.topic_for, papers.save, pages=4)
    assert len(calls) == 2 and len(rows) == 1
    assert len(list(tmp_path.rglob('*.failure.json'))) == 1
    assert len(list(tmp_path.rglob('*.xml'))) == 1
    assert json.loads((tmp_path / 'atom-candidates.json').read_text())[0]['tagged'] == '2609.99999v1'


def test_atom_unsupported_legacy_id_cannot_abort_other_candidates_or_infer_a_licence(tmp_path):
    import m11_atom
    raw = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/cs/0111012v1</id></entry>'
           '<entry><id>http://arxiv.org/abs/2609.99999v1</id><title>Grounded LLM</title>'
           '<summary>LLM hallucination with grounding.</summary>'
           '<author><name>Example Author</name></author></entry></feed>').encode()
    class FakeArxiv:
        evidence = tmp_path
        def get(self, url, **params):
            return httpx.Response(200, content=raw, request=httpx.Request('GET', url))
    rows = m11_atom.candidates(FakeArxiv(), 1300, {'Grounding & hallucination': 'abs:hallucination'},
                               papers.topic_for, papers.save, pages=1)
    assert len(rows) == 1 and rows[0]['tagged'] == '2609.99999v1'
    assert 'licence' not in rows[0] and len(list(tmp_path.rglob('*.unsupported-id.json'))) == 2
