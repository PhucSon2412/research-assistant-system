"""M7a live runtime smoke on the real C4 image with synthetic data and a hard ledger ceiling.

Usage: --context <C4 stack context> --state <m7a ledger dir> configure|up|run|chrome|down
Requires c4_stack setup/build and c4_http_flow default (synthetic accounts) first.
The key reaches containers only through this process's environment (compose
interpolation); it is never written to the context. Pricing flags are enabled
only in this synthetic context. App containers keep the internal network; a
CONNECT proxy allows openrouter.ai:443 and nothing else. The test-only observer
(scripts/m7a_observer.py) sends a provider request only with a per-slot permit token the
host ledger admitted for the current action.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'api'))
import m7a_live  # noqa: E402

CORPUS = REPO / 'evaluation' / 'm7a-synthetic'
EGRESS = """const http=require('http'),net=require('net');
const server=http.createServer((req,res)=>{console.log('deny plain '+req.method);res.writeHead(403);res.end();});
server.on('connect',(req,client,head)=>{
 const [host,port]=String(req.url).split(':');
 if(host!=='openrouter.ai'||port!=='443'){console.log('deny connect '+host+':'+port);client.end('HTTP/1.1 403 Forbidden\\r\\n\\r\\n');return;}
 console.log('allow connect openrouter.ai:443');
 const up=net.connect(443,host,()=>{client.write('HTTP/1.1 200 Connection Established\\r\\n\\r\\n');up.write(head);up.pipe(client);client.pipe(up);});
 up.on('error',()=>client.destroy());client.on('error',()=>up.destroy());
});
server.listen(3128,'0.0.0.0');
"""
CEILING = {'embedding': 'embedding-document', 'query': 'embedding-query', 'rerank': 'rerank', 'generation': 'generation'}
FAMILY = {'embedding': 'embeddings', 'query': 'embeddings', 'rerank': 'rerank', 'generation': 'chat'}


class Gate:
    """One runtime action at a time, each possible provider call bound to its own permit token.

    The ledger lock is held from admission through the action, permit revocation and
    settlement, so actions never overlap and a crash leaves the lock (fail closed). Every
    slot is admitted durably before its token exists. Closing renames each still-open token
    to ``revoked``; a token the observer already claimed cannot be revoked, and its slot is
    settled only from a receipt carrying that token. Claimed without a terminal receipt by
    the end of the grace period stays ``uncertain`` with its full request and ceiling; only a
    token the host revoked itself is ``not_sent``. Silence or a returned client never is.
    """

    def __init__(self, ledger, permits: Path, receipts: Path, grace_s: float = 90.0, poll_s: float = 0.5):
        self.ledger, self.permits, self.receipts_path = ledger, permits, receipts
        self.grace_s, self.poll_s = grace_s, poll_s

    def prepare(self):
        for name in ('open', 'claimed', 'revoked'):
            (self.permits / name).mkdir(parents=True, exist_ok=True)
        self.receipts_path.touch()

    def receipts(self):
        rows = []
        for line in self.receipts_path.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn line proves nothing; its permit stays uncertain
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def run(self, label, plan, action, settings):
        self.prepare()
        with self.ledger.exclusive():
            self.ledger.require_settled()
            stale = sorted(p.name for p in (self.permits / 'open').iterdir())
            if stale:
                for name in stale:
                    try:
                        os.replace(self.permits / 'open' / name, self.permits / 'revoked' / name)
                    except FileNotFoundError:
                        pass
                raise SystemExit('stale open permits of an unsettled action were revoked; resolve manually, not sent')
            batch = uuid.uuid4().hex[:12]
            slots = []
            for index, route in enumerate(plan):
                ceiling, source = m7a_live.ceiling_for(CEILING[route], settings)
                seq = self.ledger.admit('runtime:' + label + ':' + CEILING[route], ceiling, source)
                slots.append((seq, f'{batch}.{index}.{FAMILY[route]}'))
            for seq, token in slots:
                (self.permits / 'open' / token).write_text(str(seq), encoding='utf-8')
            try:
                result = action()
            finally:
                responses = self.close(label, slots)
        return result, responses

    def close(self, label, slots):
        revoked, claimed = set(), set()
        for _, token in slots:
            try:
                os.rename(self.permits / 'open' / token, self.permits / 'revoked' / token)
                revoked.add(token)
            except (FileNotFoundError, FileExistsError, PermissionError):
                claimed.add(token)  # the observer won this token (or it vanished): never not_sent
        deadline = time.monotonic() + self.grace_s
        while True:
            terminal = {r.get('permit'): r for r in self.receipts()
                        if r.get('event') in ('response', 'transport_error') and r.get('permit') in claimed}
            if set(terminal) >= claimed or time.monotonic() >= deadline:
                break
            time.sleep(min(self.poll_s, max(0.0, deadline - time.monotonic())))
        responses = []
        for seq, token in slots:
            row = terminal.get(token)
            if token in revoked:
                outcome = {'status': 'not_sent', 'proof': 'permit_revoked_by_host', 'permit': token, 'observed': []}
            elif row is None:
                outcome = {'status': 'uncertain', 'error_code': 'permit_claimed_no_receipt', 'permit': token, 'observed': []}
            elif row['event'] == 'transport_error':
                outcome = {'status': 'uncertain', 'error_code': 'transport_error', 'permit': token, 'observed': [row]}
            else:
                cost = m7a_live.valid_cost((row.get('usage') or {}).get('cost'))
                outcome = {'status': 'completed' if row.get('status') == 200 and cost is not None else 'uncertain',
                           'permit': token, 'observed': [row]}
                responses.append(row)
            cost = m7a_live.valid_cost((row or {}).get('usage', {}).get('cost')) if row and row['event'] == 'response' else None
            outcome['provider_cost_usd'] = float(cost) if cost is not None else None
            self.ledger.append({'event': 'outcome', 'seq': seq, 'route': 'runtime:' + label, 'at': m7a_live.now(), **outcome})
        print(json.dumps({'action': label, 'admitted': len(slots), 'claimed': len(claimed), 'revoked': len(revoked),
                          'responses': len(responses), 'ledger': self.ledger.state()}, default=str), flush=True)
        return responses


def corpus():
    md_a = (CORPUS / 'lumen-bench.md').read_bytes()
    from reportlab.pdfgen import canvas
    lines = [line for line in (CORPUS / 'harbor-sensor.txt').read_text(encoding='utf-8').splitlines()]
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=(612, 792), pageCompression=0, invariant=1)
    y = 760
    for line in lines:
        pdf.drawString(36, y, line)
        y -= 14
    pdf.showPage()
    pdf.save()
    return md_a, out.getvalue()


def embedded_chunks(data: bytes, kind: str) -> int:
    """Distinct body/abstract chunk hashes the worker will embed, via the worker's own parsers."""
    from types import SimpleNamespace
    from app.config import Settings
    from app.ingest import pdf
    from app.ingest.markdown import chunk_hash, chunk_section, extract
    settings = Settings(_env_prefix='RAS_M7A_UNUSED_', db=None, database_url='postgresql+psycopg://unused@localhost/unused')
    size = settings.ingest.markdown_max_chunk_chars
    hashes = set()
    if kind == 'markdown':
        text = data.decode('utf-8')
        for section in extract(text).sections:
            if section.kind in ('body', 'abstract'):
                hashes |= {chunk_hash(span.text) for span in chunk_section(text, section, size)}
    else:
        from pypdf import PdfReader
        pages = len(PdfReader(io.BytesIO(data)).pages)
        result = pdf.classify(pdf.parse(data, pages, settings), settings)
        if result.disposition != 'awaiting_index':
            raise SystemExit('synthetic PDF would not reach index: ' + str(result.disposition))
        for segment in result.segments:
            if segment.kind in ('body', 'abstract') and segment.text:
                page = result.pages[segment.page - 1]
                span = SimpleNamespace(kind=segment.kind, heading=None, start_offset=segment.start, end_offset=segment.end)
                hashes |= {chunk_hash(s.text) for s in chunk_section(page.text, span, size)}
    return len(hashes)


class Drill:
    def __init__(self, root: Path, state: Path):
        self.root, self.state = root, state
        self.c = json.loads((root / 'stack.json').read_text())
        assert self.c['project'].startswith('rasc4')
        e = self.c['env']
        self.origin, self.api = 'http://127.0.0.1:' + e['WEB_HOST_PORT'], 'http://127.0.0.1:' + e['API_HOST_PORT']
        self.fixture = root / 'm7a-fixture'
        self.ledger = m7a_live.Ledger(state)
        self.gate = Gate(self.ledger, self.fixture / 'permits', self.fixture / 'receipts.jsonl')
        self.log = state / 'runtime'
        self.log.mkdir(exist_ok=True)
        self.checks = []

    def check(self, name, condition, **facts):
        self.checks.append({'check': name, 'pass': bool(condition), **facts})
        (self.log / 'checks.json').write_text(json.dumps(self.checks, indent=2, default=str), encoding='utf-8')
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        assert condition, name

    def compose(self, *args, key=None):
        env = dict(os.environ)
        if key:
            env['M7A_OPENROUTER_KEY'] = key
        r = subprocess.run([sys.executable, str(REPO / 'scripts/c4_stack.py'), '--context', str(self.root), 'compose', *args],
                           capture_output=True, text=True, encoding='utf-8', env=env)
        text = r.stdout + r.stderr
        (self.log / f'compose-{time.time_ns()}.log').write_text(m7a_live.redact(text, key or ''), encoding='utf-8')
        if r.returncode:
            raise SystemExit('compose failed; see runtime log')
        return r

    def configure(self):
        self.fixture.mkdir(exist_ok=True)
        shutil.copyfile(REPO / 'scripts/m7a_observer.py', self.fixture / 'sitecustomize.py')
        (self.fixture / 'egress.cjs').write_text(EGRESS, encoding='utf-8')
        self.gate.prepare()
        config = json.loads((self.root / 'override.json').read_text(encoding='utf-8'))
        config['services']['egress'] = {'image': 'node:24-alpine', 'command': ['node', '/m7a/egress.cjs'],
            'volumes': [self.fixture.as_posix() + ':/m7a:ro'], 'networks': ['default', 'exposure']}
        environment = {'PYTHONPATH': '/m7a-fixture:/srv/api', 'M7A_RECEIPTS': '/m7a-fixture/receipts.jsonl',
            'M7A_PERMITS': '/m7a-fixture/permits', 'M7A_OBSERVER': '1', 'HTTPS_PROXY': 'http://egress:3128',
            'NO_PROXY': 'api,web,db,minio,redis,egress,localhost,127.0.0.1',
            'RAS_EMBEDDING__API_KEY': '${M7A_OPENROUTER_KEY:-}'}
        for route in ('EMBEDDING', 'RERANK', 'GENERATION'):
            environment[f'RAS_{route}__PRICING_VERIFIED'] = 'true'  # synthetic context only
            environment[f'RAS_{route}__MAX_CONCURRENT_REQUESTS'] = '1'
        for service in ('api', 'worker', 'beat'):
            settings = config['services'].setdefault(service, {})
            settings['volumes'] = [self.fixture.as_posix() + ':/m7a-fixture']
            settings['environment'] = environment
            settings.setdefault('depends_on', {})['egress'] = {'condition': 'service_started'}
        (self.root / 'override.json').write_text(json.dumps(config), encoding='utf-8')
        print('configured live synthetic context (no key on disk)')

    # ---- ledger-gated runtime actions ----
    def receipts(self):
        path = self.fixture / 'receipts.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def gated(self, label, plan, action):
        return self.gate.run(label, plan, action, m7a_live.settings_for('unused-key-for-ceiling'))

    def client(self, key):
        accounts = json.loads((self.root / 'synthetic-accounts.json').read_text())
        out = httpx.Client(base_url=self.origin, headers={'origin': self.origin}, timeout=180)
        out.get('/api/auth/csrf')
        r = self.post(out, '/auth/login', accounts[key])
        assert r.status_code == 200, r.status_code
        return out

    @staticmethod
    def post(client, path, body):
        return client.post('/api' + path, json=body, headers={'x-csrf-token': client.cookies.get('ras_csrf', '')})

    def upload(self, client, data, kind, filename):
        notice = client.get('/api/uploads/notice').json()['version']
        r = self.post(client, '/uploads/intents', {'acknowledged': True, 'notice_version': notice, 'file_kind': kind,
            'filename': filename, 'byte_size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        assert r.status_code == 201, r.text
        sent = client.put('/api/uploads/original', content=data, headers={'x-csrf-token': client.cookies.get('ras_csrf', ''),
            'x-upload-intent': r.json()['intent_token'], 'content-type': 'application/octet-stream'})
        assert sent.status_code == 201, sent.text
        return sent.json()

    @staticmethod
    def wait(client, item, seconds=300):
        end = time.monotonic() + seconds
        terminal = ('ready', 'duplicate', 'terminal_failed', 'fulltext_rejected', 'needs_review', 'cancelled')
        while time.monotonic() < end:
            r = client.get('/api/library/items/' + item)
            if r.status_code == 200:
                detail = r.json()['item']
                job = detail['jobs'][0]
                if job['state'] in terminal or (job['state'] == 'retryable_failed' and job['last_error_code']):
                    return detail
            time.sleep(1)
        raise AssertionError('worker did not settle')

    def run(self):
        md_a, pdf_a = corpus()
        plan = {'lumen-md': embedded_chunks(md_a, 'markdown'), 'harbor-pdf': embedded_chunks(pdf_a, 'pdf')}
        total = plan['lumen-md'] * 2 + plan['harbor-pdf']  # A markdown, A PDF, B same bytes as A markdown
        self.check('preflight corpus embeds at most eight chunks', total <= 8, plan=plan, total=total)
        (self.log / 'corpus.json').write_text(json.dumps({'lumen-md_sha256': hashlib.sha256(md_a).hexdigest(),
            'harbor-pdf_sha256': hashlib.sha256(pdf_a).hexdigest(), 'plan': plan}), encoding='utf-8')
        self.check('real API readiness', httpx.get(self.api + '/readyz', timeout=30).status_code == 200)
        a, b, admin = self.client('a'), self.client('b'), self.client('admin')
        out = {}
        for label, who, data, kind, name in (('a-lumen', a, md_a, 'markdown', 'lumen-bench.md'),
                                             ('a-harbor', a, pdf_a, 'pdf', 'harbor-sensor.pdf'),
                                             ('b-lumen', b, md_a, 'markdown', 'lumen-bench.md')):
            n = plan['lumen-md' if kind == 'markdown' else 'harbor-pdf']
            ids, _ = self.gated(label, ['embedding'] * n, lambda: (lambda r: (r, self.wait(who, r['item_id'])))(
                self.upload(who, data, kind, name)))
            receipt, detail = ids
            out[label] = receipt
            self.check(label + ' indexed with live embeddings', detail['content_status'] == 'indexed'
                       and detail['jobs'][0]['state'] == 'ready' and detail['jobs'][0]['existing_copy'] is None,
                       state=detail['jobs'][0]['state'], error=detail['jobs'][0]['last_error_code'])
        self.check('B same bytes independent, A not visible to B or Admin',
                   b.get('/api/library/items/' + out['a-lumen']['item_id']).status_code == 404
                   and admin.get('/api/library/items/' + out['a-lumen']['item_id']).status_code == 404)
        (search, rows) = self.gated('hybrid-search', ['query', 'rerank'], lambda: self.post(a, '/search', {
            'query': 'calibration bench drift', 'mode': 'hybrid', 'rerank': True}))
        hits = search.json().get('hits', []) if search.status_code == 200 else []
        self.check('hybrid search with live query embedding and rerank', search.status_code == 200 and hits
                   and len(rows) == 2 and {r['path'] for r in rows} == {'/api/v1/embeddings', '/api/v1/rerank'}
                   and all(h['item_id'] != out['b-lumen']['item_id'] for h in hits), status=search.status_code,
                   paths=[r['path'] for r in rows], hits=len(hits))
        questions = [('single', {'item_id': out['a-lumen']['item_id']},
                      'How much drift did the Lumen calibration bench measure over twelve hours?'),
                     ('compare', {'scope': 'all'},
                      'Compare the drift of the Lumen calibration bench with the error of the Harbor tide sensor.'),
                     ('insufficient', {'item_id': out['a-lumen']['item_id']},
                      'What was the purchase price of the Lumen calibration bench?')]
        answers = []
        for label, scope, question in questions:
            (response, rows) = self.gated('rag-' + label, ['generation'], lambda: self.post(
                a, '/rag/ask', {**scope, 'question': question, 'mode': 'keyword'}))
            body = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
            answers.append({'label': label, 'question': question, 'http': response.status_code,
                            'status': body.get('status'), 'reason': body.get('reason'),
                            'claims': [{'text': c['text'], 'citations': [{'quote': x['quote'], 'title': x['title'],
                                'locator_kind': x['locator_kind'], 'start_page': x['start_page'], 'start_line': x['start_line'],
                                'chunk_id': x['chunk_id'], 'version_id': x['version_id'], 'source_file_id': x['source_file_id']}
                                for x in c['citations']]} for c in body.get('claims', [])],
                            'provider_calls': len(rows), 'error': body.get('error')})
            self.check('RAG ' + label + ' classified', response.status_code in (200, 409, 503),
                       http=response.status_code, status=body.get('status'), error=body.get('error'))
        (self.log / 'answers.json').write_text(json.dumps(answers, indent=2, ensure_ascii=False), encoding='utf-8')
        cited = [x for item in answers for c in item['claims'] for x in c['citations']]
        for x in cited:
            r = a.get('/api/search/chunks/' + x['chunk_id'])
            chunk = r.json().get('chunk', r.json()) if r.status_code == 200 else {}
            self.check('citation opens exact version/file/locator', r.status_code == 200
                       and chunk.get('version_id') == x['version_id'] and chunk.get('source_file_id') == x['source_file_id']
                       and x['quote'] in chunk.get('text', ''), chunk=x['chunk_id'])
        a_chunks = [x['chunk_id'] for x in cited if x['title'] and 'Lumen' in str(x['title'])]
        for x in cited:
            self.check('B and Admin denied cited private chunk', b.get('/api/search/chunks/' + x['chunk_id']).status_code == 404
                       and admin.get('/api/search/chunks/' + x['chunk_id']).status_code == 404)
        receipt = self.post(a, '/library/items/' + out['a-lumen']['item_id'] + '/delete', {'confirm_delete': True})
        self.check('owner deletes cited private item', receipt.status_code == 200)
        reopened = [a.get('/api/search/chunks/' + c).status_code for c in a_chunks]
        (late, rows) = self.gated('rag-after-delete', ['generation'], lambda: self.post(
            a, '/rag/ask', {'item_id': out['a-lumen']['item_id'], 'question': questions[0][2], 'mode': 'keyword'}))
        self.check('new requests cannot reopen deleted source', all(code == 404 for code in reopened)
                   and late.status_code == 404 and not rows, reopened=reopened, rag=late.status_code)
        self.check('B independent same-bytes copy still indexed',
                   b.get('/api/library/items/' + out['b-lumen']['item_id']).json()['item']['content_status'] == 'indexed')
        self.check('readiness open after mutations', httpx.get(self.api + '/readyz', timeout=30).status_code == 200)
        targets = {'origin': self.origin, 'harbor': out['a-harbor']}
        (self.log / 'targets.json').write_text(json.dumps(targets), encoding='utf-8')

    def chrome(self):
        from playwright.sync_api import sync_playwright, expect
        accounts = json.loads((self.root / 'synthetic-accounts.json').read_text())
        targets = json.loads((self.log / 'targets.json').read_text())
        origin = self.origin
        expect.set_options(timeout=180000)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
            ctx = browser.new_context(); page = ctx.new_page(); outside, posts = [], []
            ctx.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/')
                      else (outside.append(route.request.url), route.abort()))
            page.on('request', lambda r: posts.append(r.url) if r.method == 'POST' and r.url.endswith('/api/rag/ask') else None)
            page.goto(origin + '/login')
            page.get_by_label('Email', exact=True).fill(accounts['a']['email'])
            page.get_by_label('Mật khẩu', exact=True).fill(accounts['a']['password'])
            page.get_by_role('button', name='Đăng nhập', exact=True).click(); page.wait_for_url(origin + '/')
            page.goto(origin + '/items/' + targets['harbor']['item_id'])
            page.get_by_label('Câu hỏi', exact=True).fill('What error did the Harbor tide sensor report?')

            def ask():
                page.get_by_role('button', name='Hỏi', exact=True).click()
                expect(page.get_by_text('Trạng thái:', exact=False)).to_be_visible()
                text = page.locator('div[aria-live="polite"]').inner_text()
                links = page.locator('div[aria-live="polite"] a[href^="/sources/"]')
                opened = None
                if links.count():
                    links.first.click(); page.wait_for_url('**/sources/*')
                    page.wait_for_timeout(2000); opened = page.url
                return text, opened
            (text, opened), rows = self.gated('chrome-rag', ['generation'], ask)
            page.wait_for_timeout(5000)
            (self.log / 'chrome-answer.txt').write_text(text, encoding='utf-8')
            page.screenshot(path=str(self.log / 'chrome-source.png'), full_page=True)
            self.check('Chrome RAG through Next proxy on image answers or classifies', 'Trạng thái:' in text
                       and len(posts) == 1 and len(rows) <= 1, answered='Có câu trả lời' in text, opened=bool(opened))
            self.check('Chrome blocked every external origin', not outside)
            (self.log / 'chrome.json').write_text(json.dumps({'chrome': browser.version, 'outside': outside,
                                                              'rag_posts': len(posts)}), encoding='utf-8')
            browser.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('phase', choices=['configure', 'up', 'run', 'chrome', 'egress-check'])
    a = p.parse_args()
    root, state = a.context.resolve(), a.state.resolve()
    for path in (root, state):
        if path == REPO or REPO in path.parents:
            raise SystemExit('external context/state required')
    drill = Drill(root, state)
    if a.phase == 'configure':
        drill.configure()
    elif a.phase == 'up':
        drill.compose('up', '-d', '--wait', '--force-recreate', 'egress', 'api', 'worker', 'beat', key=m7a_live.load_key())
        print('live synthetic services up')
    elif a.phase == 'egress-check':
        # Read-only egress proof without the observer (no allowance, no inference).
        code = ("import httpx\n"
                "for url in ('https://example.com', 'https://openrouter.ai/api/v1/models/openai/text-embedding-3-small/endpoints'):\n"
                "    try: print(url.split('/')[2], httpx.get(url, timeout=15).status_code)\n"
                "    except Exception as e: print(url.split('/')[2], 'blocked', type(e).__name__)\n")
        r = drill.compose('exec', '-T', '-e', 'PYTHONPATH=/srv/api', 'worker', 'python', '-c', code)
        print(r.stdout.strip())
    elif a.phase == 'run':
        drill.run()
    else:
        drill.chrome()


if __name__ == '__main__':
    main()
