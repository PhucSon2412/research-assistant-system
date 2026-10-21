"""M7c live OpenRouter tooling: ledger with the task ceiling, read-only catalog, AI judge calls.

    python scripts/m7c_live.py --state <dir outside repo> ledger
    python scripts/m7c_live.py --state <dir> catalog [--judge-model <id> ...]
    python scripts/m7c_live.py --state <dir> reconcile [--deadline 300]

The key is read only from the owner's secret file (default
``%USERPROFILE%\\ras-pilot\\secrets\\openrouter_api_key``); never from .env or the
environment, never printed, logged or written. Task ceiling (orchestrator): $5.00 and
400 inference calls including the judge; new calls stop at 80 % of either. Every call is
recorded ``sending`` (fsynced) before it leaves; a call without a valid outcome counts at
its full ceiling (M7a ledger contract, reused unchanged).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import m7a_live  # noqa: E402

TASK_USD = Decimal('5.00')
TASK_REQUESTS = 400
STOP_FRACTION = Decimal('0.8')
KEY_FILE = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'ras-pilot' / 'secrets' / 'openrouter_api_key'
CHAT = 'https://openrouter.ai/api/v1/chat/completions'
MODELS = dict(m7a_live.MODELS)


def load_key(path: Path = KEY_FILE) -> str:
    """The owner's secret file only. No .env fallback, no environment variable."""
    path = Path(path)
    if path.name == '.env' or path.suffix == '.env':
        raise SystemExit('refusing to read an .env file for the key')
    try:
        key = path.read_text(encoding='utf-8').strip()
    except OSError:
        raise SystemExit('OpenRouter key file absent; live verification blocked') from None
    if not key or any(c.isspace() for c in key):
        raise SystemExit('OpenRouter key file malformed; live verification blocked')
    return key


class Ledger(m7a_live.Ledger):
    """M7a append-only ledger with the M7c ceiling: refuse any call once 80 % is reached."""

    usd, requests, stop = TASK_USD, TASK_REQUESTS, STOP_FRACTION

    def limits(self) -> tuple[Decimal, int]:
        return self.usd * self.stop, int(self.requests * self.stop)

    def admit(self, route: str, ceiling: Decimal, source: str) -> int:
        if not isinstance(ceiling, Decimal) or not ceiling.is_finite() or ceiling <= 0:
            raise SystemExit('request ceiling unknown; not sent')
        usd, requests = self.limits()
        current = self.state()
        if (current['requests'] + 1 > requests or current['committed_usd'] + ceiling > usd
                or current['requests'] + 1 > self.requests or current['committed_usd'] + ceiling > self.usd):
            raise SystemExit(f"task stop at 80% of ${self.usd}/{self.requests} calls reached "
                             f"(requests={current['requests']}, committed_usd={current['committed_usd']}); not sent")
        seq = current['slots'] + 1
        self.append({'event': 'sending', 'seq': seq, 'route': route, 'ceiling_usd': str(ceiling),
                     'ceiling_source': source, 'at': m7a_live.now()})
        return seq


def zdr_endpoints(fetcher, key) -> list[dict]:
    status, body = m7a_live._get(fetcher, '/endpoints/zdr', key)
    rows = body.get('data') if isinstance(body, dict) else None
    return [{k: row.get(k) for k in ('name', 'model_id', 'model_name', 'provider_name', 'tag', 'pricing',
                                     'context_length', 'max_completion_tokens', 'supported_parameters',
                                     'status', 'uptime_last_30m', 'quantization')}
            for row in rows or [] if isinstance(row, dict)] if status == 200 else []


def catalog(state: Path, key: str, judge_models: list[str]) -> dict:
    out = {'checked_at': m7a_live.now()}
    with m7a_live.Fetcher(key) as fetcher:
        out['zdr'] = zdr_endpoints(fetcher, key)
        for model in [*MODELS.values(), *judge_models]:
            status, body = m7a_live._get(fetcher, f'/models/{model}/endpoints', key)
            data = body.get('data') if isinstance(body, dict) else None
            out[model] = {'status': status, 'endpoints': [
                {k: e.get(k) for k in ('name', 'provider_name', 'tag', 'pricing', 'status', 'uptime_last_30m',
                                       'supported_parameters', 'context_length', 'max_completion_tokens')}
                for e in (data or {}).get('endpoints', [])] if isinstance(data, dict) else None}
        status, info = m7a_live._get(fetcher, '/key', key)
        data = info.get('data') if isinstance(info, dict) else {}
        out['key'] = {k: data.get(k) for k in ('usage', 'usage_daily', 'limit', 'limit_remaining', 'limit_reset',
                                               'is_free_tier')}
    text = m7a_live.redact(json.dumps(out, indent=2, default=str), key)
    (state / f'catalog-{int(time.time())}.json').write_text(text, encoding='utf-8')
    return json.loads(text)


class JudgeRoute:
    """A reviewed judge endpoint: model, pinned ZDR provider and the price ceilings of that endpoint."""

    def __init__(self, model: str, provider: str, provider_name: str, input_usd_per_m: Decimal,
                 output_usd_per_m: Decimal, max_input_tokens: int, max_output_tokens: int):
        self.model, self.provider, self.provider_name = model, provider, provider_name
        self.input_usd_per_m, self.output_usd_per_m = Decimal(input_usd_per_m), Decimal(output_usd_per_m)
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens

    def ceiling(self) -> Decimal:
        return (Decimal(self.max_input_tokens) * self.input_usd_per_m
                + Decimal(self.max_output_tokens) * self.output_usd_per_m) / Decimal(1_000_000)

    def body(self, messages: list[dict]) -> dict:
        # No temperature: the reviewed endpoint does not list it, and require_parameters would then
        # exclude every endpoint (live 404 in M7c). The model default is recorded in each record.
        return {'model': self.model, 'messages': messages, 'stream': False,
                'max_tokens': self.max_output_tokens, 'response_format': {'type': 'json_object'},
                'provider': {'only': [self.provider], 'allow_fallbacks': False, 'data_collection': 'deny',
                             'zdr': True, 'require_parameters': True,
                             'max_price': {'prompt': format(self.input_usd_per_m, 'f'),
                                           'completion': format(self.output_usd_per_m, 'f')}}}


def estimate_tokens(messages: list[dict]) -> int:
    """Upper estimate: never more tokens than UTF-8 bytes (plus framing)."""
    return sum(len(str(m.get('content', '')).encode('utf-8')) + 16 for m in messages)


class JudgeTransport:
    """Ledger-gated judge calls. Returns the parsed JSON content plus a bounded receipt."""

    def __init__(self, state: Path, key: str, route: JudgeRoute, ledger: Ledger | None = None):
        self.state, self.key, self.route = state, key, route
        self.ledger = ledger or Ledger(state)

    def __call__(self, messages: list[dict], label: str) -> dict:
        if estimate_tokens(messages) > self.route.max_input_tokens:
            raise SystemExit('judge prompt above the reviewed input bound; not sent')
        with self.ledger.exclusive():
            self.ledger.require_settled()
            seq = self.ledger.admit('judge:' + label, self.route.ceiling(), 'judge max_input x price + max_output x price')
            started = time.monotonic()
            outcome = {'status': 'uncertain', 'error_code': 'judge_unknown'}
            content = None
            try:
                with httpx.Client(timeout=120) as client:
                    response = client.post(CHAT, json=self.route.body(messages), headers={
                        'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                        'X-OpenRouter-Metadata': 'enabled'})
                body = response.json() if response.headers.get('content-type', '').startswith('application/json') else None
                receipt = judge_receipt(response.status_code, body)
                # The serving provider must be the pinned one (display name of the reviewed slug).
                provider_ok = self.route.provider_name in (receipt.get('selected_provider'), receipt.get('provider'))
                if response.status_code == 200 and receipt.get('finish_reason') == 'stop' and provider_ok:
                    content = body['choices'][0]['message']['content']
                    outcome = {'status': 'completed'}
                elif response.status_code == 200:
                    outcome = {'status': 'completed', 'error_code': 'judge_unusable_reply'}
                elif response.status_code in (400, 401, 402, 403, 404, 429):
                    outcome = {'status': 'rejected', 'error_code': f'judge_http_{response.status_code}'}
                else:
                    outcome = {'status': 'uncertain', 'error_code': f'judge_http_{response.status_code}'}
            except httpx.HTTPError as exc:
                receipt = {'transport_error': type(exc).__name__}
            cost = m7a_live.valid_cost((receipt.get('usage') or {}).get('cost'))
            if outcome['status'] == 'completed' and cost is None:
                outcome = {'status': 'uncertain', 'error_code': 'judge_cost_missing'}
            if outcome['status'] != 'completed' or outcome.get('error_code'):
                content = None  # only a clean, pinned, costed reply is ever judged
            record = {'event': 'outcome', 'seq': seq, 'route': 'judge:' + label, 'at': m7a_live.now(),
                      'elapsed_s': round(time.monotonic() - started, 2), 'observed': [receipt],
                      'provider_cost_usd': float(cost) if cost is not None else None, **outcome}
            self.ledger.append(json.loads(m7a_live.redact(json.dumps(record, default=str), self.key)))
        return {'content': content, 'receipt': receipt, 'seq': seq, 'status': outcome['status'],
                'error_code': outcome.get('error_code'), 'cost_usd': float(cost) if cost is not None else None}


def judge_receipt(status: int, body) -> dict:
    row = {'status': status}
    if not isinstance(body, dict):
        return row
    bounded = m7a_live.bounded
    row.update({k: bounded(body.get(k)) for k in ('id', 'model', 'provider')})
    usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
    row['usage'] = {k: bounded(usage.get(k)) for k in m7a_live.KEEP_USAGE if k in usage}
    details = usage.get('prompt_tokens_details') if isinstance(usage.get('prompt_tokens_details'), dict) else {}
    row['prompt_tokens_details'] = {k: bounded(v) for k, v in details.items() if isinstance(v, (int, float))}
    meta = body.get('openrouter_metadata')
    endpoints = meta.get('endpoints') if isinstance(meta, dict) and isinstance(meta.get('endpoints'), dict) else {}
    selected = [e for e in endpoints.get('available') or [] if isinstance(e, dict) and e.get('selected') is True]
    row['selected_provider'] = bounded(selected[0].get('provider')) if len(selected) == 1 else None
    choices = body.get('choices')
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        row['finish_reason'] = bounded(choices[0].get('finish_reason'))
    if isinstance(body.get('error'), dict):
        row['error'] = {'code': bounded(body['error'].get('code')),
                        'message': bounded(str(body['error'].get('message'))[:200], 200)}
    return row


def found_so_far(state: Path) -> dict:
    """Provider usage already read in earlier reconcile passes (read-only data, merged by ID)."""
    found = {}
    for path in sorted(state.glob('reconcile-*.json')):
        found.update(json.loads(path.read_text(encoding='utf-8')).get('found') or {})
    return found


def reconcile(state: Path, key: str, deadline_s: int) -> dict:
    """Read-only ``/generation`` lookups for every ledger ID not found by an earlier pass (<= deadline_s).

    Same bounded GET as M7a (killable child, absolute deadline); a pass ends at its deadline and the
    next pass continues with the IDs still pending. Keeps every scalar field of the provider record."""
    ledger = Ledger(state)
    ids = sorted({r.get('id') for row in ledger.records() if row['event'] == 'outcome'
                  for r in row.get('observed', []) if isinstance(r.get('id'), str) and r['id'].startswith('gen-')})
    before = found_so_far(state)
    wanted = [i for i in ids if i not in before]
    found, end = {}, time.monotonic() + min(deadline_s, 300)
    with m7a_live.Fetcher(key) as fetcher:
        for gid in wanted:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            try:
                status, body = m7a_live._get(fetcher, '/generation', key, {'id': gid}, timeout=min(30.0, remaining))
            except (m7a_live.DeadlineExceeded, httpx.TransportError):
                continue
            data = body.get('data') if isinstance(body, dict) else None
            if status == 200 and isinstance(data, dict):
                found[gid] = {k: m7a_live.bounded(v) for k, v in data.items() if m7a_live.bounded(v) is not None}
    out = {'event': 'reconcile', 'at': m7a_live.now(), 'generation_ids': len(ids), 'found': found,
           'found_before': len(before), 'pending': [i for i in wanted if i not in found]}
    (state / f'reconcile-{time.time_ns()}.json').write_text(m7a_live.redact(json.dumps(out, indent=1, default=str), key),
                                                             encoding='utf-8')
    return {k: v for k, v in out.items() if k != 'found'} | {'found_now': len(found)}


def prompt_sha256(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--key-file', type=Path, default=KEY_FILE)
    p.add_argument('action', choices=['ledger', 'catalog', 'reconcile'])
    p.add_argument('--judge-model', action='append', default=[])
    p.add_argument('--deadline', type=int, default=300)
    a = p.parse_args()
    state = a.state.resolve()
    ledger = Ledger(state)
    if a.action == 'ledger':
        print(json.dumps({**ledger.state(), 'stop_at': [str(x) for x in ledger.limits()]}, default=str))
        return 0
    key = load_key(a.key_file)
    if a.action == 'catalog':
        result = catalog(state, key, a.judge_model)
        print(json.dumps({'zdr_rows': len(result['zdr']), 'key': result['key'],
                          **{m: [e.get('provider_name') for e in (result[m]['endpoints'] or [])]
                             for m in [*MODELS.values(), *a.judge_model]}}, default=str))
        return 0
    print(m7a_live.redact(json.dumps(reconcile(state, key, a.deadline), default=str), key))
    return 0


if __name__ == '__main__':
    sys.exit(main())
