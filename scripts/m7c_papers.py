"""M7c open-licence arXiv corpus: discovery, exact-version licence check, checksum fetch, production parse.

    python scripts/m7c_papers.py discover --out <dir outside repo> [--max-pages 6] [--per-query 60]
    python scripts/m7c_papers.py license <id>v<N> [...]
    python scripts/m7c_papers.py fetch --dest <dir outside repo> [--manifest evaluation/m7c-papers/manifest.json]
    python scripts/m7c_papers.py parse --dest <dir> --image <API image> [--out <dir>]

Only arXiv is contacted (polite pacing, descriptive User-Agent); no provider, no key, no .env.
A version is accepted only when both the abs page of exactly that version and the OAI-PMH
arXivRaw record name CC BY 4.0, CC BY-SA 4.0 or CC0 1.0, and that version is the latest one
(the OAI licence is record level). The arXiv default licence, NC/ND variants or an unknown
licence are refused. PDFs and full text stay outside the repository; the repo keeps only the
manifest (ID/version/licence/attribution/SHA-256). ``parse`` runs the production PDF parser
inside the API image with no network, so page texts and locators are the product's own.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / 'evaluation' / 'm7c-papers' / 'manifest.json'
USER_AGENT = ('ras-m7c-corpus/1.0 (open-licence evaluation corpus of a research assistant MVP; '
              'https://github.com/PhucSon2412/research-assistant-system)')
PACE_S = 3.2  # arXiv asks for at most one request every three seconds
ACCEPTED = {
    'https://creativecommons.org/licenses/by/4.0/': 'CC BY 4.0',
    'https://creativecommons.org/licenses/by-sa/4.0/': 'CC BY-SA 4.0',
    'https://creativecommons.org/publicdomain/zero/1.0/': 'CC0 1.0',
}
ARXIV_ID = re.compile(r'^(\d{4}\.\d{4,5})v(\d+)$')
QUERIES = (
    'abs:"retrieval-augmented generation" AND abs:evaluation',
    'abs:"retrieval augmented generation" AND abs:citation',
    'abs:"retrieval-augmented generation" AND abs:hallucination',
    'abs:"retrieval-augmented" AND abs:"question answering" AND abs:scientific',
    'abs:"retrieval-augmented generation" AND abs:benchmark',
    'abs:"dense retrieval" AND abs:"scientific"',
    'abs:"retrieval-augmented generation" AND abs:reranking',
    'abs:"retrieval-augmented generation" AND abs:"long context"',
    'abs:"retrieval-augmented generation" AND abs:chunking',
    'abs:"RAG" AND abs:"faithfulness"',
    'abs:"retrieval-augmented generation" AND abs:"prompt injection"',
    'abs:"retrieval augmented" AND abs:"attribution"',
)


def normalise_licence(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    value = url.strip().lower().replace('http://', 'https://', 1)
    return value if value.endswith('/') else value + '/'


def licence_verdict(arxiv_id: str, version: int, abs_licence: str | None, oai_licence: str | None,
                    oai_versions: list[int]) -> dict:
    """Pure policy decision for one exact version; every disagreement or unknown refuses."""
    abs_url, oai_url = normalise_licence(abs_licence), normalise_licence(oai_licence)
    reasons = []
    if abs_url is None:
        reasons.append('abs_licence_unknown')
    elif abs_url not in ACCEPTED:
        reasons.append('abs_licence_not_open')
    if oai_url is None:
        reasons.append('oai_licence_unknown')
    elif oai_url not in ACCEPTED:
        reasons.append('oai_licence_not_open')
    if abs_url is not None and oai_url is not None and abs_url != oai_url:
        reasons.append('licence_sources_disagree')
    if not oai_versions or version != max(oai_versions):
        reasons.append('not_latest_version')
    return {'arxiv_id': arxiv_id, 'version': version, 'abs_licence': abs_licence, 'oai_licence': oai_licence,
            'accepted': not reasons, 'licence': ACCEPTED.get(abs_url) if not reasons else None,
            'licence_url': abs_url if not reasons else None, 'reasons': reasons}


class Arxiv:
    """One paced HTTP client for arxiv.org hosts only."""

    def __init__(self):
        self.client = httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=60, follow_redirects=True)
        self.last = 0.0

    def get(self, url: str, **params) -> httpx.Response:
        host = httpx.URL(url).host
        if host not in ('arxiv.org', 'export.arxiv.org', 'oaipmh.arxiv.org'):
            raise SystemExit('refusing non-arXiv host ' + host)
        wait = self.last + PACE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self.client.get(url, params=params or None)
        finally:
            self.last = time.monotonic()

    def close(self):
        self.client.close()


def abs_licence(arxiv: Arxiv, arxiv_id: str, version: int) -> str | None:
    response = arxiv.get(f'https://arxiv.org/abs/{arxiv_id}v{version}')
    if response.status_code != 200:
        return None
    match = re.search(r'<div class="abs-license">\s*<a href="([^"]+)"', response.text)
    return html.unescape(match.group(1)) if match else None


def oai_record(arxiv: Arxiv, arxiv_id: str) -> dict:
    response = arxiv.get('https://oaipmh.arxiv.org/oai', verb='GetRecord',
                         identifier='oai:arXiv.org:' + arxiv_id, metadataPrefix='arXivRaw')
    if response.status_code != 200:
        return {}
    ns = {'r': 'http://arxiv.org/OAI/arXivRaw/'}
    root = ET.fromstring(response.content)
    raw = root.find('.//r:arXivRaw', ns)
    if raw is None:
        return {}
    text = lambda tag: (raw.findtext('r:' + tag, default='', namespaces=ns) or '').strip()
    versions = [int(v.get('version', 'v0')[1:]) for v in raw.findall('r:version', ns)]
    return {'licence': text('license') or None, 'versions': versions, 'title': ' '.join(text('title').split()),
            'authors': ' '.join(text('authors').split()), 'comments': ' '.join(text('comments').split()),
            'categories': text('categories')}


def check(arxiv: Arxiv, tagged: str) -> dict:
    match = ARXIV_ID.match(tagged)
    if not match:
        raise SystemExit('expected <id>v<N>: ' + tagged)
    arxiv_id, version = match.group(1), int(match.group(2))
    record = oai_record(arxiv, arxiv_id)
    verdict = licence_verdict(arxiv_id, version, abs_licence(arxiv, arxiv_id, version), record.get('licence'),
                              record.get('versions', []))
    return {**verdict, 'title': record.get('title'), 'authors': record.get('authors'),
            'comments': record.get('comments'), 'categories': record.get('categories'),
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}


def pages_from_comment(comment: str) -> int | None:
    match = re.search(r'(\d+)\s*pages?', comment or '', re.I)
    return int(match.group(1)) if match else None


def discover(out: Path, max_pages: int, per_query: int, queries: list[str] | None = None) -> None:
    arxiv = Arxiv()
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    seen, rows = set(), []
    try:
        for query in queries or QUERIES:
            response = arxiv.get('https://export.arxiv.org/api/query', search_query=query, start=0,
                                 max_results=per_query, sortBy='relevance')
            for entry in ET.fromstring(response.content).findall('a:entry', ns):
                tagged = entry.findtext('a:id', namespaces=ns).rsplit('/', 1)[-1]
                comment = entry.findtext('x:comment', default='', namespaces=ns)
                pages = pages_from_comment(comment)
                if tagged in seen or pages is None or pages > max_pages:
                    continue
                seen.add(tagged)
                rows.append({'tagged': tagged, 'pages_comment': pages, 'comment': ' '.join(comment.split()),
                             'title': ' '.join(entry.findtext('a:title', namespaces=ns).split()),
                             'primary': entry.find('x:primary_category', ns).get('term'), 'query': query})
        (out / 'candidates.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
        print(f'{len(rows)} short candidates; checking licences', flush=True)
        checked = []
        for row in rows:
            checked.append({**row, **check(arxiv, row['tagged'])})
            (out / 'licences.json').write_text(json.dumps(checked, indent=2), encoding='utf-8')
            if checked[-1]['accepted']:
                print('accepted', row['tagged'], checked[-1]['licence'], row['title'][:80], flush=True)
    finally:
        arxiv.close()


def outside(path: Path) -> Path:
    path = path.resolve()
    if path == REPO or REPO in path.parents:
        raise SystemExit('PDFs and full text stay outside the repository')
    return path


def fetch(dest: Path, manifest: Path, recheck: bool) -> int:
    papers = json.loads(manifest.read_text(encoding='utf-8'))['papers']
    dest.mkdir(parents=True, exist_ok=True)
    arxiv, failures, report = Arxiv(), 0, []
    try:
        for paper in papers:
            tagged = f"{paper['arxiv_id']}v{paper['version']}"
            if recheck:
                verdict = check(arxiv, tagged)
                if not verdict['accepted'] or verdict['licence_url'] != paper['licence_url']:
                    failures += 1
                    report.append({'paper': tagged, 'status': 'licence_refused', 'reasons': verdict['reasons']})
                    continue
            target = dest / paper['filename']
            if not target.exists():
                response = arxiv.get(f'https://arxiv.org/pdf/{tagged}')
                if response.status_code != 200 or not response.content.startswith(b'%PDF'):
                    failures += 1
                    report.append({'paper': tagged, 'status': 'download_failed', 'http': response.status_code})
                    continue
                partial = target.with_suffix('.partial')
                partial.write_bytes(response.content)
                partial.replace(target)
            data = target.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            ok = digest == paper['sha256'] and len(data) == paper['bytes']
            failures += not ok
            report.append({'paper': tagged, 'status': 'ok' if ok else 'checksum_mismatch', 'sha256': digest,
                           'bytes': len(data)})
    finally:
        arxiv.close()
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


# Runs inside the API image (network none): production parser, classification and chunking.
PARSE_CHILD = r'''
import io, json, sys
from pathlib import Path
from types import SimpleNamespace
from pypdf import PdfReader
from app.config import Settings
from app.ingest import pdf
from app.ingest.markdown import chunk_hash, chunk_section
settings = Settings(_env_prefix='RAS_M7C_UNUSED_', db=None, database_url='postgresql+psycopg://unused@localhost/unused')
size = settings.ingest.markdown_max_chunk_chars
out = Path(sys.argv[2])
summary = []
for source in sorted(Path(sys.argv[1]).glob('*.pdf')):
    data = source.read_bytes()
    row = {'file': source.name, 'parser': pdf.PIPELINE_VERSION}
    try:
        pages = pdf.parse(data, len(PdfReader(io.BytesIO(data)).pages), settings)
        result = pdf.classify(pages, settings)
    except pdf.PdfError as exc:
        summary.append({**row, 'disposition': 'parser_error', 'error': exc.code})
        continue
    chunks = []
    for segment in result.segments:
        if segment.kind in ('body', 'abstract') and segment.text:
            page = result.pages[segment.page - 1]
            span = SimpleNamespace(kind=segment.kind, heading=None, start_offset=segment.start, end_offset=segment.end)
            for s in chunk_section(page.text, span, size):
                chunks.append({'page': page.number, 'kind': segment.kind, 'start': s.start_offset,
                               'end': s.end_offset, 'hash': chunk_hash(s.text)})
    row.update({'pages': len(result.pages), 'disposition': result.disposition, 'coverage': result.coverage,
                'warnings': result.warnings, 'body_pages': result.body_pages, 'deficient_pages': result.deficient_pages,
                'failed_pages': result.failed_pages, 'total_body_chars': result.total_body_chars,
                'chunks': len(chunks), 'distinct_chunk_hashes': len({c['hash'] for c in chunks})})
    summary.append(row)
    text = {'file': source.name, 'parser': pdf.PIPELINE_VERSION, 'disposition': result.disposition,
            'pages': [{'number': p.number, 'text': p.text} for p in result.pages],
            'segments': [{'page': s.page, 'kind': s.kind, 'start': s.start, 'end': s.end} for s in result.segments],
            'chunks': chunks}
    (out / (source.stem + '.parsed.json')).write_text(json.dumps(text, ensure_ascii=False), encoding='utf-8')
(out / 'parse-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps(summary))
'''


def parse(dest: Path, image: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python',
                             '-v', dest.as_posix() + ':/papers:ro', '-v', out.as_posix() + ':/parsed',
                             image, '-c', PARSE_CHILD, '/papers', '/parsed'], capture_output=True, text=True,
                            encoding='utf-8')
    if result.returncode:
        print(result.stderr[-2000:], file=sys.stderr)
        return result.returncode
    for row in json.loads(result.stdout.strip().splitlines()[-1]):
        print(json.dumps({k: row.get(k) for k in ('file', 'pages', 'disposition', 'coverage', 'chunks',
                                                   'distinct_chunk_hashes', 'warnings')}))
    return 0


QUESTIONS = REPO / 'evaluation' / 'm7c-papers' / 'questions.json'
GOLD_KEYS = ('paper', 'quote', 'page', 'start', 'end', 'segment')


def normalise_ws(text: str) -> tuple[str, list[int]]:
    """Whitespace runs collapse to one space; ``index[i]`` is the original offset of character ``i``."""
    out, index, space = [], [], False
    for position, char in enumerate(text):
        if char.isspace():
            if not space and out:
                out.append(' ')
                index.append(position)
            space = True
            continue
        out.append(char)
        index.append(position)
        space = False
    return ''.join(out), index


def locate_gold(parsed: dict, quote: str) -> list[dict]:
    """Every occurrence of ``quote`` (whitespace-insensitive) in the production page text."""
    wanted = ' '.join(quote.split())
    found = []
    for page in parsed['pages']:
        flat, index = normalise_ws(page['text'])
        start = flat.find(wanted)
        while start >= 0:
            a, b = index[start], index[start + len(wanted) - 1] + 1
            segments = [s['kind'] for s in parsed['segments'] if s['page'] == page['number'] and s['start'] <= a
                        and b <= s['end']]
            found.append({'page': page['number'], 'start': a, 'end': b, 'quote': page['text'][a:b],
                          'segment': segments[0] if segments else None})
            start = flat.find(wanted, start + 1)
    return found


def annotate(parsed_dir: Path, write: bool) -> int:
    """Fill (or with ``write`` False, check) page/offsets/exact quote of every gold span."""
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    files = {p['id']: p['filename'] for p in manifest['papers']}
    data = json.loads(QUESTIONS.read_text(encoding='utf-8'))
    parsed_cache, errors = {}, []
    for question in data['questions']:
        for gold in question['gold']:
            stem = Path(files[gold['paper']]).stem
            parsed = parsed_cache.setdefault(stem, json.loads((parsed_dir / (stem + '.parsed.json')).read_text(
                encoding='utf-8')))
            # An authored ``page`` pins a quote that also occurs on another page.
            hits = [h for h in locate_gold(parsed, gold['quote']) if h['segment'] in ('body', 'abstract')
                    and gold.get('page') in (None, h['page'])]
            if len(hits) != 1:
                errors.append(f"{question['id']}: {gold['paper']} quote found {len(hits)}x in indexed text: "
                              f"{gold['quote'][:60]!r}")
                continue
            hit = hits[0]
            if write:
                gold.update({'quote': hit['quote'], 'page': hit['page'], 'start': hit['start'], 'end': hit['end'],
                             'segment': hit['segment'], 'parser': parsed['parser']})
            elif any(gold.get(k) != hit[k] for k in ('quote', 'page', 'start', 'end', 'segment')) or \
                    gold.get('parser') != parsed['parser']:
                errors.append(f"{question['id']}: {gold['paper']} gold span differs from the parser text")
    for error in errors:
        print(error)
    if write and not errors:
        QUESTIONS.write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f"{len(data['questions'])} questions, {sum(len(q['gold']) for q in data['questions'])} gold spans: "
          + ('OK' if not errors else f'{len(errors)} error(s)'))
    return 1 if errors else 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='action', required=True)
    d = sub.add_parser('discover')
    d.add_argument('--out', type=Path, required=True)
    d.add_argument('--max-pages', type=int, default=6)
    d.add_argument('--per-query', type=int, default=60)
    d.add_argument('--query', action='append', help='replaces the built-in queries')
    lic = sub.add_parser('license')
    lic.add_argument('ids', nargs='+')
    f = sub.add_parser('fetch')
    f.add_argument('--dest', type=Path, required=True)
    f.add_argument('--manifest', type=Path, default=MANIFEST)
    f.add_argument('--no-recheck-licence', action='store_true')
    pa = sub.add_parser('parse')
    pa.add_argument('--dest', type=Path, required=True)
    pa.add_argument('--image', required=True)
    pa.add_argument('--out', type=Path)
    qa = sub.add_parser('questions')
    qa.add_argument('--parsed', type=Path, required=True)
    qa.add_argument('--write', action='store_true', help='annotate; default only checks')
    a = p.parse_args()
    if a.action == 'questions':
        return annotate(outside(a.parsed), a.write)
    if a.action == 'discover':
        out = outside(a.out)
        out.mkdir(parents=True, exist_ok=True)
        discover(out, a.max_pages, a.per_query, a.query)
        return 0
    if a.action == 'license':
        arxiv = Arxiv()
        try:
            for tagged in a.ids:
                print(json.dumps(check(arxiv, tagged)), flush=True)
        finally:
            arxiv.close()
        return 0
    if a.action == 'fetch':
        return fetch(outside(a.dest), a.manifest, not a.no_recheck_licence)
    dest = outside(a.dest)
    return parse(dest, a.image, outside(a.out) if a.out else dest / 'parsed')


if __name__ == '__main__':
    raise SystemExit(main())
