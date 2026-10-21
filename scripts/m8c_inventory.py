"""M8c release candidate: full API inventory on isolated shards against one frozen API image.

    python scripts/m8c_inventory.py --context <dir outside repo> --name <run> --image <tag> --image-id <sha256:...>
        [--jobs 1..6] [--files <file or stem> ...] [--nodes-file <txt>] [--file-timeout <s>]

Each shard is a scripts/c3_test_host.py context (own PostgreSQL on tmpfs, Redis, MinIO) with the
storage container switched to the digest-pinned MinIO of deploy/compose.yaml. Every pytest call
gets the shard's own Redis for the limiter tests, RAS_TEST_PROCESS_IMAGE = the frozen API image,
a --basetemp under the context and @args files (no WinError 206). Never reads .env, never touches
the dev project. A run is never merged into another: --files/--nodes-file reruns get their own
--name and summary. A file that exceeds --file-timeout is stopped by its own PID tree only.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
HOST = REPO / 'scripts/c3_test_host.py'
HEAVY = ('_browser', '_proxy_smoke', '_process_smoke', 'test_c3_')
MINIO = re.search(r"\$\{RAS_MINIO_IMAGE:-([^}]+@sha256:[0-9a-f]{64})\}",
                  (REPO / 'deploy/compose.yaml').read_text(encoding='utf-8')).group(1)


def sources():
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', 'api', 'scripts',
                                     'web', 'deploy', 'docker-compose.yml'], cwd=REPO, text=True).splitlines()
    return {n: hashlib.sha256((REPO / n).read_bytes()).hexdigest() for n in sorted(set(names))
            if (REPO / n).is_file() and not any(part.startswith('.env') for part in Path(n).parts)}


def image_id(tag):
    return subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()


def shard_compose(shard):
    c = json.loads((shard / 'credentials.json').read_text(encoding='utf-8'))
    return c, ['docker', 'compose', '--env-file', str(shard / 'empty.env'), '-p', c['project'],
               '-f', str(shard / 'compose.json')]


def setup_shard(shard, log):
    setup = ("import sys;sys.path.insert(0,sys.argv.pop(1));import c3_test_host as h;"
             "from c4_isolation import free_port;h.free_port=free_port;raise SystemExit(h.main())")
    r = subprocess.run([sys.executable, '-c', setup, str(REPO / 'scripts'), '--context', str(shard), 'setup'],
                       capture_output=True, text=True)
    log.write_text(r.stdout + r.stderr, encoding='utf-8')
    if r.returncode:
        raise SystemExit('shard setup failed: ' + str(shard))
    config = json.loads((shard / 'compose.json').read_text(encoding='utf-8'))
    config['services']['db']['tmpfs'] = ['/var/lib/postgresql/data']
    config['services']['minio']['image'] = MINIO
    config['services']['minio']['tmpfs'] = ['/data']
    (shard / 'compose.json').write_text(json.dumps(config), encoding='utf-8')
    _, command = shard_compose(shard)
    subprocess.run(command + ['up', '-d', '--wait', '--force-recreate', 'db', 'minio'], check=True, capture_output=True)


def shard_facts(shard):
    _, command = shard_compose(shard)
    ids = subprocess.check_output(command + ['ps', '-q'], text=True).split()
    facts = json.loads(subprocess.check_output(['docker', 'inspect', *ids], text=True))
    return [{'name': f['Name'], 'image': f['Image'], 'config_image': f['Config']['Image'],
             'tmpfs': sorted((f['HostConfig'].get('Tmpfs') or {}).keys())} for f in facts]


def kill_tree(pid):
    # Only the PID this runner started, with its children; never by image name.
    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--jobs', type=int, default=6)
    p.add_argument('--image', required=True)
    p.add_argument('--image-id', required=True)
    p.add_argument('--files', nargs='*')
    p.add_argument('--nodes-file', type=Path, help='rerun exactly these node IDs (one per line)')
    p.add_argument('--file-timeout', type=int, default=3 * 3600)
    a = p.parse_args()
    ctx = a.context.resolve()
    if ctx == REPO or REPO in ctx.parents or not 1 <= a.jobs <= 6:
        raise SystemExit('external context and 1..6 jobs required')
    if Path(sys.executable).resolve() != Path(r'C:\Users\ADMIN\.conda\envs\ras\python.exe').resolve():
        raise SystemExit('Conda ras required')
    if image_id(a.image) != a.image_id:
        raise SystemExit('frozen API image changed')
    root = ctx / a.name
    root.mkdir(parents=True, exist_ok=False)
    before = sources()
    (root / 'source-before.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
    base_env = {k: v for k, v in os.environ.items()
                if not k.upper().startswith(('RAS_', 'OPENROUTER_', 'POSTGRES_', 'MINIO_', 'REDIS_'))}
    base_env['RAS_TEST_PROCESS_IMAGE'] = a.image
    available = Queue()
    shards = {}
    for number in range(a.jobs):
        shard = ctx / ('shard-' + str(number))
        if not (shard / 'credentials.json').exists():
            setup_shard(shard, root / ('setup-' + str(number) + '.log'))
        else:
            _, command = shard_compose(shard)
            if not subprocess.check_output(command + ['ps', '-q'], text=True).split():
                # A shard taken down earlier: same compose file, ports and credentials.
                subprocess.run(command + ['up', '-d', '--wait'], check=True, capture_output=True)
        shards[str(shard)] = shard_facts(shard)
        available.put(shard)
    (root / 'shards.json').write_text(json.dumps(shards, indent=2), encoding='utf-8')
    tmp = ctx / 't'

    def call(shard, name, args):
        c = json.loads((shard / 'credentials.json').read_text(encoding='utf-8'))
        env = dict(base_env, RAS_TEST_REDIS_HOST='127.0.0.1', RAS_TEST_REDIS_PORT=str(c['redis_port']),
                   RAS_TEST_REDIS_PASSWORD=c['redis'])
        basetemp = tmp / (a.name[:12] + '-' + str(time.time_ns())[-9:])
        basetemp.parent.mkdir(parents=True, exist_ok=True)
        argfile = root / (name + '.args.txt')
        argfile.write_text('\n'.join(['-p', 'no:cacheprovider', '--basetemp', str(basetemp), *args]) + '\n',
                           encoding='utf-8')
        started = time.monotonic()
        with (root / (name + '.log')).open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, str(HOST), '--context', str(shard), 'test', '@' + str(argfile)],
                                       cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=a.file_timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                kill_tree(process.pid)
                code, timed_out = process.wait(), True
        return code, timed_out, round(time.monotonic() - started, 2), str(basetemp)

    shard = available.get()
    try:
        code, _, _, _ = call(shard, 'collection', ['api/tests', '--collect-only', '-q'])
    finally:
        available.put(shard)
    if code:
        raise SystemExit('collection failed')
    from c2d7_seal import collected
    nodes = collected(root / 'collection.log')
    (root / 'collection.json').write_text(json.dumps(nodes, indent=2), encoding='utf-8')
    c4 = json.loads((REPO / 'docs/c4-test-inventory.json').read_text(encoding='utf-8'))['api']['nodes']
    m6d = json.loads((REPO / 'docs/m6d-test-inventory.json').read_text(encoding='utf-8'))['api']['nodes']
    summary = {'image': a.image, 'image_id': a.image_id, 'minio_image': MINIO, 'jobs': a.jobs,
               'collected': len(nodes), 'duplicate_collection': [n for n, k in Counter(nodes).items() if k > 1],
               'c4_nodes': len(c4), 'missing_c4': sorted(set(c4) - set(nodes)),
               'm6d_nodes': len(m6d), 'missing_m6d': sorted(set(m6d) - set(nodes)),
               'new_since_m6d': sorted(set(nodes) - set(m6d)), 'groups': {}}
    wanted = None
    if a.nodes_file:
        wanted = [n.strip() for n in a.nodes_file.read_text(encoding='utf-8').splitlines() if n.strip()]
        unknown = sorted(set(wanted) - set(nodes))
        if unknown:
            raise SystemExit('nodes not collected: ' + ', '.join(unknown[:5]))
    counts = Counter(n.split('::')[0] for n in nodes)
    files = sorted(counts, key=lambda name: -counts[name])
    if a.files:
        files = [f for f in files if f in a.files or Path(f).stem in a.files]
    if wanted is not None:
        files = sorted({n.split('::')[0] for n in wanted})
    summary['mode'] = ('node rerun' if wanted is not None else 'selected whole-file rerun' if a.files
                       else 'full API inventory')

    def execute(file):
        shard = available.get()
        name = Path(file).stem
        selected = [n for n in nodes if n.split('::')[0] == file and (wanted is None or n in wanted)]
        targets = [file] if wanted is None else selected
        try:
            exit_code, timed_out, seconds, basetemp = call(
                shard, name, [*targets, '-q', '--tb=short', '-o', 'faulthandler_timeout=0',
                              '--junitxml=' + str(root / (name + '.xml'))])
        finally:
            available.put(shard)
        xml = root / (name + '.xml')
        states, raw = {}, []
        if xml.exists():
            for case in ET.parse(xml).findall('.//testcase'):
                ident = 'api/' + case.attrib['classname'].replace('.', '/') + '.py::' + case.attrib['name']
                raw.append(ident)
                state = next((s for s in ('error', 'failure', 'skipped') if case.find(s) is not None), 'pass')
                states[ident] = state if states.get(ident) in (None, 'pass') else states[ident]
        return name, dict(file=file, exit=exit_code, timed_out=timed_out, seconds=seconds, selected=selected,
                          states=states, duplicates=[n for n, k in Counter(raw).items() if k > 1], xml=str(xml),
                          xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest() if xml.exists() else None,
                          shard=str(shard), basetemp=basetemp)

    for workload, jobs in (([f for f in files if not any(k in f for k in HEAVY)], a.jobs),
                           ([f for f in files if any(k in f for k in HEAVY)], 1)):
        with ThreadPoolExecutor(jobs) as pool:
            for future in as_completed([pool.submit(execute, file) for file in workload]):
                name, result = future.result()
                summary['groups'][name] = result
                (root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
                print(time.strftime('%H:%M:%S'), name, 'exit=' + str(result['exit']), result['seconds'],
                      dict(Counter(result['states'].values())), flush=True)
    after = sources()
    (root / 'source-after.json').write_text(json.dumps(after, indent=2), encoding='utf-8')
    summary['source_equal'] = before == after
    summary['image_unchanged'] = image_id(a.image) == a.image_id
    observed = {n: s for g in summary['groups'].values() for n, s in g['states'].items()}
    selected = {n for g in summary['groups'].values() for n in g['selected']}
    summary['counts'] = dict(Counter(observed[n] for n in selected if n in observed))
    summary['not_run'] = sorted(selected - set(observed))
    summary['unexpected'] = sorted(set(observed) - selected)
    summary['failed_nodes'] = sorted(n for n in selected if observed.get(n) in ('failure', 'error'))
    (root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    okay = (summary['source_equal'] and summary['image_unchanged'] and not summary['not_run']
            and not summary['unexpected'] and not summary['failed_nodes']
            and all(g['exit'] == 0 and not g['duplicates'] for g in summary['groups'].values()))
    print('source_equal=' + str(summary['source_equal']), 'image_unchanged=' + str(summary['image_unchanged']),
          summary['counts'], 'not_run=' + str(len(summary['not_run'])), flush=True)
    return 0 if okay else 1


if __name__ == '__main__':
    raise SystemExit(main())
