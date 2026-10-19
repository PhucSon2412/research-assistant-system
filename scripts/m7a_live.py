"""M7a live OpenRouter verification with a hard experiment ceiling (synthetic data only).

Subcommands (all require --state <directory outside the repo>):
  catalog            read-only model/endpoint/key lookups, redacted
  probe ROUTE        one request through the production adapter, ledger-gated
  reconcile          read-only polling of provider-reported usage (<= 5 min)
  ledger             print the redacted ledger summary

The key comes from OPENROUTER_API_KEY or, when absent, only that one line of
the repo .env is read into memory. It is never printed, logged or written.
Every inference is recorded as ``sending`` (fsynced) before it leaves the
process; a record without an outcome counts at its full ceiling forever.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BASE = 'https://openrouter.ai/api/v1'
TASK_USD = Decimal('0.50')
TASK_REQUESTS = 20
ROUTES = ('embedding-document', 'embedding-query', 'rerank', 'generation')
MODELS = {'embedding': 'openai/text-embedding-3-small', 'rerank': 'qwen/qwen3-reranker-8b',
          'generation': 'openai/gpt-6-sol'}
# Response fields kept from a provider body: identifiers, identity, usage. Never text/vectors.
KEEP_USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'search_units', 'cost', 'is_byok')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_key() -> str:
    key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not key:
        env = REPO / '.env'
        if env.exists():
            for line in env.read_text(encoding='utf-8').splitlines():
                name, sep, value = line.partition('=')
                if sep and name.strip() == 'OPENROUTER_API_KEY':
                    key = value.strip().strip('"').strip("'")
                    break
    if not key:
        raise SystemExit('OpenRouter key absent; live verification blocked')
    return key


def redact(text: str, key: str) -> str:
    return text.replace(key, '<redacted>') if key else text


def valid_cost(value) -> Decimal | None:
    """Provider-reported USD usable to release a ceiling: finite, non-negative, int/float, never bool.

    One contract for probe/runtime producers and for every ledger read; anything else is
    unknown, so the full ceiling stays committed (never clipped to zero)."""
    if type(value) not in (int, float):
        return None
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def bounded(value, limit=255):
    return value if isinstance(value, (str, int, float, bool)) and len(str(value)) <= limit else None


class Ledger:
    """Append-only experiment manifest outside the repo; one process at a time."""

    def __init__(self, state: Path):
        if state == REPO or REPO in state.parents:
            raise SystemExit('state directory must be outside the repository')
        state.mkdir(parents=True, exist_ok=True)
        self.path, self.lock = state / 'ledger.jsonl', state / 'ledger.lock'

    @contextlib.contextmanager
    def exclusive(self):
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit('ledger lock present: another run may be active; resolve manually') from None
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            self.lock.unlink(missing_ok=True)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def append(self, record: dict) -> None:
        with self.path.open('a', encoding='utf-8') as out:
            out.write(json.dumps(record, sort_keys=True) + '\n')
            out.flush()
            os.fsync(out.fileno())

    def state(self) -> dict:
        sent, outcomes = {}, {}
        for row in self.records():
            (sent if row['event'] == 'sending' else outcomes)[row['seq']] = row
        spent, requests = Decimal(0), 0
        for seq, row in sent.items():
            outcome = outcomes.get(seq)
            if outcome and outcome['status'] == 'not_sent' and outcome.get('proof') == 'permit_revoked_by_host':
                # Only a runtime permit the host itself revoked unclaimed was provably never usable.
                continue
            requests += 1
            ceiling = Decimal(row['ceiling_usd'])
            billed = valid_cost(outcome.get('provider_cost_usd')) if outcome else None
            # A valid provider-reported cost of a completed call counts as is; anything else
            # (missing, negative, NaN/Infinity, bool, wrong type, uncertain) at the full ceiling.
            spent += billed if billed is not None and outcome['status'] == 'completed' else ceiling
        return {'requests': requests, 'committed_usd': spent, 'slots': len(sent),
                'open': sorted(seq for seq in sent if seq not in outcomes)}

    def require_settled(self) -> None:
        """Fail closed when an earlier request of this experiment has no outcome yet."""
        current = self.state()
        if current['open']:
            raise SystemExit(f"unsettled earlier request(s) {current['open']}; resolve manually, not sent")

    def admit(self, route: str, ceiling: Decimal, source: str) -> int:
        if not isinstance(ceiling, Decimal) or not ceiling.is_finite() or ceiling <= 0:
            raise SystemExit('request ceiling unknown; not sent')
        current = self.state()
        if current['requests'] + 1 > TASK_REQUESTS or current['committed_usd'] + ceiling > TASK_USD:
            raise SystemExit(f"task ceiling reached (requests={current['requests']}, "
                             f"committed_usd={current['committed_usd']}); not sent")
        seq = current['slots'] + 1
        self.append({'event': 'sending', 'seq': seq, 'route': route, 'ceiling_usd': str(ceiling),
                     'ceiling_source': source, 'at': now()})
        return seq


class Observer(httpx.BaseTransport):
    """Delegates to the real transport; keeps bounded identifiers/usage of OpenRouter bodies."""

    def __init__(self, inner: httpx.BaseTransport, sink: list):
        self.inner, self.sink = inner, sink

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.url.host == 'openrouter.ai':
            response.read()
            row = {'path': request.url.path, 'status': response.status_code}
            try:
                body = json.loads(response.content)
            except ValueError:
                body = None
            if isinstance(body, dict):
                row.update({k: bounded(body.get(k)) for k in ('id', 'model', 'provider', 'object')})
                usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
                row['usage'] = {k: bounded(usage.get(k)) for k in KEEP_USAGE if k in usage}
                details = usage.get('cost_details') if isinstance(usage.get('cost_details'), dict) else {}
                row['cost_details'] = {k: bounded(v) for k, v in details.items() if isinstance(v, (int, float))}
                meta = body.get('openrouter_metadata')
                if isinstance(meta, dict):
                    endpoints = (meta.get('endpoints') or {}).get('available') if isinstance(meta.get('endpoints'), dict) else None
                    row['metadata'] = {'requested': bounded(meta.get('requested')), 'strategy': bounded(meta.get('strategy')),
                        'region': bounded(meta.get('region')),
                        'selected': [{'provider': bounded(e.get('provider')), 'model': bounded(e.get('model'))}
                                     for e in endpoints or [] if isinstance(e, dict) and e.get('selected') is True]}
                choices = body.get('choices')
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    row['finish_reason'] = bounded(choices[0].get('finish_reason'))
                if isinstance(body.get('error'), dict):
                    row['error'] = {'code': bounded(body['error'].get('code')),
                                    'message': bounded(str(body['error'].get('message'))[:200], 200)}
            self.sink.append(row)
        return response


@contextlib.contextmanager
def observed(sink: list):
    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        kwargs['transport'] = Observer(httpx.HTTPTransport(), sink)
        original(self, *args, **kwargs)
    httpx.Client.__init__ = init
    try:
        yield
    finally:
        httpx.Client.__init__ = original


def _get(client, path, key, params=None, timeout=30.0):
    response = client.get(BASE + path, params=params, headers={'Authorization': f'Bearer {key}'}, timeout=timeout)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def catalog(state: Path, key: str) -> dict:
    out = {'checked_at': now(), 'requests': []}
    with httpx.Client(timeout=30) as client:
        status, models = _get(client, '/models', key)
        out['requests'].append(['/models', status])
        status, embeddings = _get(client, '/embeddings/models', key)
        out['requests'].append(['/embeddings/models', status])
        rows = {}
        for listing in (models, embeddings):
            for row in (listing or {}).get('data', []) if isinstance(listing, dict) else []:
                if row.get('id') in MODELS.values():
                    rows[row['id']] = {'pricing': row.get('pricing'), 'context_length': row.get('context_length'),
                        'supported_parameters': row.get('supported_parameters'),
                        'architecture': row.get('architecture')}
        for task, model in MODELS.items():
            entry = rows.get(model)
            status, endpoints = _get(client, f'/models/{model}/endpoints', key)
            out['requests'].append([f'/models/{model}/endpoints', status])
            data = endpoints.get('data') if isinstance(endpoints, dict) else None
            out[task] = {'model': model, 'listed': entry is not None, 'listing': entry,
                'endpoints_status': status,
                'endpoints': [{k: e.get(k) for k in ('name', 'provider_name', 'tag', 'pricing', 'status', 'uptime_last_30m',
                               'supported_parameters', 'quantization', 'context_length', 'max_completion_tokens')}
                              for e in (data or {}).get('endpoints', [])] if isinstance(data, dict) else None}
        status, info = _get(client, '/key', key)
        out['requests'].append(['/key', status])
        data = info.get('data') if isinstance(info, dict) else {}
        out['key'] = {k: data.get(k) for k in ('usage', 'usage_daily', 'limit', 'limit_remaining', 'limit_reset',
                                               'is_free_tier', 'byok_usage_daily', 'include_byok_in_limit')}
        out['key']['has_effective_budget'] = data.get('effective_budget') is not None
    text = redact(json.dumps(out, indent=2, default=str), key)
    (state / f'catalog-{int(time.time())}.json').write_text(text, encoding='utf-8')
    return json.loads(text)


DOC = ('Synthetic note M7A-DOC: the Lumen calibration bench measured a drift of 0.4 percent '
       'over twelve hours when the enclosure stayed at 21 degrees.')
QUERY = 'How much drift did the Lumen calibration bench measure?'
RERANK_DOCS = ['The Lumen calibration bench measured a drift of 0.4 percent over twelve hours.',
               'A synthetic orchard survey counted forty pear trees in the north field.',
               'The enclosure temperature was held at 21 degrees during the bench run.']


def settings_for(key: str):
    sys.path.insert(0, str(REPO / 'api'))
    from pydantic import SecretStr
    from app.config import Settings
    # Explicit synthetic settings: never reads RAS_* env; every pricing flag stays false.
    s = Settings(_env_prefix='RAS_M7A_UNUSED_', db=None, database_url='postgresql+psycopg://unused@localhost/unused',
                 embedding={'api_key': SecretStr(key)})
    assert not (s.embedding.pricing_verified or s.rerank.pricing_verified or s.generation.pricing_verified)
    return s


def ceiling_for(route: str, s) -> tuple[Decimal, str]:
    if route.startswith('embedding'):
        # Tokens never exceed request bytes; the configured per-token ceiling bounds the price.
        return (Decimal(s.embedding.max_request_bytes) * Decimal(str(s.embedding.max_usd_per_million_tokens))
                / Decimal(1_000_000), 'embedding.max_request_bytes x max_usd_per_million_tokens')
    if route == 'rerank':
        return Decimal(str(s.rerank.max_request_usd)), 'rerank.max_request_usd (catalog lists price 0)'
    return Decimal(str(s.generation.max_request_usd)), 'generation.max_request_usd'


def run_route(route: str, s) -> dict:
    from app.ingest.embedding import EmbeddingError, embed
    from app.rag import GenerationError, _messages, _validate_answer, generate
    from app.search import RerankError, rerank, rerank_order
    import uuid
    checks = {}
    try:
        if route.startswith('embedding'):
            result = embed(DOC if route == 'embedding-document' else QUERY, s,
                           input_type='search_document' if route == 'embedding-document' else 'search_query')
            checks = {'dimensions': len(result.vector),
                      'finite': all(math.isfinite(v) for v in result.vector),
                      'response_model': result.response_model_id, 'response_provider': result.response_provider,
                      'prompt_tokens': result.input_tokens}
        elif route == 'rerank':
            result = rerank(QUERY, RERANK_DOCS, s)
            ids = [uuid.uuid4() for _ in RERANK_DOCS]
            order = rerank_order(ids, result.results)
            checks = {'complete_order': len(order) == len(RERANK_DOCS), 'top_index': ids.index(order[0]),
                      'response_model': result.response_model_id, 'response_provider': result.response_provider,
                      'total_tokens': result.tokens, 'search_units': result.units}
        else:
            chunk = uuid.uuid4()
            evidence = {chunk: {'item_id': uuid.uuid4(), 'version_id': uuid.uuid4(), 'file_id': uuid.uuid4(),
                'title': 'Synthetic Lumen bench note', 'locator_kind': 'markdown', 'start_page': None, 'end_page': None,
                'start_line': 3, 'end_line': 3, 'start_offset': 0, 'end_offset': len(DOC), 'locator_detail': None,
                'coverage': 'complete', 'text': DOC, 'visibility': 'private'}}
            result = generate(_messages(QUERY, evidence), s)
            claims = _validate_answer(result.content, evidence, s)
            checks = {'answer_valid': claims is not None, 'claims': len(claims or []),
                      'response_model': result.response_model_id, 'response_provider': result.response_provider,
                      'input_tokens': result.input_tokens, 'output_tokens': result.output_tokens,
                      'synthetic_answer': result.content[:1500]}
        return {'status': 'completed', 'checks': checks}
    except (EmbeddingError, RerankError, GenerationError) as exc:
        code = exc.code
        uncertain = 'uncertain' in code or 'invalid' in code
        return {'status': 'uncertain' if uncertain else 'rejected', 'error_code': code,
                'checks': {'response_model': exc.response_model_id, 'response_provider': exc.response_provider}}
    except ValueError as exc:
        return {'status': 'invalid_mapping', 'error_code': type(exc).__name__}


def probe(state: Path, key: str, route: str) -> dict:
    ledger = Ledger(state)
    s = settings_for(key)
    ceiling, source = ceiling_for(route, s)
    with ledger.exclusive():
        ledger.require_settled()
        seq = ledger.admit(route, ceiling, source)
        sink: list = []
        started = time.monotonic()
        try:
            with observed(sink):
                outcome = run_route(route, s)
        except BaseException as exc:  # unknown outcome: the ceiling stays committed
            outcome = {'status': 'uncertain', 'error_code': 'probe_' + type(exc).__name__}
        seen = [r for r in sink if r['path'].startswith('/api/v1/') and r['path'] != '/api/v1/generation']
        reported = seen[0].get('usage', {}).get('cost') if len(seen) == 1 else None
        cost = valid_cost(reported)
        record = {'event': 'outcome', 'seq': seq, 'route': route, 'at': now(), 'elapsed_s': round(time.monotonic() - started, 2),
                  'http_requests': len(seen), 'observed': seen,
                  'provider_cost_usd': float(cost) if cost is not None else None,
                  'provider_cost_valid': cost is not None, **outcome}
        ledger.append(json.loads(redact(json.dumps(record, default=str), key)))
    return record


def reconcile(state: Path, key: str, deadline_s: int) -> dict:
    """Read-only: provider usage per generation id, plus current-key totals. <= deadline_s."""
    ledger = Ledger(state)
    ids = sorted({r.get('id') for row in ledger.records() if row['event'] == 'outcome'
                  for r in row.get('observed', []) if isinstance(r.get('id'), str) and r['id'].startswith('gen-')})
    found, delay, end = {}, 2.0, time.monotonic() + min(deadline_s, 300)
    remaining = lambda: end - time.monotonic()
    expired = False
    status, info = None, None
    # Every GET and sleep fits inside the deadline: each starts only with time left and
    # gets at most that time; /key is skipped once the deadline passed.
    with httpx.Client(timeout=30) as client:
        while ids and not expired and len(found) < len(ids):
            for gid in [i for i in ids if i not in found]:
                if remaining() <= 0:
                    expired = True
                    break
                try:
                    status, body = _get(client, '/generation', key, {'id': gid}, timeout=min(30.0, remaining()))
                except httpx.TransportError:
                    continue
                data = body.get('data') if isinstance(body, dict) else None
                if status == 200 and isinstance(data, dict):
                    found[gid] = {k: bounded(data.get(k)) for k in ('model', 'provider_name', 'total_cost', 'usage',
                        'upstream_inference_cost', 'tokens_prompt', 'tokens_completion', 'native_tokens_prompt',
                        'native_tokens_completion', 'native_tokens_reasoning', 'api_type', 'finish_reason',
                        'cancelled', 'is_byok', 'created_at')}
            if expired or len(found) == len(ids):
                break
            if remaining() <= 0:
                expired = True
                break
            time.sleep(min(delay, remaining()))
            delay = min(delay * 2, 30)
        status = None
        if remaining() > 0:
            try:
                status, info = _get(client, '/key', key, timeout=min(30.0, remaining()))
            except httpx.TransportError:
                status = 'transport_error'
        else:
            expired, status = True, 'skipped_deadline'
    data = info.get('data') if isinstance(info, dict) else {}
    out = {'event': 'reconcile', 'at': now(), 'generation_ids': ids, 'found': found,
           'pending': [i for i in ids if i not in found], 'deadline_s': min(deadline_s, 300), 'deadline_reached': expired,
           'key': {k: data.get(k) for k in ('usage', 'usage_daily', 'limit_remaining')}, 'key_status': status}
    (state / f'reconcile-{int(time.time())}.json').write_text(redact(json.dumps(out, indent=2, default=str), key),
                                                               encoding='utf-8')
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('action', choices=['catalog', 'ledger', 'probe', 'reconcile'])
    p.add_argument('route', nargs='?', choices=ROUTES)
    p.add_argument('--deadline', type=int, default=300)
    a = p.parse_args()
    state = a.state.resolve()
    ledger = Ledger(state)
    if a.action == 'ledger':
        print(json.dumps(ledger.state(), default=str))
        return 0
    key = load_key()
    if a.action == 'probe':
        if not a.route:
            raise SystemExit('route required')
        record = probe(state, key, a.route)
        shown = {k: record.get(k) for k in ('seq', 'route', 'status', 'error_code', 'provider_cost_usd', 'elapsed_s')}
        shown['checks'] = {k: v for k, v in record.get('checks', {}).items() if k != 'synthetic_answer'}
        shown['observed'] = [{k: r.get(k) for k in ('status', 'id', 'model', 'provider', 'usage', 'metadata', 'finish_reason', 'error')}
                             for r in record.get('observed', [])]
        print(redact(json.dumps(shown, default=str), key))
        print(json.dumps(ledger.state(), default=str))
        return 0
    if a.action == 'reconcile':
        print(redact(json.dumps(reconcile(state, key, a.deadline), default=str), key))
        return 0
    result = catalog(state, key)
    print(json.dumps({t: {'listed': result[t]['listed'], 'endpoints_status': result[t]['endpoints_status']}
                      for t in MODELS}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
