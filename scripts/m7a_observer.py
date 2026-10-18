"""Test-only sitecustomize for the M7a live runtime smoke; never imported by product code.

Mounted outside the image. It delegates every OpenRouter request to the real
transport unchanged and keeps a bounded receipt (path, status, id, model,
provider, usage/cost, finish reason): no prompt, vector, answer, header or key.
A request leaves only while the host-written allowance is positive; otherwise it
is refused before any byte is sent (the adapter sees a transport error).
"""
import fcntl
import json
import os
import time

import httpx

_original = httpx.HTTPTransport.handle_request
_KEEP = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'search_units', 'cost', 'is_byok')


def _bounded(value):
    return value if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 255 else None


def _append(row):
    with open(os.environ['M7A_RECEIPTS'], 'a', encoding='utf-8') as out:
        out.write(json.dumps(row, sort_keys=True) + '\n')
        out.flush()
        os.fsync(out.fileno())


def _take_allowance():
    with open(os.environ['M7A_ALLOWANCE'], 'r+', encoding='utf-8') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        left = int(handle.read().strip() or 0)
        if left <= 0:
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(left - 1))
        handle.flush()
        os.fsync(handle.fileno())
        return True


def observed(self, request):
    if request.url.host != 'openrouter.ai':
        return _original(self, request)
    if not _take_allowance():
        _append({'event': 'refused', 'path': request.url.path, 'at': time.time()})
        raise httpx.ConnectError('m7a allowance exhausted; not sent', request=request)
    _append({'event': 'sending', 'path': request.url.path, 'at': time.time(), 'pid': os.getpid()})
    try:
        response = _original(self, request)
        response.read()
    except Exception as exc:
        _append({'event': 'transport_error', 'path': request.url.path, 'error': type(exc).__name__, 'at': time.time()})
        raise
    row = {'event': 'response', 'path': request.url.path, 'status': response.status_code, 'at': time.time()}
    try:
        body = json.loads(response.content)
    except ValueError:
        body = None
    if isinstance(body, dict):
        row.update({k: _bounded(body.get(k)) for k in ('id', 'model', 'provider')})
        usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
        row['usage'] = {k: _bounded(usage.get(k)) for k in _KEEP if k in usage}
        meta = body.get('openrouter_metadata')
        if isinstance(meta, dict) and isinstance(meta.get('endpoints'), dict):
            row['selected'] = [{'provider': _bounded(e.get('provider')), 'model': _bounded(e.get('model'))}
                               for e in meta['endpoints'].get('available') or []
                               if isinstance(e, dict) and e.get('selected') is True]
        choices = body.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            row['finish_reason'] = _bounded(choices[0].get('finish_reason'))
        if isinstance(body.get('error'), dict):
            row['error_code'] = _bounded(body['error'].get('code'))
    _append(row)
    return response


httpx.HTTPTransport.handle_request = observed
