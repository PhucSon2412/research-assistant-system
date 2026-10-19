"""Test-only sitecustomize: controlled HTTP transport, never imported by product.

Mounted outside the image on an internal-only network before fake pricing/key.
Real adapters still validate identities, dimensions and usage; real accounting
and every authentication/content boundary execute in the application image.
"""
import json
import os
import ipaddress
from pathlib import Path
import httpx

original = httpx.HTTPTransport.handle_request


def controlled(self, request):
    host = request.url.host
    if host != 'openrouter.ai':
        try: allowed = ipaddress.ip_address(host).is_loopback
        except ValueError: allowed = host in ('api','web','db','minio','redis','testserver')
        if not allowed: raise httpx.ConnectError('test outbound denied', request=request)
        return original(self, request)
    if request.headers.get('authorization') != 'Bearer c4-synthetic-controlled-provider':
        raise AssertionError('fake transport requires the synthetic key')
    body = json.loads(request.content)
    path = request.url.path
    policy = body.get('provider') or {}
    if policy.get('allow_fallbacks') is not False or policy.get('zdr') is not True or not policy.get('only'):
        raise AssertionError('fake transport requires the pinned no-fallback ZDR provider policy')
    log = os.environ.get('C4_FAKE_CALL_LOG')
    if log:
        with Path(log).open('a', encoding='utf-8') as output:
            output.write(json.dumps({'path':path, 'model':body['model']})+'\n')
    if path.endswith('/embeddings'):
        data = {'model':'text-embedding-3-small','provider':'Azure',
                'data':[{'embedding':[0.1,0.2,0.3]}], 'usage':{'prompt_tokens':1}}
    elif path.endswith('/rerank'):
        data = {'model':'accounts/fireworks/models/qwen3-reranker-8b','provider':'Fireworks',
                'results':[{'index':i,'relevance_score':1/(i+1)} for i in range(len(body['documents']))],
                'usage':{'total_tokens':1,'search_units':1}}
    elif path.endswith('/chat/completions'):
        evidence = json.loads(body['messages'][1]['content'])['evidence']
        quote = evidence[0]['text'][:120]
        claims = {'claims':[{'text':quote,'citations':[{'chunk_id':evidence[0]['chunk_id'],'quote':quote}]}]}
        data = {'object':'chat.completion','model':body['model'],
            'openrouter_metadata':{'requested':body['model'],'endpoints':{'available':[
                {'selected':True,'provider':'Azure','model':body['model']}]}},
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps(claims)}}],
            'usage':{'prompt_tokens':1,'completion_tokens':1}}
    else:
        raise AssertionError('unapproved fake endpoint')
    return httpx.Response(200,json=data,request=request)


httpx.HTTPTransport.handle_request = controlled
