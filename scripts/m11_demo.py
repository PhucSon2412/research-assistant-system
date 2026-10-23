"""Post-ingest M11 inventory and fifteen product API smoke questions (not an evaluation).

No application mutations except paid search/RAG accounting. All calls share the M11
ledger, retain full ceilings and use the existing demo user. Raw answers/chunks stay
outside Git. Refuses to start until the upload run has finished.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

import m11_library as lib
import m11_papers as papers


def item_by_title(items, fragment):
    matches = [i for i in items if fragment.casefold() in (i['title'] or '').casefold()]
    if len(matches) != 1 or matches[0]['content_status'] != 'indexed':
        raise RuntimeError('question source absent/ambiguous/not indexed: ' + fragment)
    return matches[0]['id']


def questions(items, collections):
    specs = [
        ('single', 'Rank-K:', 'What is the role of test-time reasoning in Rank-K listwise reranking?'),
        ('single', 'HLATR:', 'How does HLATR combine retrieval and reranking information?'),
        ('single', 'TimeSeek:', 'How does TimeSeek evaluate the temporal reliability of agentic forecasters?'),
        ('single', 'SCAR:', 'What recall does SCAR achieve on boundary-fragmented queries, and with how many chunks?'),
        ('single', 'SciRet:', 'What effect did the MS MARCO-trained cross-encoder reranker have in SciRet?'),
        ('unanswerable', 'Rank-K:', 'What monthly subscription price does Rank-K charge?'),
        ('unanswerable', 'SCAR:', 'What is the exact date of the next SCAR commercial product launch?'),
        ('unanswerable', 'TimeSeek:', 'What is the exact production launch date promised for TimeSeek?'),
    ]
    rows = [{'kind': kind, 'path': '/rag/ask', 'body': {'item_id': item_by_title(items, title),
             'question': question, 'mode': 'hybrid'}} for kind, title, question in specs]
    rows += [
        {'kind': 'comparison', 'path': '/rag/ask', 'body': {'collection_id': collections['Reranking & search'],
         'question': 'Compare how Rank-K and HLATR rerank retrieved passages.', 'mode': 'hybrid'}},
        {'kind': 'comparison', 'path': '/rag/ask', 'body': {'scope': 'all',
         'question': 'Compare how SCAR and Structure-Aware Semantic Chunking with Title-Chain Prefixes address evidence split across chunk boundaries.', 'mode': 'hybrid'}},
        {'kind': 'comparison', 'path': '/rag/ask', 'body': {'scope': 'all',
         'question': 'Compare the reranking approaches evaluated in SciRet and Rank-K.', 'mode': 'hybrid'}},
    ]
    for topic, query in [
        ('Reranking & search', 'listwise reranking with test-time reasoning'),
        ('Dense retrieval & embeddings', 'dense retrieval and text embeddings'),
        ('Long context & chunking', 'long context chunk boundaries and semantic continuity'),
        ('Grounding & hallucination', 'hallucination risk uncertainty and grounded answers'),
    ]:
        rows.append({'kind': 'hybrid_search', 'path': '/search', 'body': {
            'query': query, 'mode': 'hybrid', 'rerank': True, 'collection_id': collections[topic]}})
    return rows


def inventory(api, items, uploads, collections, evidence):
    per_item = []
    baseline = {i['id'] for i in json.loads((evidence / 'library-before.json').read_text(encoding='utf-8'))}
    for item in items:
        chunks = 0
        if item['content_status'] == 'indexed':
            while True:
                page = api.request('GET', '/library/items/' + item['id'] + '/chunks',
                                   params={'limit': 100, 'offset': chunks})['chunks']
                chunks += len(page)
                if len(page) < 100:
                    break
        jobs = item['jobs']
        per_item.append({'item_id': item['id'], 'title': item['title'], 'baseline': item['id'] in baseline,
            'content_status': item['content_status'], 'jobs': [{k: j[k] for k in ('state', 'last_error_code')} for j in jobs],
            'pages': sum(f['page_count'] or 0 for f in item['files']),
            'original_bytes': sum(f['byte_size'] for f in item['files']), 'readable_chunks': chunks})
    counts = {}
    for name, cid in collections.items():
        counts[name] = api.request('GET', '/collections/' + cid + '/items', params={'limit': 1})['total']
    report = {'at': lib.now(), 'items': len(items), 'indexed': sum(i['content_status'] == 'indexed' for i in items),
              'new_upload_states': dict(Counter(r.get('state', r['status']) for r in uploads.values())),
              'pages': sum(i['pages'] for i in per_item),
              'original_bytes': sum(i['original_bytes'] for i in per_item),
              'readable_chunks': sum(i['readable_chunks'] for i in per_item),
              'collections': counts, 'papers': per_item}
    papers.save(evidence / 'inventory.json', report)
    return report


def run(root, inspect_only=False):
    evidence = root / 'evidence/m11'
    uploads = json.loads((evidence / 'uploads.json').read_text(encoding='utf-8'))
    if any(r.get('status') == 'sending' or r.get('item_id') and not r.get('terminal') for r in uploads.values()):
        raise RuntimeError('uploads still running; no post-ingest smoke yet')
    manifest = json.loads(papers.MANIFEST.read_text(encoding='utf-8'))['papers']
    if len(manifest) < 300 or len(uploads) != len(manifest):
        raise RuntimeError('corpus not fully processed yet')
    with lib.Ledger(evidence / 'cost').exclusive():
        api = lib.Api(root)
        try:
            items = api.inventory()
            collections = json.loads((evidence / 'collections.json').read_text(encoding='utf-8'))
            report = inventory(api, items, uploads, collections, evidence)
            print(json.dumps({k: v for k, v in report.items() if k != 'papers'}), flush=True)
            if inspect_only:
                return
            plan = questions(items, collections)
            papers.save(evidence / 'questions.json', plan)
            ledger = lib.Ledger(evidence / 'cost')
            settings = lib.config()
            prior_path = evidence / 'smoke.json'
            results = json.loads(prior_path.read_text(encoding='utf-8')) if prior_path.exists() else []
            if results:
                raise RuntimeError('smoke already has records; no automatic repeated paid questions')
            for index, spec in enumerate(plan, 1):
                ceiling = Decimal(str(settings['search']['max_query_request_usd'])) + Decimal(str(settings['rerank']['max_request_usd']))
                if spec['path'] == '/rag/ask':
                    ceiling += Decimal(str(settings['generation']['max_request_usd']))
                seq = ledger.admit('smoke:' + str(index), ceiling, 'query + rerank + optional generation configured caps')
                row = {'number': index, **spec, 'seq': seq, 'at': lib.now(), 'status': 'sending'}
                results.append(row)
                papers.save(prior_path, results)
                try:
                    response = api.request('POST', spec['path'], json=spec['body'])
                    papers.save(evidence / f'smoke-answer-{index:02}.json', response)
                    citations = [cite for claim in response.get('claims') or [] for cite in claim['citations']]
                    verified = 0
                    for cite in citations:
                        source = api.request('GET', '/search/chunks/' + cite['chunk_id'])
                        papers.save(evidence / ('source-' + cite['chunk_id'] + '.json'), source)
                        verified += bool(cite['quote'] in source['text']
                            and all(cite[k] == source[k] for k in ('item_id', 'version_id', 'source_file_id', 'start_page', 'end_page'))
                            and cite.get('original_url') == source.get('original_url') and source.get('original_url'))
                    row.update({'status': 'response', 'response_keys': sorted(response),
                                'outcome': response.get('status'), 'reason': response.get('reason'),
                                'claims': len(response.get('claims') or []),
                                'citations': len(citations), 'verified_citations': verified,
                                'rerank_status': response.get('rerank_status'),
                                'hits': len(response.get('hits') or response.get('results') or [])})
                except Exception as exc:
                    row.update({'status': 'error', 'error': str(exc)})
                ledger.append({'event': 'outcome', 'seq': seq, 'status': 'uncertain', 'at': lib.now(),
                               'note': 'full operation ceiling retained; no per-call receipt'})
                papers.save(prior_path, results)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            papers.save(evidence / 'shared-key-after-smoke.json', lib.shared_meter())
        finally:
            api.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(os.environ['USERPROFILE']) / 'ras-pilot')
    p.add_argument('--inventory-only', action='store_true')
    args = p.parse_args()
    run(args.root, args.inventory_only)
