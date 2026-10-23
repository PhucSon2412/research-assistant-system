"""M11 product-API ingestion; explicit batch consent, bounded spend, resumable journal.

run --watch: one authenticated session, at most two outstanding jobs, no POST retries.
inspect: read library inventory. PDF bytes and all raw evidence stay outside Git.
Only read-only Settings and OpenRouter /key probes use docker exec; all library
reads/writes use the real HTTPS product API. No SQL or internal upload endpoints.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

import m11_papers
import m7a_live
import m9_lan_demo

REPO = Path(__file__).resolve().parents[1]
STOP_USD = Decimal('24')  # owner's $30 ceiling, stop at 80%; no inherited M7c budget
TERMINAL = {'ready', 'needs_review', 'fulltext_rejected', 'terminal_failed', 'cancelled', 'duplicate'}


def now():
    return m11_papers.stamp()


def docker_read(code):
    result = subprocess.run(['docker', 'exec', 'ras-pilot-api-1', 'python', '-c', code],
                            capture_output=True, timeout=90, env=m9_lan_demo.clean_env())
    if result.returncode:
        raise RuntimeError('demo read-only probe failed; no credentials printed')
    return json.loads(result.stdout)


def config():
    return docker_read("import json; from app.config import get_settings; from app.ingest.pdf import PIPELINE_VERSION; s=get_settings(); "
        "d={k:getattr(s,k).model_dump(mode='json',exclude={'api_key'}) "
        "for k in ('files','pdf_body','ingest','embedding','budget','search','rerank','generation')}; "
        "d['parser']=PIPELINE_VERSION; print(json.dumps(d))")


def shared_meter():
    # The key remains inside the existing demo environment. Only numeric usage leaves
    # the container. A group key's delta is not attributable to this task alone.
    return docker_read("import json,httpx; from app.config import get_settings; "
        "k=get_settings().embedding.api_key.get_secret_value(); "
        "r=httpx.get('https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+k},timeout=25); "
        "r.raise_for_status(); d=r.json()['data']; "
        "print(json.dumps({n:d.get(n) for n in ('usage','limit','limit_remaining','usage_daily')}))")


class Ledger(m7a_live.Ledger):
    """Conservative operation reservations, never released from missing receipts.

    One upload reserves ALL possible chunk calls, including four worker attempts.
    Without per-call receipts the entire ceiling remains committed after completion.
    The provider's shared-key delta is recorded separately, never called task billing.
    """
    def admit(self, route, ceiling, source):
        if not isinstance(ceiling, Decimal) or not ceiling.is_finite() or ceiling <= 0:
            raise RuntimeError('unknown operation ceiling; not sent')
        state = self.state()
        if state['committed_usd'] + ceiling > STOP_USD:
            raise RuntimeError('M11 stop at 80% ($24); not sent')
        seq = state['slots'] + 1
        self.append({'event': 'sending', 'seq': seq, 'route': route, 'ceiling_usd': str(ceiling),
                     'ceiling_source': source, 'at': now()})
        return seq


class Api:
    def __init__(self, root):
        state = m9_lan_demo.load_state(root)
        if state.get('access_mode') != 'local' or state['host'] != 'ras-demo.lan' or state['port'] != 8443:
            raise RuntimeError('expected M11 local demo only')
        self.root = root
        context = ssl.create_default_context(cafile=str(root / 'public/ras-demo-ca.crt'))
        self.client = httpx.Client(base_url='https://127.0.0.1:8443/api', verify=context,
            trust_env=False, timeout=150, headers={'Host': 'ras-demo.lan:8443',
            'Origin': 'https://ras-demo.lan:8443', 'User-Agent': 'ras-m11-library/1.0'})
        self.request('GET', '/auth/csrf')
        email = (root / 'secrets/library_user_email').read_text(encoding='utf-8-sig').strip()
        password = (root / 'secrets/library_user_password').read_text(encoding='utf-8-sig').rstrip('\r\n')
        login = self.request('POST', '/auth/login', json={'email': email, 'password': password})
        if login['user']['role'] != 'user':
            raise RuntimeError('upload account must be the existing demo user')

    def request(self, method, path, **kwargs):
        headers = kwargs.pop('headers', {})
        if method not in ('GET', 'HEAD'):
            headers['X-CSRF-Token'] = self.client.cookies.get('ras_csrf', '')
        response = self.client.request(method, path, headers=headers,
            extensions={'sni_hostname': 'ras-demo.lan'}, **kwargs)
        if response.status_code >= 400:
            try:
                code = response.json().get('error', 'http_error')
            except ValueError:
                code = 'non_json_error'
            raise RuntimeError(f'product API {method} {path}: {response.status_code} {code}; no retry')
        if response.status_code == 204:
            return None
        return response.json()

    def inventory(self):
        rows = []
        offset = 0
        while True:
            page = self.request('GET', '/library/items', params={'scope': 'mine', 'limit': 100, 'offset': offset})
            rows.extend(page['items'])
            offset += len(page['items'])
            if offset >= page['total']:
                break
        return [self.request('GET', '/library/items/' + row['id'])['item'] for row in rows]

    def close(self):
        self.client.close()


def preflight(path, settings):
    """Same pinned parser/chunker as rc2; only byte/count metadata is persisted.

    UTF-8 bytes bound token count. Multiplying by ingest.max_attempts covers
    all automatic attempts; unique chunk reuse can only reduce expenditure.
    """
    from app.config import Settings
    from app.ingest import pdf
    from app.ingest.markdown import chunk_section
    from pypdf import PdfReader
    if pdf.PIPELINE_VERSION != settings['parser']:
        raise RuntimeError('preflight/runtime parser version mismatch; not sent')
    cfg = Settings(_env_file=None, _env_prefix='M11_UNUSED_', db=None,
        database_url='postgresql+psycopg://unused@localhost/unused',
        **{k: settings[k] for k in ('files', 'ingest', 'pdf_body')})
    data = path.read_bytes()
    if len(data) > cfg.files.pdf_max_bytes:
        return {'skip': 'file_too_large'}
    pages = len(PdfReader(io.BytesIO(data), strict=True).pages)
    if pages > cfg.files.pdf_max_pages:
        return {'skip': 'too_many_pages', 'pages': pages}
    try:
        result = pdf.classify(pdf.parse(data, pages, cfg), cfg)
    except pdf.PdfError as exc:
        if not exc.retryable and exc.code in ('pdf_text_invalid', 'pdf_invalid', 'pdf_page_mismatch'):
            return {'pages': pages, 'parser': pdf.PIPELINE_VERSION, 'predicted_parser_error': exc.code,
                    'eligible_chunks': 0, 'input_bytes_bound': 0, 'ceiling_usd': '0.000001'}
        # Parser failures still go through the product flow and retain its actual state.
        # Reserve a whole parser output bound if the preflight cannot establish text.
        # Such a file cannot fit the task limit, so stop for explicit review, never guess.
        return {'skip': 'preflight_' + exc.code, 'pages': pages}
    byte_count, chunks = 0, 0
    for segment in result.segments:
        if segment.kind not in ('abstract', 'body'):
            continue
        section = SimpleNamespace(kind=segment.kind, heading=None, start_offset=segment.start, end_offset=segment.end)
        for chunk in chunk_section(result.pages[segment.page - 1].text, section, cfg.ingest.markdown_max_chunk_chars):
            byte_count += len(chunk.text.encode('utf-8'))
            chunks += 1
    rate = Decimal(str(settings['embedding']['max_usd_per_million_tokens']))
    ceiling = max(Decimal('0.000001'), Decimal(byte_count) * cfg.ingest.max_attempts * rate / Decimal(1_000_000))
    return {'pages': pages, 'parser': pdf.PIPELINE_VERSION, 'predicted_disposition': result.disposition,
            'eligible_chunks': chunks, 'input_bytes_bound': byte_count, 'ceiling_usd': str(ceiling)}


def run(root, manifest, watch, target):
    dest = root / 'demo-papers'
    evidence = root / 'evidence/m11'
    evidence.mkdir(parents=True, exist_ok=True)
    checkpoint = evidence / 'uploads.json'
    rows = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
    settings = config()
    m11_papers.save(evidence / 'settings.json', settings)
    if settings['embedding']['model_id'] != 'openai/text-embedding-3-small':
        raise RuntimeError('unexpected embedding configuration; not sent')
    api = Api(root)
    ledger = Ledger(evidence / 'cost')
    with ledger.exclusive():
        try:
            inventory = api.inventory()
            baseline_path = evidence / 'library-before.json'
            if not baseline_path.exists():
                if len(inventory) != 12:
                    raise RuntimeError('expected the existing 12-paper library; not sent')
                m11_papers.save(baseline_path, inventory)
                old_hashes = {}
                for item in inventory:
                    for file in item['files']:
                        response = api.client.get(file['original_url'],
                            extensions={'sni_hostname': 'ras-demo.lan'})
                        response.raise_for_status()
                        old_hashes[file['filename']] = hashlib.sha256(response.content).hexdigest()
                m11_papers.save(evidence / 'existing-sha256.json', old_hashes)
                m11_papers.save(evidence / 'shared-key-before.json', shared_meter())
            old_hashes = set(json.loads((evidence / 'existing-sha256.json').read_text()).values())
            collections = {c['name']: c['id'] for c in api.request('GET', '/collections')['collections']}
            for name in m11_papers.TOPICS:
                if name not in collections:
                    collections[name] = api.request('POST', '/collections', json={'name': name})['collection']['id']
            m11_papers.save(evidence / 'collections.json', collections)
            while True:
                papers = json.loads(manifest.read_text(encoding='utf-8'))['papers']
                # Resume only acknowledged uploads. A sending record without IDs is
                # an uncertain mutation and blocks new uploads until reconciled by owner.
                if any(row.get('status') in ('sending', 'title_sending', 'membership_sending') for row in rows.values()):
                    raise RuntimeError('uncertain prior upload; inspect library, never auto-resend')
                active = []
                for tagged, row in rows.items():
                    if 'item_id' not in row or row.get('terminal'):
                        continue
                    item = api.request('GET', '/library/items/' + row['item_id'])['item']
                    job = next(j for j in item['jobs'] if j['id'] == row['job_id'])
                    row.update({'state': job['state'], 'content_status': item['content_status'],
                                'error': job['last_error_code'], 'warnings': job['warnings']})
                    if job['state'] in TERMINAL:
                        row['finished_at'] = now()
                        row['status'] = 'title_sending'
                        m11_papers.save(checkpoint, rows)
                        api.request('PATCH', '/library/items/' + row['item_id'], json={'title': row['title']})
                        row['status'] = 'membership_sending'
                        m11_papers.save(checkpoint, rows)
                        api.request('POST', '/collections/' + collections[row['topic']] + '/items',
                                    json={'item_id': row['item_id']})
                        row['terminal'] = True
                        row['status'] = 'accepted'
                        m11_papers.save(checkpoint, rows)
                        ledger.append({'event': 'outcome', 'seq': row['seq'], 'status': 'uncertain',
                            'at': now(), 'note': 'operation complete; full ceiling retained without per-call receipts'})
                        print(json.dumps({'terminal': tagged, 'state': row['state'],
                                          'completed': sum(bool(r.get('terminal')) for r in rows.values())}), flush=True)
                    else:
                        active.append(tagged)
                m11_papers.save(checkpoint, rows)
                pending = [p for p in papers if p['tagged'] not in rows]
                if pending and len(active) < 2:
                    paper = pending[0]
                    tagged = paper['tagged']
                    path = dest / paper['filename']
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != paper['sha256'] or len(content) != paper['bytes']:
                        raise RuntimeError('manifest/PDF integrity mismatch; not sent')
                    if paper['sha256'] in old_hashes:
                        rows[tagged] = {'status': 'skipped', 'reason': 'existing_sha256'}
                        m11_papers.save(checkpoint, rows)
                        continue
                    bound = preflight(path, settings)
                    if 'skip' in bound:
                        rows[tagged] = {'status': 'skipped', 'reason': bound['skip'], 'preflight': bound}
                        m11_papers.save(checkpoint, rows)
                        continue
                    seq = ledger.admit('upload:' + tagged, Decimal(bound['ceiling_usd']),
                        'all body/abstract UTF8 bytes x max_attempts x configured provider price cap')
                    # Durable sending state precedes notice/intent/PUT. Consent is the
                    # owner's explicit batch approval recorded in task M11, per-file ack stays intact.
                    rows[tagged] = {'status': 'sending', 'seq': seq, 'title': paper['title'],
                                    'topic': paper['topic'], 'preflight': bound, 'started_at': now()}
                    m11_papers.save(checkpoint, rows)
                    notice = api.request('GET', '/uploads/notice')
                    intent = api.request('POST', '/uploads/intents', json={'acknowledged': True,
                        'notice_version': notice['version'], 'file_kind': 'pdf', 'filename': paper['filename'],
                        'byte_size': paper['bytes'], 'sha256': paper['sha256']})
                    receipt = api.request('PUT', '/uploads/original', content=content,
                        headers={'Content-Type': 'application/pdf', 'X-Upload-Intent': intent['intent_token']})
                    rows[tagged].update({**receipt, 'status': 'accepted', 'notice_version': notice['version']})
                    m11_papers.save(checkpoint, rows)
                    print(json.dumps({'uploaded': tagged, 'accepted': sum(r.get('status') == 'accepted' for r in rows.values()),
                                      'committed_usd': str(ledger.state()['committed_usd'])}), flush=True)
                    continue
                if len(papers) >= target and not pending and not active:
                    break
                if not watch and not pending and not active:
                    break
                time.sleep(5)
            m11_papers.save(evidence / 'library-after.json', api.inventory())
            m11_papers.save(evidence / 'shared-key-after.json', shared_meter())
            print(json.dumps({'done': True, 'committed_usd': str(ledger.state()['committed_usd'])}), flush=True)
        finally:
            api.close()


def quarantine_topics(root):
    """Move already accepted topic exclusions to an eighth review collection.

    Keeps every item/original/job/cost. Only collection memberships change.
    A durable sending marker blocks an automatic replay after an unknown result.
    """
    evidence = root / 'evidence/m11'
    path = evidence / 'topic-reclassification.json'
    progress = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if any(r.get('status') != 'complete' for r in progress.values()):
        raise RuntimeError('uncertain prior topic reclassification; inspect memberships, no automatic replay')
    checkpoint = evidence / 'uploads.json'
    uploads = json.loads(checkpoint.read_text(encoding='utf-8'))
    exclusions = json.loads(m11_papers.MANIFEST.with_name('topic-exclusions.json').read_text(encoding='utf-8'))
    name = 'Cần xem lại — ngoài phạm vi M11'
    with Ledger(evidence / 'cost').exclusive():
        api = Api(root)
        try:
            collections = {c['name']: c['id'] for c in api.request('GET', '/collections')['collections']}
            if name not in collections:
                progress['_collection'] = {'status': 'sending'}
                m11_papers.save(path, progress)
                collections[name] = api.request('POST', '/collections', json={'name': name})['collection']['id']
                progress['_collection'] = {'status': 'complete', 'id': collections[name]}
                m11_papers.save(path, progress)
            for paper in exclusions:
                tagged, iid = paper['tagged'], paper.get('uploaded_item_id')
                if not iid or tagged in progress:
                    continue
                row = uploads[tagged]
                progress[tagged] = {'status': 'sending', 'item_id': iid, 'old_topic': row['topic']}
                m11_papers.save(path, progress)
                # Membership removal does not remove the item, original or derived content.
                api.request('DELETE', '/collections/' + collections[row['topic']] + '/items/' + iid)
                api.request('POST', '/collections/' + collections[name] + '/items', json={'item_id': iid})
                row.update({'excluded_from_target': paper['reason'], 'topic': name})
                m11_papers.save(checkpoint, uploads)
                progress[tagged]['status'] = 'complete'
                m11_papers.save(path, progress)
                print(json.dumps({'retained_item': iid, 'excluded_from_target': tagged}, ensure_ascii=False), flush=True)
            m11_papers.save(evidence / 'collections.json', collections)
        finally:
            api.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['run', 'inspect', 'quarantine-topics'])
    p.add_argument('--root', type=Path, default=Path(os.environ['USERPROFILE']) / 'ras-pilot')
    p.add_argument('--manifest', type=Path, default=m11_papers.MANIFEST)
    p.add_argument('--watch', action='store_true')
    p.add_argument('--target', type=int, default=300)
    args = p.parse_args()
    if args.action == 'run':
        run(args.root, args.manifest, args.watch, args.target)
    elif args.action == 'quarantine-topics':
        quarantine_topics(args.root)
    else:
        api = Api(args.root)
        try:
            items = api.inventory()
            print(json.dumps({'total': len(items), 'indexed': sum(i['content_status'] == 'indexed' for i in items)}))
        finally:
            api.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'M11 stopped: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1) from None
