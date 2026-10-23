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


def test_locked_manifest_has_300_unique_open_licence_papers_outside_old_corpus():
    import re
    manifest = json.loads(papers.MANIFEST.read_text(encoding='utf-8'))['papers']
    old = json.loads(papers.policy.MANIFEST.read_text(encoding='utf-8'))['papers']
    assert len(manifest) == 300
    assert len({p['arxiv_id'] for p in manifest}) == 300
    assert len({p['sha256'] for p in manifest}) == 300
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
        assert papers.policy.normalise_licence(proof['oai']) == paper['licence_url']
        assert proof['latest_version_required'] is True


def test_fetch_recreates_attribution_in_a_fresh_destination(tmp_path, monkeypatch):
    manifest = tmp_path / 'manifest.json'
    dest = tmp_path / 'papers'
    papers.save(manifest, {'papers': [{'title': 'A retrieval paper', 'authors': ['Example Author'],
        'arxiv_id': '0000.00001', 'version': 1, 'licence': 'CC0 1.0',
        'licence_url': 'https://creativecommons.org/publicdomain/zero/1.0/', 'filename': 'paper.pdf'}]})
    calls = []
    monkeypatch.setattr(papers.policy, 'fetch', lambda d, m, recheck: calls.append((d, m, recheck)) or 0)
    monkeypatch.setattr(sys, 'argv', ['m11_papers.py', 'fetch', '--dest', str(dest), '--manifest', str(manifest)])
    assert papers.main() == 0
    assert calls == [(dest, manifest, True)]
    credit = (dest / 'ATTRIBUTION.md').read_text(encoding='utf-8')
    assert 'A retrieval paper' in credit and 'Example Author' in credit
    assert 'creativecommons.org/publicdomain/zero/1.0/' in credit
