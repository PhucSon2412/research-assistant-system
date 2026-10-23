"""M11: checkpointed, paced arXiv corpus download. PDFs/evidence stay outside Git.

discover --target 300; fetch rechecks the committed manifest, license and checksum.
No provider calls, application writes, credentials or .env reads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx

import m7c_papers as policy

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / 'evaluation/m11-papers/manifest.json'
TOPICS = {
    'Dense retrieval & embeddings': 'abs:"dense retrieval" OR abs:"text embeddings"',
    'Reranking & search': 'abs:reranking AND (abs:retrieval OR abs:"language model")',
    'Retrieval-augmented generation': 'abs:"retrieval-augmented generation"',
    'RAG evaluation & benchmarks': 'abs:"retrieval augmented" AND (abs:evaluation OR abs:benchmark)',
    'Grounding & hallucination': 'abs:hallucination AND (abs:grounding OR abs:retrieval)',
    'Long context & chunking': '(abs:"long context" OR abs:chunking) AND abs:retrieval',
    'Search agents & tools': 'abs:agent AND (abs:"web search" OR abs:"tool use" OR abs:retrieval)',
}
NS = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}


def topic_for(title, abstract):
    text = (title + ' ' + abstract).lower()
    llm = re.search(r'\b(?:llms?|rag|retrieval)\b|language models?|(?:text|sentence|multilingual) embeddings?', text)
    if not llm:
        return None
    rules = [
        ('Search agents & tools', r'(?:agent|tool use|tool-use).*(?:search|retriev)|(?:search|retriev).*agent'),
        ('Reranking & search', r'rerank|re-rank'),
        ('Dense retrieval & embeddings', r'dense retriev|text embedding|sentence embedding'),
        ('Long context & chunking', r'long.context|chunking|chunk boundar'),
        ('Grounding & hallucination', r'hallucinat|grounding|faithful'),
        ('RAG evaluation & benchmarks', r'(?:evaluat|benchmark).*(?:retriev|\brag\b)|(?:retriev|\brag\b).*(?:evaluat|benchmark)'),
        ('Retrieval-augmented generation', r'retrieval.augmented|retrieval augmented'),
    ]
    return next((topic for topic, pattern in rules if re.search(pattern, text, re.S)), None)


def bulk_candidates(arxiv):
    """OAI ListRecords supplies the same arXivRaw fields as GetRecord, in batches.

    The from date selects metadata updates (not submission dates). Each selected
    latest version is still cross-checked against its exact abs page.
    """
    namespace = {'r': 'http://arxiv.org/OAI/arXivRaw/', 'o': 'http://www.openarchives.org/OAI/2.0/'}
    groups = {topic: [] for topic in TOPICS}
    params = {'verb': 'ListRecords', 'metadataPrefix': 'arXivRaw', 'set': 'cs', 'from': '2026-09-01'}
    page = 0
    while True:
        cache = arxiv.evidence / f'oai-batch-{page:03}.xml'
        if cache.exists():
            raw_bytes = cache.read_bytes()
        else:
            raw_bytes = arxiv.get('https://oaipmh.arxiv.org/oai', **params).content
            cache.write_bytes(raw_bytes)
        root = ET.fromstring(raw_bytes)
        error = root.find('o:error', namespace)
        if error is not None:
            raise RuntimeError('OAI protocol error: ' + error.get('code', 'unknown'))
        for raw in root.findall('.//r:arXivRaw', namespace):
            get = lambda name: ' '.join((raw.findtext('r:' + name, default='', namespaces=namespace) or '').split())
            licence = policy.normalise_licence(get('license'))
            aid = get('id')
            if licence not in policy.ACCEPTED or not re.fullmatch(r'\d{4}\.\d{4,5}', aid):
                continue
            versions = [int(v.get('version')[1:]) for v in raw.findall('r:version', namespace)]
            if not versions:
                continue
            topic = topic_for(get('title'), get('abstract'))
            if topic:
                groups[topic].append({'tagged': f'{aid}v{max(versions)}', 'topic': topic,
                    'title': get('title'), 'authors': [get('authors')],
                    'oai': {'licence': get('license'), 'versions': versions,
                            'batch': cache.name, 'batch_sha256': hashlib.sha256(raw_bytes).hexdigest()}})
        count = sum(map(len, groups.values()))
        print(json.dumps({'oai_pages': page + 1, 'open_topic_candidates': count}), flush=True)
        token = root.findtext('.//o:resumptionToken', namespaces=namespace)
        if not token or count >= 650:
            break
        params = {'verb': 'ListRecords', 'resumptionToken': token}
        page += 1
    rows, seen = [], set()
    for index in range(max(map(len, groups.values()), default=0)):
        for group in groups.values():
            if index < len(group) and group[index]['tagged'] not in seen:
                rows.append(group[index])
                seen.add(group[index]['tagged'])
    save(arxiv.evidence / 'bulk-candidates.json', rows)
    return rows


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


class Arxiv(policy.Arxiv):
    def __init__(self, evidence):
        super().__init__()
        self.evidence = evidence
        self.client.headers['User-Agent'] = ('ras-m11-library/1.0 (personal open-licence research library; '
                                              'https://github.com/PhucSon2412/research-assistant-system)')

    def get(self, url, **params):
        for attempt in range(4):
            try:
                response = super().get(url, **params)
                if response.status_code == 200:
                    return response
                if response.status_code not in (429, 500, 502, 503, 504):
                    response.raise_for_status()
            except httpx.TransportError:
                if attempt == 3:
                    raise
            if attempt == 3:
                raise RuntimeError('arXiv unavailable; checkpoint preserved')
            time.sleep(min(45, 10 * (attempt + 1)))


def candidates(arxiv, dest):
    cache = dest / 'candidates.json'
    if cache.exists():
        return json.loads(cache.read_text(encoding='utf-8'))
    groups = []
    for topic, query in TOPICS.items():
        rows = []
        for order in ('relevance', 'submittedDate'):
            response = arxiv.get('https://export.arxiv.org/api/query', search_query=query,
                                 start=0, max_results=150, sortBy=order, sortOrder='descending')
            (arxiv.evidence / ('query-' + str(len(groups)) + '-' + order + '.xml')).write_bytes(response.content)
            for entry in ET.fromstring(response.content).findall('a:entry', NS):
                tagged = entry.findtext('a:id', namespaces=NS).rsplit('/', 1)[-1]
                if not policy.ARXIV_ID.fullmatch(tagged):
                    continue
                rows.append({'tagged': tagged, 'topic': topic,
                             'title': ' '.join(entry.findtext('a:title', namespaces=NS).split()),
                             'authors': [n.text for n in entry.findall('a:author/a:name', NS)]})
        groups.append(rows)
    # Round robin keeps all seven subjects represented when stopping at target.
    seen, rows = set(), []
    for index in range(max(map(len, groups))):
        for group in groups:
            if index < len(group) and group[index]['tagged'].split('v')[0] not in seen:
                row = group[index]
                seen.add(row['tagged'].split('v')[0])
                rows.append(row)
    save(cache, rows)
    return rows


def attribution(dest, papers, exclusions=None):
    text = '# Attribution — M11 open arXiv papers\n\nUnmodified PDFs. Licence checked for the exact version.\n\n'
    for p in papers:
        text += (f"- **{p['title']}** — {', '.join(p['authors'])}. "
                 f"https://arxiv.org/abs/{p['arxiv_id']}v{p['version']}; "
                 f"[{p['licence']}]({p['licence_url']}); `{p['filename']}`.\n")
    if exclusions:
        text += '\n## Downloaded topic exclusions — outside the target corpus\n\n'
        for p in exclusions:
            text += (f"- **{p['title']}** — {', '.join(p['authors'])}. "
                     f"https://arxiv.org/abs/{p['arxiv_id']}v{p['version']}; "
                     f"[{p['licence']}]({p['licence_url']}); `{p['filename']}`.\n")
    path = dest / 'ATTRIBUTION.md'
    marker = '<!-- M11 attribution -->'
    previous = path.read_text(encoding='utf-8') if path.exists() else ''
    previous = previous.split(marker, 1)[0].rstrip()
    path.write_text(previous + '\n\n' + marker + '\n\n' + text, encoding='utf-8')


def curate(dest, manifest):
    """Reapply the topic policy to cached OAI metadata; never delete PDFs/items.

    Exclusions remain auditable, including any upload IDs already accepted before
    a selection-policy correction. They never count toward the target corpus.
    """
    ns = {'r': 'http://arxiv.org/OAI/arXivRaw/'}
    records = {}
    for path in (dest / 'm11-evidence').glob('oai-batch-*.xml'):
        for raw in ET.fromstring(path.read_bytes()).findall('.//r:arXivRaw', ns):
            get = lambda name: raw.findtext('r:' + name, default='', namespaces=ns)
            records[get('id')] = (get('title'), get('abstract'))
    data = json.loads(manifest.read_text(encoding='utf-8'))
    out = manifest.with_name('topic-exclusions.json')
    excluded = json.loads(out.read_text(encoding='utf-8')) if out.exists() else []
    uploads_path = dest.parent / 'evidence/m11/uploads.json'
    uploads = json.loads(uploads_path.read_text(encoding='utf-8')) if uploads_path.exists() else {}
    kept = []
    for paper in data['papers']:
        record = records.get(paper['arxiv_id'])
        if record is not None and topic_for(*record) is None:
            receipt = uploads.get(paper['tagged'], {})
            excluded.append({**paper, 'reason': 'outside_llm_ir_topic_after_word_boundary_review',
                             'excluded_at': stamp(), 'uploaded_item_id': receipt.get('item_id')})
        else:
            kept.append(paper)
    data['papers'] = kept
    save(out, excluded)
    save(manifest, data)
    attribution(dest, kept, excluded)
    print(json.dumps({'retained': len(kept), 'topic_exclusions': len(excluded),
                      'already_uploaded_exclusions': [p['tagged'] for p in excluded if p.get('uploaded_item_id')]}))
    return 0


def discover(dest, manifest, target):
    evidence = dest / 'm11-evidence'
    evidence.mkdir(parents=True, exist_ok=True)
    arxiv = Arxiv(evidence)
    existing = json.loads(policy.MANIFEST.read_text(encoding='utf-8'))['papers']
    excluded_ids = {p['arxiv_id'] for p in existing}
    excluded_hashes = {p['sha256'] for p in existing}
    data = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {
        'task': 'M11', 'target_new_papers': target, 'created_at': stamp(), 'papers': []}
    checkpoint = evidence / 'decisions.json'
    decisions = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
    selected = {p['arxiv_id'] for p in data['papers']}
    exclusion_path = manifest.with_name('topic-exclusions.json')
    exclusions = json.loads(exclusion_path.read_text(encoding='utf-8')) if exclusion_path.exists() else []
    hashes = {p['sha256'] for p in data['papers']} | excluded_hashes
    try:
        rows = bulk_candidates(arxiv)
        print(json.dumps({'candidates': len(rows), 'downloaded': len(selected)}), flush=True)
        for row in rows:
            if len(selected) >= target:
                break
            tagged = row['tagged']
            aid, version = policy.ARXIV_ID.fullmatch(tagged).groups()
            if aid in selected or aid in excluded_ids or tagged in decisions:
                continue
            record = row['oai']
            verdict = policy.licence_verdict(aid, int(version),
                policy.abs_licence(arxiv, aid, int(version)), record['licence'], record['versions'])
            verdict.update({'title': row['title'], 'checked_at': stamp()})
            if not verdict['accepted']:
                decisions[tagged] = verdict
                save(checkpoint, decisions)
                continue
            filename = f'arxiv-{tagged}.pdf'
            path = dest / filename
            if not path.exists():
                response = arxiv.get(f'https://arxiv.org/pdf/{tagged}')
                if not response.content.startswith(b'%PDF'):
                    raise RuntimeError('non-PDF response; checkpoint preserved')
                tmp = path.with_suffix('.partial')
                tmp.write_bytes(response.content)
                tmp.replace(path)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            # Product FilePolicy: 30 MiB. Upload preflight also checks live config.
            if len(content) > 30 * 1024 * 1024 or digest in hashes:
                decisions[tagged] = {'accepted': False, 'reasons': ['too_large_or_duplicate_sha256']}
                save(checkpoint, decisions)
                continue
            paper = {**row, 'arxiv_id': aid, 'version': int(version), 'filename': filename,
                     'title': verdict['title'] or row['title'], 'licence': verdict['licence'],
                     'licence_url': verdict['licence_url'], 'sha256': digest, 'bytes': len(content),
                     'licence_checked_at': verdict['checked_at'],
                     'licence_evidence': {'abs': verdict['abs_licence'], 'oai': verdict['oai_licence'],
                                          'latest_version_required': True}}
            data['papers'].append(paper)
            selected.add(aid)
            hashes.add(digest)
            save(manifest, data)
            decisions[tagged] = verdict
            save(checkpoint, decisions)
            attribution(dest, data['papers'], exclusions)
            print(json.dumps({'downloaded': len(selected), 'tagged': tagged, 'topic': row['topic']}), flush=True)
    finally:
        arxiv.close()
    return 0 if len(selected) >= target else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['discover', 'fetch', 'curate'])
    parser.add_argument('--dest', type=Path, default=Path(os.environ['USERPROFILE']) / 'ras-pilot/demo-papers')
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    parser.add_argument('--target', type=int, default=300)
    args = parser.parse_args()
    dest = policy.outside(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    if args.action == 'fetch':
        return policy.fetch(dest, args.manifest, True)
    if args.action == 'curate':
        return curate(dest, args.manifest)
    return discover(dest, args.manifest, args.target)


if __name__ == '__main__':
    raise SystemExit(main())
