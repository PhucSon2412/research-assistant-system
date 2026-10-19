"""Test-only sitecustomize for the M7a live runtime smoke; never imported by product code.

Mounted outside the image and installed only when M7A_OBSERVER=1. It delegates every
OpenRouter request to the real transport unchanged and keeps a bounded receipt (path,
status, id, model, provider, usage/cost, finish reason): no prompt, vector, answer,
header or key.

A request leaves only after this process claims one per-slot permit token of its route
family by an atomic rename ``permits/open/<token> -> permits/claimed/<token>``. The host
closes an action by renaming every still-open token to ``permits/revoked``; for each token
exactly one rename wins, so a token is either provably unused (revoked by the host) or
claimed (and then counted until a receipt proves its outcome). No cross-OS file lock.
"""
import json
import os
import time

import httpx

_original = httpx.HTTPTransport.handle_request
_KEEP = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'search_units', 'cost', 'is_byok')
FAMILIES = {'/api/v1/embeddings': 'embeddings', '/api/v1/rerank': 'rerank', '/api/v1/chat/completions': 'chat'}


def _bounded(value):
    return value if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 255 else None


def _append(row):
    with open(os.environ['M7A_RECEIPTS'], 'a', encoding='utf-8') as out:
        out.write(json.dumps(row, sort_keys=True) + '\n')
        out.flush()
        os.fsync(out.fileno())


def claim(family):
    """Atomically take one open permit of ``family``; None when none is open."""
    root = os.environ['M7A_PERMITS']
    try:
        names = sorted(os.listdir(os.path.join(root, 'open')))
    except FileNotFoundError:
        return None
    for name in names:
        if not name.endswith('.' + family):
            continue
        try:
            os.rename(os.path.join(root, 'open', name), os.path.join(root, 'claimed', name))
            return name
        except (FileNotFoundError, FileExistsError, PermissionError):
            continue  # the host revoked it or another claimer won
    return None


def observed(self, request):
    if request.url.host != 'openrouter.ai':
        return _original(self, request)
    family = FAMILIES.get(request.url.path)
    permit = claim(family) if family else None
    if permit is None:
        _append({'event': 'refused', 'path': request.url.path, 'at': time.time()})
        raise httpx.ConnectError('m7a permit unavailable; not sent', request=request)
    _append({'event': 'sending', 'permit': permit, 'path': request.url.path, 'at': time.time(), 'pid': os.getpid()})
    try:
        response = _original(self, request)
        response.read()
    except Exception as exc:
        _append({'event': 'transport_error', 'permit': permit, 'path': request.url.path,
                 'error': type(exc).__name__, 'at': time.time()})
        raise
    row = {'event': 'response', 'permit': permit, 'path': request.url.path, 'status': response.status_code,
           'at': time.time()}
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


def install():
    httpx.HTTPTransport.handle_request = observed


if os.environ.get('M7A_OBSERVER') == '1':
    install()
