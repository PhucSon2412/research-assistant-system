"""Primary arXiv Atom + live latest-version/abs licence checks for M11.

No OAI evidence is inferred. Unknown history, a newer version, NC/ND/default
licences, or disagreement between the two abs pages all fail closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

import m7c_papers as policy

NS = {'a': 'http://www.w3.org/2005/Atom'}


class Abs(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.metadata, self.versions, self.selected = {}, set(), set()
        self.history = self.licence_depth = self.strong = 0
        self.licence = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.metadata[attrs.get('name')] = attrs.get('content')
        if tag == 'div':
            self.history = self.history + 1 if self.history else int('submission-history' in attrs.get('class', '').split())
            self.licence_depth = self.licence_depth + 1 if self.licence_depth else int('abs-license' in attrs.get('class', '').split())
        if self.history and tag == 'strong':
            self.strong += 1
        if self.licence_depth and tag == 'a' and self.licence is None:
            self.licence = attrs.get('href')

    def handle_endtag(self, tag):
        if tag == 'strong' and self.strong:
            self.strong -= 1
        if tag == 'div':
            self.history = max(0, self.history - 1)
            self.licence_depth = max(0, self.licence_depth - 1)

    def handle_data(self, text):
        if self.history:
            values = {int(v) for v in re.findall(r'\[v(\d+)\]', text)}
            self.versions.update(values)
            if self.strong:
                self.selected.update(values)

    def is_latest(self, aid, version):
        return (self.metadata.get('citation_arxiv_id') in (aid, f'{aid}v{version}')
                and bool(self.versions) and max(self.versions) == version and self.selected == {version})


def blob(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('wb') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def verify(arxiv, row, *, allow_original_oai=False):
    tagged = row['tagged']
    match = policy.ARXIV_ID.fullmatch(tagged)
    if not match:
        raise ValueError('expected a versioned arXiv ID')
    aid, version = match.group(1), int(match.group(2))
    result = {'accepted': False, 'reasons': [], 'title': row['title'],
              'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    atom = row.get('atom')
    original_oai = row.get('oai') if allow_original_oai else None
    if atom:
        if atom['entry_id'] not in (f'http://arxiv.org/abs/{tagged}', f'https://arxiv.org/abs/{tagged}'):
            result['reasons'] = ['atom_version_mismatch']
            return result
    elif not original_oai or max(original_oai.get('versions', [0]), default=0) != version:
        result['reasons'] = ['metadata_version_unverifiable']
        return result
    latest = arxiv.get(f'https://arxiv.org/abs/{aid}')
    latest_page = Abs(latest.text)
    if latest.status_code != 200 or not latest_page.is_latest(aid, version):
        result['reasons'] = ['live_latest_version_mismatch_or_unknown']
        return result
    latest_licence = policy.normalise_licence(latest_page.licence)
    if latest_licence not in policy.ACCEPTED:
        result['reasons'] = ['latest_abs_licence_not_open']
        return result
    exact = arxiv.get(f'https://arxiv.org/abs/{tagged}')
    exact_page = Abs(exact.text)
    exact_licence = policy.normalise_licence(exact_page.licence)
    if exact.status_code != 200 or not exact_page.is_latest(aid, version):
        result['reasons'] = ['exact_abs_version_mismatch_or_unknown']
        return result
    if exact_licence not in policy.ACCEPTED or exact_licence != latest_licence:
        result['reasons'] = ['abs_licence_not_open_or_disagreement']
        return result
    evidence = {'method': 'atom_and_live_abs_latest' if atom else 'live_abs_recheck_original_oai',
                'abs': exact_page.licence, 'latest_abs': latest_page.licence,
                'latest_version': version, 'latest_version_required': True}
    for label, response in [('latest', latest), ('exact', exact)]:
        digest = hashlib.sha256(response.content).hexdigest()
        path = arxiv.evidence / 'abs-evidence' / f'{tagged}-{label}-{digest[:16]}.html'
        blob(path, response.content)
        evidence[label + '_sha256'] = digest
        evidence[label + '_cache'] = path.relative_to(arxiv.evidence).as_posix()
    if atom:
        evidence['atom_entry_id'] = atom['entry_id']
    result.update({'accepted': True, 'licence': policy.ACCEPTED[exact_licence],
                   'licence_url': exact_licence, 'abs_licence': exact_page.licence,
                   'licence_evidence': evidence})
    return result


def candidates(arxiv, target, queries, topic_for, save, pages=4):
    root = arxiv.evidence / ('atom-' + datetime.now(timezone.utc).date().isoformat())
    root.mkdir(parents=True, exist_ok=True)
    selected = {}
    count = 0
    for index, query in enumerate(queries.values()):
        for order in ('relevance', 'submittedDate'):
            for page in range(pages):
                params = {'search_query': query, 'start': page * 150, 'max_results': 150,
                          'sortBy': order, 'sortOrder': 'descending'}
                signature = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
                cache = root / (signature + '.xml')
                # The first M11 discovery retained the same primary Atom queries
                # before OAI selection. Metadata has no expiring pagination token;
                # every resulting candidate still needs two fresh abs checks.
                legacy = arxiv.evidence / f'query-{index}-{order}.xml'
                if page == 0 and not cache.exists() and legacy.exists():
                    cache = legacy
                try:
                    if cache.exists():
                        raw = cache.read_bytes()
                    else:
                        # One failed search page must not discard other topics or
                        # prevent processing already retained primary metadata.
                        response = arxiv.get('https://export.arxiv.org/api/query', attempts=1, **params)
                        response.raise_for_status()
                        raw = response.content
                    feed = ET.fromstring(raw)
                    if feed.tag != '{http://www.w3.org/2005/Atom}feed':
                        raise RuntimeError('unexpected Atom feed root')
                    entries = feed.findall('a:entry', NS)
                    if any('/api/errors' in e.findtext('a:id', default='', namespaces=NS) for e in entries):
                        raise RuntimeError('arXiv Atom error entry')
                except (httpx.HTTPError, RuntimeError, ET.ParseError) as exc:
                    failure = {'params': params, 'error_type': type(exc).__name__,
                               'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                    save(root / (signature + '.failure.json'), failure)
                    print(json.dumps({'atom_query_failed': signature, 'error_type': type(exc).__name__,
                                      'topic_candidates': len(selected)}), flush=True)
                    break
                if not cache.exists():
                    blob(cache, raw)
                    save(root / (signature + '.query.json'), params)
                for entry in entries:
                    uri = entry.findtext('a:id', default='', namespaces=NS)
                    tagged = uri.rsplit('/', 1)[-1]
                    match = policy.ARXIV_ID.fullmatch(tagged)
                    if not match:
                        # Legacy subject/YYMMNNN identifiers are outside this
                        # manifest's supported modern versioned ID grammar.
                        save(root / (signature + '.unsupported-id.json'),
                             {'entry_id': uri, 'reason': 'unsupported_atom_identifier',
                              'batch_sha256': hashlib.sha256(raw).hexdigest()})
                        continue
                    title = ' '.join(entry.findtext('a:title', default='', namespaces=NS).split())
                    abstract = entry.findtext('a:summary', default='', namespaces=NS)
                    topic = topic_for(title, abstract)
                    authors = [e.findtext('a:name', default='', namespaces=NS) for e in entry.findall('a:author', NS)]
                    if not topic or not title or not authors or any(not a.strip() for a in authors):
                        continue
                    aid, version = match.group(1), int(match.group(2))
                    if aid not in selected or version > int(selected[aid]['tagged'].rsplit('v', 1)[1]):
                        selected[aid] = {'tagged': tagged, 'title': title, 'topic': topic,
                            'authors': authors,
                            'atom': {'entry_id': uri, 'batch': cache.relative_to(arxiv.evidence).as_posix(),
                                     'batch_sha256': hashlib.sha256(raw).hexdigest()}}
                count += 1
                print(json.dumps({'atom_queries': count, 'topic_candidates': len(selected), 'target': target}), flush=True)
                if len(entries) < 150:
                    break
    groups = {topic: [] for topic in queries}
    for row in selected.values():
        groups[row['topic']].append(row)
    rows = [group[i] for i in range(max(map(len, groups.values()), default=0))
            for group in groups.values() if i < len(group)]
    save(arxiv.evidence / 'atom-candidates.json', rows)
    return rows
