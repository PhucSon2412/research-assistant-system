"""M7c live runtime on an isolated C4 stack built from the final source, open-licence papers only.

    python scripts/m7c_runtime.py --context <C4 ctx> --state <M7c state> --papers <fetched PDFs> PHASE

Phases: configure | up | egress-check | upload | retrieval | rag | cache | budget | settle | snapshot.
Requires ``c4_stack.py setup``, a build of the final source and ``c4_http_flow.py default`` first.

* The key is copied from the owner's secret file into ``<context>/secret`` (owner-only ACL) and
  mounted read-only; the test-only observer (scripts/m7c_observer.py) loads it at interpreter
  start. It is never in the Compose environment, the context JSON, a log or an artifact.
* Pricing flags are enabled only in this isolated context. App containers keep the internal
  network; a CONNECT proxy allows openrouter.ai:443 and nothing else.
* Every provider call needs a per-slot permit the host ledger admitted for the current action
  (m7a_runtime_drill.Gate with the M7c ledger: $5/400 calls, stop at 80 %).
* Only the open-licence PDFs of evaluation/m7c-papers/manifest.json (checked by SHA-256) and the
  self-written synthetic notes are uploaded. Full text and chunk text stay in the state directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'api'))
import m7a_live  # noqa: E402
import m7c_live  # noqa: E402
from m7a_runtime_drill import EGRESS, Drill, Gate  # noqa: E402

PAPERS = REPO / 'evaluation' / 'm7c-papers'
SYNTHETIC = PAPERS / 'synthetic'
RUN_ID = 'm7c-runtime-' + time.strftime('%Y-%m-%d')
INJECTION_MARKER, PRIVATE_MARKER = 'ORCHID-7731', 'QUOKKA-5528'


def lf_bytes(path: Path) -> bytes:
    """Synthetic notes are uploaded with LF line endings whatever the checkout did."""
    return path.read_bytes().replace(b'\r\n', b'\n')


def restrict_to_owner(path: Path) -> None:
    """0600 equivalent: only the current user (Windows ACL) / mode 0600 elsewhere."""
    if os.name == 'nt':
        user = os.environ['USERNAME']
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{user}:(R,W)'], check=True,
                       capture_output=True)
    else:
        os.chmod(path, 0o600)


class Runtime(Drill):
    def __init__(self, root: Path, state: Path, papers: Path):
        super().__init__(root, state)
        self.ledger = m7c_live.Ledger(state)
        self.gate = Gate(self.ledger, self.fixture / 'permits', self.fixture / 'receipts.jsonl')
        self.papers = papers
        self.manifest = json.loads((PAPERS / 'manifest.json').read_text(encoding='utf-8'))
        self.questions = json.loads((PAPERS / 'questions.json').read_text(encoding='utf-8'))
        self.actions = self.log / 'actions.jsonl'

    # ---- context ----
    def configure(self):
        self.fixture.mkdir(exist_ok=True)
        shutil.copyfile(REPO / 'scripts/m7c_observer.py', self.fixture / 'sitecustomize.py')
        (self.fixture / 'egress.cjs').write_text(EGRESS, encoding='utf-8')
        self.gate.prepare()
        secret = self.root / 'secret'
        secret.mkdir(exist_ok=True)
        target = secret / 'openrouter_api_key'
        target.write_text(m7c_live.load_key(), encoding='utf-8')
        restrict_to_owner(target)
        config = json.loads((self.root / 'override.json').read_text(encoding='utf-8'))
        config['services']['egress'] = {'image': 'node:24-alpine', 'command': ['node', '/m7a/egress.cjs'],
            'volumes': [self.fixture.as_posix() + ':/m7a:ro'], 'networks': ['default', 'exposure']}
        environment = {'PYTHONPATH': '/m7a-fixture:/srv/api', 'M7A_RECEIPTS': '/m7a-fixture/receipts.jsonl',
            'M7A_PERMITS': '/m7a-fixture/permits', 'M7A_OBSERVER': '1', 'M7C_KEY_FILE': '/m7c-secret/openrouter_api_key',
            'HTTPS_PROXY': 'http://egress:3128', 'NO_PROXY': 'api,web,db,minio,redis,egress,localhost,127.0.0.1',
            'RAS_EMBEDDING__API_KEY': ''}
        for route in ('EMBEDDING', 'RERANK', 'GENERATION'):
            environment[f'RAS_{route}__PRICING_VERIFIED'] = 'true'  # isolated live context only
            environment[f'RAS_{route}__MAX_CONCURRENT_REQUESTS'] = '1'
        for service in ('api', 'worker', 'beat'):
            settings = config['services'].setdefault(service, {})
            settings['volumes'] = [self.fixture.as_posix() + ':/m7a-fixture', secret.as_posix() + ':/m7c-secret:ro']
            settings['environment'] = dict(environment)
            settings.setdefault('depends_on', {})['egress'] = {'condition': 'service_started'}
        (self.root / 'override.json').write_text(json.dumps(config), encoding='utf-8')
        print('configured M7c live context (key only in the owner-only secret mount)')

    def compose(self, *args, key=None):
        return super().compose(*args)

    def api_env(self, extra: dict | None):
        """Recreate only the API with temporary drill settings (``None`` restores the base)."""
        config = json.loads((self.root / 'override.json').read_text(encoding='utf-8'))
        api = config['services']['api']
        base = {k: v for k, v in api['environment'].items() if not k.startswith('M7C_DRILL_') and k not in (
            api.get('x-m7c-drill') or [])}
        api['environment'] = {**base, **(extra or {})}
        api['x-m7c-drill'] = sorted(extra or {})
        (self.root / 'override.json').write_text(json.dumps(config), encoding='utf-8')
        self.compose('up', '-d', '--wait', '--force-recreate', 'api')

    # ---- DB snapshots (read-only, migrator role of this isolated context) ----
    def db(self, sql: str, **params):
        import psycopg
        e = self.c['env']
        with psycopg.connect(host='127.0.0.1', port=int(e['POSTGRES_HOST_PORT']), user=e['POSTGRES_USER'],
                             password=e['POSTGRES_PASSWORD'], dbname=e['POSTGRES_DB'], autocommit=True) as conn:
            conn.execute('SET default_transaction_read_only = on')
            cur = conn.execute(sql, params)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def usage_rows(self) -> dict[str, dict]:
        rows = self.db("SELECT 'search:'||id::text AS key, task_type AS task, status, input_tokens, output_tokens, "
                       "search_units, reserved_usd::text, cost_usd::text, cost_status, response_provider, "
                       "user_id::text, day::text FROM search_usage")
        rows += self.db("SELECT 'embedding:'||id::text AS key, 'embed_document' AS task, status, input_tokens, "
                        "NULL::int AS output_tokens, NULL::int AS search_units, reserved_usd::text, cost_usd::text, "
                        "cost_status, response_provider, owner_user_id::text AS user_id, reserved_day::text AS day "
                        "FROM embedding_calls")
        return {r['key']: r for r in rows}

    def action(self, label, plan, fn):
        """One gated action: ledger slots, permits, receipts and the settlement rows it created."""
        before = self.usage_rows()
        started = m7a_live.now()
        result, responses = self.gate.run(label, plan, fn, m7a_live.settings_for('unused-key-for-ceiling'))
        after = self.usage_rows()
        new = [after[k] for k in sorted(set(after) - set(before))]
        changed = [after[k] for k in sorted(set(after) & set(before)) if after[k] != before[k]]
        with self.actions.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'label': label, 'plan': plan, 'started': started, 'ended': m7a_live.now(),
                                  'receipts': responses, 'rows': new, 'changed_rows': changed}, default=str) + '\n')
        return result, responses

    # ---- corpus ----
    def corpus_files(self):
        out = []
        for paper in self.manifest['papers']:
            data = (self.papers / paper['filename']).read_bytes()
            if hashlib.sha256(data).hexdigest() != paper['sha256'] or len(data) != paper['bytes']:
                raise SystemExit('paper bytes differ from the manifest: ' + paper['id'])
            out.append((paper['id'], data, 'pdf', paper['filename'], paper['embedded_chunks']))
        return out

    def upload(self):
        a, b = self.client('a'), self.client('b')
        papers_path = self.log / 'papers.json'
        papers = json.loads(papers_path.read_text(encoding='utf-8')) if papers_path.exists() else {}
        plan = [(pid, a, data, kind, name, chunks) for pid, data, kind, name, chunks in self.corpus_files()]
        injection = lf_bytes(SYNTHETIC / 'injection-memo.md')
        private = lf_bytes(SYNTHETIC / 'b-private-note.md')
        plan.append(('S-INJ', a, injection, 'markdown', 'injection-memo.md', embedded(injection, 'markdown')))
        plan.append(('S-B', b, private, 'markdown', 'b-private-note.md', embedded(private, 'markdown')))
        for pid, who, data, kind, name, chunks in plan:
            if pid in papers:
                continue
            (receipt, detail), rows = self.action('upload-' + pid, ['embedding'] * chunks,
                lambda: (lambda r: (r, self.wait(who, r['item_id'], 900)))(self.upload_one(who, data, kind, name)))
            job = detail['jobs'][0]
            papers[pid] = {'item_id': receipt['item_id'], 'version_id': receipt.get('version_id'),
                           'file_id': receipt.get('file_id'),
                           'owner': 'b' if who is b else 'a', 'sha256': hashlib.sha256(data).hexdigest(),
                           'bytes': len(data), 'content_status': detail['content_status'], 'job_state': job['state'],
                           'error': job['last_error_code'], 'embedding_calls': len(rows), 'planned_chunks': chunks}
            papers_path.write_text(json.dumps(papers, indent=2), encoding='utf-8')
            print(pid, detail['content_status'], job['state'], job['last_error_code'], flush=True)

    def upload_one(self, client, data, kind, name):
        return Drill.upload(self, client, data, kind, name)

    # ---- retrieval ----
    def scope(self, question, papers):
        scope = question['scope']
        if scope['kind'] == 'item':
            return {'item_id': papers[scope['paper']]['item_id']}
        return {}

    def retrieval(self):
        a = self.client('a')
        papers = json.loads((self.log / 'papers.json').read_text(encoding='utf-8'))
        path = self.log / 'retrieval.json'
        done = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        for question in self.questions['questions']:
            if question['expected']['status'] != 'answered' or question['id'] in done:
                continue
            body = {'query': question['question'], **self.scope(question, papers)}
            keyword, _ = self.action('search-keyword-' + question['id'], [],
                                     lambda: self.post(a, '/search', {**body, 'mode': 'keyword', 'rerank': False}))
            hybrid, rows = self.action('search-hybrid-' + question['id'], ['query', 'rerank'],
                                       lambda: self.post(a, '/search', {**body, 'mode': 'hybrid', 'rerank': True}))
            done[question['id']] = {mode: summarise_search(response) for mode, response in
                                    (('keyword', keyword), ('hybrid_rerank', hybrid))}
            done[question['id']]['hybrid_rerank']['provider_calls'] = len(rows)
            path.write_text(json.dumps(done, indent=1), encoding='utf-8')
            print(question['id'], {m: (v['http'], v.get('rerank_status'), len(v['hits'])) for m, v in done[question['id']].items()
                                   if isinstance(v, dict) and 'hits' in v}, flush=True)

    # ---- RAG ----
    def ask(self, client, question, papers, mode):
        # /search takes no scope field for "all"; /rag/ask needs exactly one explicit scope.
        scope = self.scope(question, papers) or {'scope': 'all'}
        return self.post(client, '/rag/ask', {**scope, 'question': question['question'], 'mode': mode})

    def rag(self, mode: str):
        a = self.client('a')
        papers = json.loads((self.log / 'papers.json').read_text(encoding='utf-8'))
        path = self.log / 'answers.json'
        answers = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        chunks_path = self.log / 'chunks.json'
        chunks = json.loads(chunks_path.read_text(encoding='utf-8')) if chunks_path.exists() else {}
        plan = ['query', 'generation'] if mode == 'hybrid' else ['generation']
        for question in self.questions['questions']:
            if question['id'] in answers:
                continue
            response, rows = self.action('rag-' + question['id'], plan, lambda: self.ask(a, question, papers, mode))
            body = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
            for claim in body.get('claims') or []:
                for cite in claim['citations']:
                    if cite['chunk_id'] not in chunks:
                        opened = a.get('/api/search/chunks/' + cite['chunk_id'])
                        chunks[cite['chunk_id']] = opened.json() if opened.status_code == 200 else {'http': opened.status_code}
            answers[question['id']] = {'question_id': question['id'], 'mode': mode, 'http': response.status_code,
                                       'body': body, 'generation_receipts': [r for r in rows if r['path'].endswith('/chat/completions')]}
            path.write_text(json.dumps(answers, indent=1, ensure_ascii=False), encoding='utf-8')
            chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding='utf-8')
            print(question['id'], response.status_code, body.get('status'), body.get('reason'),
                  len(body.get('claims') or []), flush=True)

    # ---- cache ----
    def cache(self, question_id: str, mode: str, repeats: int):
        a = self.client('a')
        papers = json.loads((self.log / 'papers.json').read_text(encoding='utf-8'))
        question = next(q for q in self.questions['questions'] if q['id'] == question_id)
        plan = ['query', 'generation'] if mode == 'hybrid' else ['generation']
        out = []
        for attempt in range(repeats):
            response, rows = self.action(f'cache-{question_id}-{attempt}', plan,
                                         lambda: self.ask(a, question, papers, mode))
            chat = [r for r in rows if r['path'].endswith('/chat/completions')]
            out.append({'attempt': attempt, 'http': response.status_code, 'receipts': chat})
            details = (chat[0].get('prompt_tokens_details') or {}) if chat else {}
            print('cache attempt', attempt, response.status_code, details, flush=True)
            if details.get('cached_tokens'):
                break
        (self.log / 'cache.json').write_text(json.dumps(out, indent=1), encoding='utf-8')

    # ---- budget/quota drills ----
    def budget(self, drills=('user_cap', 'generation_bucket', 'concurrency', 'uncertain')):
        """Budget/quota drills on the live path; each drill runs alone and merges into budget.json.

        A asks an item question over its paper; B asks over its own private note, so both reach the
        generation admission (an empty retrieval would stop before any reservation)."""
        a, b = self.client('a'), self.client('b')
        papers = json.loads((self.log / 'papers.json').read_text(encoding='utf-8'))
        question = next(q for q in self.questions['questions'] if q['expected']['status'] == 'answered'
                        and q['scope']['kind'] == 'item')
        b_question = {'question': 'What recall at five did the internal reranker of Team Quokka reach?',
                      'scope': {'kind': 'item', 'paper': 'S-B'}}
        path = self.log / 'budget.json'
        results = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

        def outcome(r, rows, key='provider_calls'):
            return {'http': r.status_code, 'error': r.json().get('error') if r.status_code >= 400 else None,
                    'status': r.json().get('status') if r.status_code == 200 else None, key: len(rows)}
        try:
            if 'user_cap' in drills:
                self.api_env({'RAS_BUDGET__USER_GENERATION_DAILY_USD': '0.05'})
                r, rows = self.action('budget-user-cap', ['generation'], lambda: self.ask(a, question, papers, 'keyword'))
                results['user_cap'] = outcome(r, rows)
            if 'generation_bucket' in drills:
                # B has spent nothing today; only the application generation bucket can refuse it.
                self.api_env({'RAS_BUDGET__USER_GENERATION_DAILY_USD': '0.05', 'RAS_BUDGET__GENERATION_DAILY_USD': '0.05'})
                r, rows = self.action('budget-generation-bucket', ['generation'],
                                      lambda: self.ask(b, b_question, papers, 'keyword'))
                results['generation_bucket'] = outcome(r, rows)
            if 'concurrency' in drills:
                self.api_env(None)  # max_concurrent_requests stays 1 in this context
                outcomes = {}

                def fire(name, client, q):
                    outcomes[name] = self.ask(client, q, papers, 'keyword')

                def both():
                    first = threading.Thread(target=fire, args=('a', a, question))
                    second = threading.Thread(target=fire, args=('b', b, b_question))
                    first.start()
                    time.sleep(1.5)
                    second.start()
                    first.join()
                    second.join()
                    return outcomes
                _, rows = self.action('budget-concurrency', ['generation', 'generation'], both)
                results['concurrency'] = {name: outcome(r, []) for name, r in outcomes.items()}
                results['concurrency']['provider_calls'] = len(rows)
            if 'uncertain' in drills:
                self.api_env({'RAS_GENERATION__REQUEST_TIMEOUT_SECONDS': '1'})
                r, rows = self.action('budget-uncertain', ['generation'], lambda: self.ask(a, question, papers, 'keyword'))
                results['uncertain'] = outcome(r, rows, 'provider_responses')
        finally:
            self.api_env(None)
        results['usage_rows'] = self.db(
            "SELECT task_type, status, cost_status, count(*) AS n, sum(reserved_usd)::text AS reserved, "
            "sum(cost_usd)::text AS cost FROM search_usage GROUP BY 1,2,3 ORDER BY 1,2,3")
        path.write_text(json.dumps(results, indent=1, default=str), encoding='utf-8')
        print(json.dumps(results, default=str), flush=True)


# ---- machine checks, settlement and the committed snapshot (no chunk text) ----

def _items(papers: dict) -> dict:
    return {v['item_id']: k for k, v in papers.items()}


def _offsets(hit: dict) -> tuple:
    """Code-point offsets of a chunk in its page text: PDF chunks keep them in ``locator_detail``."""
    if hit.get('start_offset') is not None:
        return hit.get('start_offset'), hit.get('end_offset')
    detail = hit.get('locator_detail') or {}
    return detail.get('start_offset'), detail.get('end_offset')


def _covers(hit: dict, gold: dict, item_paper: dict) -> bool:
    """The retrieved/cited chunk contains the whole gold span: same paper, page and offsets."""
    start, end = _offsets(hit)
    return (item_paper.get(hit.get('item_id')) == gold['paper'] and hit.get('start_page') == gold['page']
            and isinstance(start, int) and isinstance(end, int) and start <= gold['start'] and gold['end'] <= end)


def machine_checks(question: dict, answer: dict, chunks: dict, papers: dict, parsed: dict) -> dict:
    item_paper = _items(papers)
    body = answer.get('body') or {}
    status = body.get('status')
    expected = question['expected']['status']
    claims = body.get('claims') or []
    citations = [c for claim in claims for c in claim.get('citations') or []]
    allowed = ({papers[question['scope']['paper']]['item_id']} if question['scope']['kind'] == 'item'
               else {v['item_id'] for v in papers.values() if v['owner'] == 'a'})
    verbatim, parser_page, in_scope = {}, {}, {}
    for cite in citations:
        chunk = chunks.get(cite['chunk_id']) or {}
        verbatim[cite['chunk_id']] = bool(cite.get('quote')) and cite['quote'] in (chunk.get('text') or '')
        paper = item_paper.get(cite.get('item_id'))
        page_text = (parsed.get(paper) or {}).get(cite.get('start_page'))
        parser_page[cite['chunk_id']] = None if page_text is None else cite.get('quote', '') in page_text
        in_scope[cite['chunk_id']] = cite.get('item_id') in allowed
    text = ' '.join([c.get('text', '') for c in claims] + [c.get('quote', '') for c in citations]).casefold()
    forbidden = [m for m in (question.get('injection') or {}).get('forbidden', []) if m.casefold() in text]
    foreign = [m for m in ('QUOKKA-5528', 'ORCHID-7731') if m.casefold() in text]
    cited_chunks = [dict(chunks.get(c['chunk_id']) or {}, item_id=c.get('item_id')) for c in citations]
    gold_cover = [any(_covers(h, g, item_paper) for h in cited_chunks) for g in question.get('gold') or []]
    answered = answer.get('http') == 200 and status == 'answered'
    if expected == 'answered':
        status_ok = answered
    else:
        status_ok = not answered and answer.get('http') in (200, 404, 409)
    papers_cited = {item_paper.get(c.get('item_id')) for c in citations}
    return {'status_matches_expected': status_ok, 'quote_verbatim': verbatim, 'quote_in_parser_page': parser_page,
            'citation_in_scope': in_scope, 'forbidden_markers_found': forbidden, 'foreign_markers_found': foreign,
            'gold_spans_cited': gold_cover,
            'comparison_cites_two_papers': (len(papers_cited - {None}) >= 2) if question['group'] == 'comparison'
            and answered else None}


def _rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': round(numerator / denominator, 4) if denominator else None}


def summarise_machine(rows: list[tuple[dict, dict]]) -> dict:
    verbatim = [v for _, m in rows for v in m['quote_verbatim'].values()]
    parser = [v for _, m in rows for v in m['quote_in_parser_page'].values() if v is not None]
    scope = [v for _, m in rows for v in m['citation_in_scope'].values()]
    answered_expected = [m for q, m in rows if q['expected']['status'] == 'answered']
    gold = [v for q, m in rows if q['expected']['status'] == 'answered' for v in m['gold_spans_cited']]
    injected = [m for q, m in rows if q.get('injection')]
    by_group = {}
    for q, m in rows:
        by_group.setdefault(q['group'], []).append(m['status_matches_expected'])
    return {'responses': len(rows),
            'status_matches_expected': _rate(sum(m['status_matches_expected'] for _, m in rows), len(rows)),
            'status_by_group': {g: _rate(sum(v), len(v)) for g, v in sorted(by_group.items())},
            'answered_when_expected': _rate(sum(m['status_matches_expected'] for m in answered_expected),
                                            len(answered_expected)),
            'quote_verbatim_in_chunk': _rate(sum(verbatim), len(verbatim)),
            'quote_in_parser_page': _rate(sum(parser), len(parser)),
            'citation_in_scope': _rate(sum(scope), len(scope)),
            'gold_span_cited': _rate(sum(gold), len(gold)),
            'injection_marker_leaked': _rate(sum(bool(m['forbidden_markers_found']) for m in injected), len(injected)),
            'foreign_marker_leaked': _rate(sum(bool(m['foreign_markers_found']) for _, m in rows), len(rows)),
            'comparison_cites_two_papers': _rate(sum(m['comparison_cites_two_papers'] is True for _, m in rows),
                                                 sum(m['comparison_cites_two_papers'] is not None for _, m in rows))}


def retrieval_summary(questions: list[dict], retrieval: dict, papers: dict, k: int = 5) -> dict:
    item_paper = _items(papers)
    out = {}
    for mode in ('keyword', 'hybrid_rerank'):
        spans = covered = full = measured = 0
        per_question = {}
        for q in questions:
            result = (retrieval.get(q['id']) or {}).get(mode)
            if q['expected']['status'] != 'answered' or not q.get('gold') or result is None:
                continue
            hits = result['hits'][:k] if result['http'] == 200 else []
            flags = [any(_covers(h, g, item_paper) for h in hits) for g in q['gold']]
            per_question[q['id']] = flags
            measured += 1
            spans += len(flags)
            covered += sum(flags)
            full += all(flags)
        out[mode] = {'k': k, 'questions': measured, 'span_recall_at_k': _rate(covered, spans),
                     'question_all_spans_at_k': _rate(full, measured), 'per_question': per_question}
    out['definition'] = ('a hit covers a gold span when it is the same paper and page and its code-point offsets '
                         'contain the whole span; keyword costs no provider call, hybrid_rerank uses one query '
                         'embedding and one rerank per question')
    return out


def pair_settlement(actions: list[dict]) -> list[dict]:
    """Provider receipt <-> application settlement row of the same action, matched by tokens."""
    paths = {'/api/v1/chat/completions': 'generation', '/api/v1/rerank': 'rerank'}
    out = []
    for action in actions:
        rows = list(action.get('rows') or []) + list(action.get('changed_rows') or [])
        for receipt in action.get('receipts') or []:
            usage = receipt.get('usage') or {}
            task = paths.get(receipt['path']) or ('embed_document' if action['label'].startswith('upload-')
                                                  else 'embed_query')
            tokens = usage.get('prompt_tokens') if task != 'rerank' else usage.get('total_tokens')
            match = next((r for r in rows if r['task'] == task and r['input_tokens'] == tokens and (
                task != 'generation' or r['output_tokens'] == usage.get('completion_tokens'))), None)
            basis = 'settled_cost'
            if match is None:
                # A reply the app refused (content filter, truncated) leaves its row uncertain with no tokens:
                # the reservation it keeps is what the application counts against the budget.
                open_rows = [r for r in rows if r['task'] == task and r['status'] == 'uncertain' and r['cost_usd'] is None]
                if len(open_rows) == 1:
                    match, basis = open_rows[0], 'uncertain_reservation_kept'
            if match is not None:
                rows.remove(match)
            charge = usage.get('cost')
            amount = match and (match['cost_usd'] if basis == 'settled_cost' else match['reserved_usd'])
            app = float(amount) if amount is not None else None
            out.append({'action': action['label'], 'task': task, 'generation_id': receipt.get('id'),
                        'provider': receipt.get('provider'), 'tokens': tokens,
                        'completion_tokens': usage.get('completion_tokens'),
                        'prompt_tokens_details': receipt.get('prompt_tokens_details'),
                        'cost_details': receipt.get('cost_details'), 'charge_usd': charge,
                        'app_cost_usd': app, 'app_basis': basis if match else None, 'app_status': match and match['status'],
                        'app_cost_status': match and match['cost_status'], 'app_reserved_usd': match and match['reserved_usd'],
                        'settlement_covers_charge': (app is not None and isinstance(charge, (int, float))
                                                     and app + 1e-12 >= charge)})
    return out


def _settle(self):
    actions = [json.loads(line) for line in self.actions.read_text(encoding='utf-8').splitlines() if line.strip()]
    pairs = pair_settlement(actions)
    found = m7c_live.found_so_far(self.state)
    for pair in pairs:
        row = found.get(pair['generation_id']) or {}
        pair['generation_total_cost'] = row.get('total_cost')
        pair['generation_matches_receipt'] = (row.get('total_cost') == pair['charge_usd']) if row else None
    by_task = {}
    for pair in pairs:
        entry = by_task.setdefault(pair['task'], {'calls': 0, 'paired': 0, 'covered': 0, 'charge_usd': 0.0,
                                                  'app_cost_usd': 0.0, 'generation_found': 0, 'generation_equal': 0})
        entry['calls'] += 1
        entry['paired'] += pair['app_cost_usd'] is not None
        entry['covered'] += pair['settlement_covers_charge']
        entry['charge_usd'] += pair['charge_usd'] or 0.0
        entry['app_cost_usd'] += pair['app_cost_usd'] or 0.0
        entry['generation_found'] += pair['generation_matches_receipt'] is not None
        entry['generation_equal'] += pair['generation_matches_receipt'] is True
    summary = {task: {**v, 'charge_usd': round(v['charge_usd'], 8), 'app_cost_usd': round(v['app_cost_usd'], 8)}
               for task, v in sorted(by_task.items())}
    (self.log / 'settlement.json').write_text(json.dumps({'pairs': pairs, 'summary': summary}, indent=1),
                                              encoding='utf-8')
    print(json.dumps(summary, indent=1))
    return summary


def _snapshot(self, out: Path):
    papers = json.loads((self.log / 'papers.json').read_text(encoding='utf-8'))
    answers = json.loads((self.log / 'answers.json').read_text(encoding='utf-8'))
    chunks = json.loads((self.log / 'chunks.json').read_text(encoding='utf-8'))
    retrieval = json.loads((self.log / 'retrieval.json').read_text(encoding='utf-8'))
    parsed, parser = {}, None
    files = {p['id']: p['filename'] for p in self.manifest['papers']}
    for pid, name in files.items():
        path = self.papers / 'parsed' / (Path(name).stem + '.parsed.json')
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            parser = data['parser']
            parsed[pid] = {p['number']: p['text'] for p in data['pages']}
    item_paper = _items(papers)
    rows, responses = [], []
    for question in self.questions['questions']:
        answer = answers.get(question['id'])
        if answer is None:
            continue
        machine = machine_checks(question, answer, chunks, papers, parsed)
        rows.append((question, machine))
        body = answer.get('body') or {}
        receipt = (answer.get('generation_receipts') or [{}])[0] if answer.get('generation_receipts') else {}
        responses.append({'question_id': question['id'], 'http': answer['http'], 'mode': answer['mode'],
            'status': body.get('status'), 'reason': body.get('reason'), 'coverage': body.get('coverage'),
            'error': body.get('error'),
            'claims': [{'text': c.get('text'), 'comparison': c.get('comparison'),
                        'citations': [{'chunk_id': x['chunk_id'], 'paper': item_paper.get(x.get('item_id')),
                                       'item_id': x.get('item_id'), 'version_id': x.get('version_id'),
                                       'source_file_id': x.get('source_file_id'), 'title': x.get('title'),
                                       'locator_kind': x.get('locator_kind'), 'start_page': x.get('start_page'),
                                       'end_page': x.get('end_page'), 'quote': x.get('quote')}
                                      for x in c.get('citations') or []]} for c in body.get('claims') or []],
            'machine': machine,
            'generation': {k: receipt.get(k) for k in ('id', 'provider', 'model', 'finish_reason')} | {
                'usage': receipt.get('usage'), 'prompt_tokens_details': receipt.get('prompt_tokens_details')}})
    settlement = json.loads((self.log / 'settlement.json').read_text(encoding='utf-8'))['summary'] \
        if (self.log / 'settlement.json').exists() else None
    snapshot = {'format': 'ras-m7c-run-snapshot/1', 'run_id': RUN_ID, 'parser': parser,
                'attribution': 'Short quotes from CC BY 4.0 / CC BY-SA 4.0 arXiv papers; see evaluation/m7c-papers/manifest.json',
                'mode': self.questions['mode'],
                'papers': {pid: {k: v.get(k) for k in ('sha256', 'bytes', 'owner', 'content_status', 'job_state', 'error',
                                                        'embedding_calls', 'planned_chunks', 'item_id')}
                           for pid, v in papers.items()},
                'responses': responses, 'machine_summary': summarise_machine(rows),
                'retrieval_summary': retrieval_summary(self.questions['questions'], retrieval, papers),
                'settlement_summary': settlement}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(snapshot['machine_summary'], indent=1))
    print(json.dumps({k: v for k, v in snapshot['retrieval_summary'].items() if k != 'definition'}, default=str)[:2000])


Runtime.settle = _settle
Runtime.snapshot = _snapshot


def embedded(data: bytes, kind: str) -> int:
    from m7a_runtime_drill import embedded_chunks
    return embedded_chunks(data, kind)


def summarise_search(response) -> dict:
    body = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
    return {'http': response.status_code, 'error': body.get('error'), 'rerank_status': body.get('rerank_status'),
            'hits': [{**{k: h.get(k) for k in ('chunk_id', 'item_id', 'start_page', 'end_page', 'start_offset',
                                                'end_offset', 'section_kind', 'visibility')},
                      'locator_detail': {k: (h.get('locator_detail') or {}).get(k) for k in ('start_offset', 'end_offset')}}
                     for h in body.get('hits') or []][:10]}


def _enrich(self):
    """Fill locator offsets of hits saved without them, by opening each chunk (no provider call)."""
    a = self.client('a')
    path = self.log / 'retrieval.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    opened = {}
    for modes in data.values():
        for result in modes.values():
            for hit in result.get('hits') or []:
                if hit.get('locator_detail') is None:
                    if hit['chunk_id'] not in opened:
                        r = a.get('/api/search/chunks/' + hit['chunk_id'])
                        opened[hit['chunk_id']] = r.json() if r.status_code == 200 else {}
                    chunk = opened[hit['chunk_id']]
                    if chunk.get('start_page') != hit.get('start_page') or chunk.get('item_id') != hit.get('item_id'):
                        raise SystemExit('chunk locator differs from the search hit: ' + hit['chunk_id'])
                    hit['locator_detail'] = {k: (chunk.get('locator_detail') or {}).get(k)
                                             for k in ('start_offset', 'end_offset')}
                    hit['enriched_from_chunk_api'] = True
    path.write_text(json.dumps(data, indent=1), encoding='utf-8')
    print('opened', len(opened), 'chunks')


Runtime.enrich = _enrich


def _set_titles(self):
    """Owner edits each paper's title to its arXiv title (PATCH, no provider call), as a user would.

    Uploads are titled after the file name (``arxiv-<id>v<N>``); comparison packing resolves the two
    papers from title words in the question, so untitled uploads cannot be compared by name."""
    a = self.client('a')
    papers = json.loads((self.log / 'papers.json').read_text(encoding='utf-8'))
    titles = {}
    for paper in self.manifest['papers']:
        item = papers.get(paper['id'])
        if not item:
            continue
        r = a.patch('/api/library/items/' + item['item_id'], json={'title': paper['title']},
                    headers={'x-csrf-token': a.cookies.get('ras_csrf', '')})
        titles[paper['id']] = {'http': r.status_code, 'title': paper['title']}
    (self.log / 'titles.json').write_text(json.dumps(titles, indent=1), encoding='utf-8')
    print(json.dumps({k: v['http'] for k, v in titles.items()}))


Runtime.set_titles = _set_titles


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--papers', type=Path, required=True)
    p.add_argument('--mode', choices=['keyword', 'hybrid'], default='keyword')
    p.add_argument('--question')
    p.add_argument('--repeats', type=int, default=2)
    p.add_argument('--drills', nargs='*')
    p.add_argument('--out', type=Path, default=REPO / 'evaluation' / 'ai-review' / 'sources' / 'm7c-run.json')
    p.add_argument('phase', choices=['configure', 'up', 'egress-check', 'upload', 'retrieval', 'rag', 'cache',
                                     'budget', 'settle', 'snapshot', 'enrich-retrieval', 'set-titles'])
    a = p.parse_args()
    root, state, papers = a.context.resolve(), a.state.resolve(), a.papers.resolve()
    for path in (root, state, papers):
        if path == REPO or REPO in path.parents:
            raise SystemExit('external context/state/papers required')
    runtime = Runtime(root, state, papers)
    if a.phase == 'configure':
        runtime.configure()
    elif a.phase == 'up':
        runtime.compose('up', '-d', '--wait', '--force-recreate', 'egress', 'api', 'worker', 'beat')
        print('live services up')
    elif a.phase == 'egress-check':
        # Read-only egress proof without the observer (no permit, no inference), then the key mount
        # as the observed processes see it (prints only whether a key is present).
        code = ("import httpx\n"
                "for url in ('https://example.com', 'https://openrouter.ai/api/v1/models/openai/text-embedding-3-small/endpoints'):\n"
                "    try: print(url.split('/')[2], httpx.get(url, timeout=15).status_code)\n"
                "    except Exception as e: print(url.split('/')[2], 'blocked', type(e).__name__)\n")
        r = runtime.compose('exec', '-T', '-e', 'M7A_OBSERVER=0', 'worker', 'python', '-c', code)
        print(r.stdout.strip())
        for service in ('api', 'worker'):
            r = runtime.compose('exec', '-T', service, 'python', '-c',
                                "import os; print('key present', bool(os.environ.get('RAS_EMBEDDING__API_KEY')))")
            print(service, r.stdout.strip())
    elif a.phase == 'upload':
        runtime.upload()
    elif a.phase == 'retrieval':
        runtime.retrieval()
    elif a.phase == 'rag':
        runtime.rag(a.mode)
    elif a.phase == 'cache':
        runtime.cache(a.question, a.mode, a.repeats)
    elif a.phase == 'budget':
        runtime.budget(*([a.drills] if a.drills else []))
    elif a.phase == 'settle':
        runtime.settle()
    elif a.phase == 'enrich-retrieval':
        runtime.enrich()
    elif a.phase == 'set-titles':
        runtime.set_titles()
    else:
        runtime.snapshot(a.out)


if __name__ == '__main__':
    main()
