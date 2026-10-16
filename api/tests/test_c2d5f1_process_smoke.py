"""Linux process smoke for the citation reconcile runtime: CLI container and Celery worker+beat.

Both run on the isolated PG/Redis network with the provisioned runtime login.
The mounted sitecustomize is TEST ONLY: it uses the real C4 gate, blocks egress,
narrows the beat schedule to ``ras.citations.reconcile`` and, until the host
creates ``/evidence/allow-resume``, parks the SECOND batch of a process just
before its citation lock (the first batch is committed), so the host can
SIGKILL the process mid-pass. No operator credential, provider, arXiv or dev
service is involved; the kill is a real process kill, not a power-loss proof.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from sqlalchemy import text

from tests.conftest import REPO_DIR
from tests.resource_fixtures import resource_api, resource_world, installation, source  # noqa: F401
from tests.test_c2d5f1_citation_runtime import cit, refs  # noqa: F401

IMAGE = os.environ.get('RAS_TEST_PROCESS_IMAGE', 'rasc2d5f1-worker:20261010')
RAWS = ['RAWCANARY one arXiv:2609.00011', 'RAWCANARY two arXiv:2609.00012', 'RAWCANARY three no id',
        'RAWCANARY four arXiv:2609.00013', 'RAWCANARY five arXiv:2609.00014']
HARNESS = '''
import json, os, socket, time
from pathlib import Path
allowed = set()
for name in ('db', 'redis'):
    try:
        allowed.add(socket.gethostbyname(name))
    except OSError:
        pass
original = socket.socket.connect
def connect(self, address):
    if isinstance(address, tuple) and address[0] not in allowed and address[0] != '127.0.0.1':
        raise OSError('test egress blocked')
    return original(self, address)
socket.socket.connect = connect
role = os.environ.get('SMOKE_ROLE')
if role in ('cli', 'worker', 'beat'):
    from app import auth_safety

if role == 'beat':
    from app.ingest.celery_app import celery_app
    celery_app.conf.beat_schedule = {'citations-test-only': {'task': 'ras.citations.reconcile', 'schedule': 1.0}}
if role in ('cli', 'worker'):
    assert not os.environ.get('POSTGRES_PASSWORD')
    assert not os.environ.get('RAS_RECOVERY_DB_PASSWORD')
    from app import citations
    real = citations._lock
    calls = {'n': 0}
    def lock(db, *, exclusive):
        if not exclusive:
            calls['n'] += 1
            if calls['n'] == 2 and not Path('/evidence/allow-resume').exists():
                from app.db import get_engine
                Path('/evidence/in-batch-2-' + socket.gethostname() + '-' + str(os.getpid())).write_text(
                    str(get_engine().pool.checkedout()))
                while not Path('/evidence/allow-resume').exists():
                    time.sleep(.2)
        return real(db, exclusive=exclusive)
    citations._lock = lock
    from app.db import get_sessionmaker
    from app.ingest import runtime
    from sqlalchemy import text
    db = get_sessionmaker()()
    def proof(_):
        runtime.require_runtime(db)
        return dict(db.execute(text("SELECT current_user AS login, has_table_privilege('users','UPDATE') AS raw_user_write,"
            "has_database_privilege(current_database(),'TEMP') AS temp,"
            "has_table_privilege('revocation_journal_records','INSERT') AS journal_write,"
            "has_table_privilege('reference_citation_links','INSERT') AS link_write")).mappings().one())
    fact = runtime.short(db, proof)
    import uuid  # a restarted container reuses hostname and PID 1: one file per process start
    Path('/evidence/identity-' + role + '-' + socket.gethostname() + '-' + str(os.getpid()) + '-' + uuid.uuid4().hex
         + '.json').write_text(json.dumps(fact))
'''
IDENTITY = {'login': 'c1_runtime', 'raw_user_write': False, 'temp': False, 'journal_write': False, 'link_write': True}


def setup(h):
    context = Path(os.environ['RAS_C2D5F1_CONTEXT'])
    c = json.loads((context / 'credentials.json').read_text())
    evidence = context / ('process-' + uuid.uuid4().hex)
    (evidence / 'harness').mkdir(parents=True)
    (evidence / 'harness' / 'sitecustomize.py').write_text(HARNESS, encoding='utf-8')
    a = source(h.w, h.aid, 'SMOKE PRIVATE')
    ids = refs(h, a, RAWS)
    return context, c, evidence, ids


def state(h):
    with h.w.engine.connect() as conn:  # labelled owner read
        return tuple(conn.execute(text('SELECT count(*), max(reconciled_at) FROM reference_citation_links')).one())


def wait(predicate, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError(label)


def command(evidence, args, *, label, check=True, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
    (evidence / (label + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
    assert not check or result.returncode == 0, label + ' failed; see external raw log'
    return result


def identities(evidence, role):
    return [json.loads(p.read_text()) for p in evidence.glob('identity-' + role + '-*.json')]


def test_real_cli_process_kill_between_batches_then_resume_and_idempotent(cit):
    h = cit
    context, c, evidence, ids = setup(h)
    values = {'RAS_DB__HOST': 'db', 'RAS_DB__PORT': '5432', 'RAS_DB__USER': h.w.runtime.url.username,
              'RAS_DB__PASSWORD': c['runtime'], 'RAS_DB__NAME': h.w.runtime.url.database,
              'RAS_RECOVERY__JOURNAL_PATH': '/journal/' + h.w.path.name, 'RAS_EMBEDDING__API_KEY': '',
              'RAS_EMBEDDING__PRICING_VERIFIED': 'false', 'RAS_DISCOVERY__ENABLED': 'false',
              'RAS_CITATIONS__RECONCILE_BATCH_SIZE': '2', 'SMOKE_ROLE': 'cli', 'PYTHONPATH': '/harness:/srv/api'}
    env_file = evidence / 'runtime.env'
    env_file.write_text('\n'.join(k + '=' + v for k, v in values.items()) + '\n', encoding='utf-8')
    mounts = []
    for src, target, mode in ((evidence / 'harness', '/harness', ':ro'),
                              (evidence, '/evidence', ''), (h.w.path.parent, '/journal', '')):
        mounts += ['-v', str(src).replace('\\', '/') + ':' + target + mode]
    name = c['project'] + '-citations-' + uuid.uuid4().hex[:8]
    base = ['docker', 'run', '--label', 'ras.task=' + c['project'], '--network', c['project'] + '_default',
            '--env-file', str(env_file), *mounts]
    cli = [IMAGE, 'python', '-m', 'app.cli.reconcile_citations']
    started = False
    try:
        command(evidence, base + ['-d', '--name', name] + cli, label='start-parked')
        started = True
        wait(lambda: list(evidence.glob('in-batch-2-*')), 60, 'second batch was not reached')
        assert state(h)[0] == 2  # the first batch is committed and durable before the kill
        assert all(p.read_text() == '1' for p in evidence.glob('in-batch-2-*'))  # only the batch Session
        command(evidence, ['docker', 'kill', '--signal', 'KILL', name], label='kill')
        killed = command(evidence, ['docker', 'inspect', '--format', '{{.State.ExitCode}} {{.State.Running}}', name],
                         label='inspect')
        assert killed.stdout.split() == ['137', 'false']
        assert state(h)[0] == 2
        (evidence / 'allow-resume').write_text('1')
        resumed = command(evidence, base + ['--rm'] + cli, label='resume')
        assert resumed.stdout.strip() == 'Citation reconcile: batches=2, links_written=3, references=3'
        assert 'RAWCANARY' not in resumed.stdout + resumed.stderr
        count, stamp = state(h)
        assert count == 5
        again = command(evidence, base + ['--rm'] + cli, label='again')
        assert again.stdout.strip() == 'Citation reconcile: nothing to do'
        assert state(h) == (5, stamp)  # idempotent: no row, no timestamp bump
        facts = identities(evidence, 'cli')
        assert len(facts) == 3 and all(fact == IDENTITY for fact in facts)
        (evidence / 'result.json').write_text(json.dumps({'kill_exit': 137, 'links': count, 'identities': facts,
                                                          'image': IMAGE}), encoding='utf-8')
    finally:
        if started:
            command(evidence, ['docker', 'logs', name], label='parked-logs', check=False)
            command(evidence, ['docker', 'rm', '-f', name], label='remove', check=False)
        env_file.unlink(missing_ok=True)


def test_real_celery_worker_beat_kill_between_batches_then_resume(cit):
    h = cit
    context, c, evidence, ids = setup(h)
    values = {'POSTGRES_USER': 'task_admin', 'POSTGRES_PASSWORD': c['password'],
              'POSTGRES_DB': h.w.runtime.url.database, 'MINIO_ROOT_USER': 'task_minio', 'MINIO_ROOT_PASSWORD': c['minio'],
              'REDIS_PASSWORD': c['redis'], 'RAS_WORKER_RUNTIME_USER': h.w.runtime.url.username,
              'RAS_WORKER_RUNTIME_PASSWORD': c['runtime'], 'RAS_JOURNAL_SOURCE': str(h.w.path.parent).replace('\\', '/')}
    values.update(RAS_RUNTIME_DB_USER='c1_runtime',RAS_RUNTIME_DB_PASSWORD=c['runtime'],
        RAS_RECOVERY_DB_USER='c1_recovery',RAS_RECOVERY_DB_PASSWORD=c['recovery'],
        RAS_DEFINER_DB_ROLE='c1_definer',RAS_API_IMAGE=os.environ.get('RAS_TEST_PROCESS_IMAGE','rasc4-process:local'))
    env_file = evidence / 'runtime.env'
    env_file.write_text('\n'.join(k + '=' + v for k, v in values.items()) + '\n', encoding='utf-8')
    extra = {'services': {}, 'networks': {'default': {'external': True, 'name': c['project'] + '_default'}}}
    for role in ('worker', 'beat'):
        extra['services'][role] = {'image': IMAGE, 'build': str(REPO_DIR / 'api'),
            'labels': {'ras.task': c['project']},
            'environment': {'PYTHONPATH': '/harness:/srv/api', 'RAS_REDIS_PORT': '6379', 'SMOKE_ROLE': role,
                            'RAS_CITATIONS__RECONCILE_BATCH_SIZE': '2'},
            'volumes': [str(evidence / 'harness').replace('\\', '/') + ':/harness:ro',
                        str(evidence).replace('\\', '/') + ':/evidence']}
    extra['services']['worker']['command'] = ['celery', '-A', 'app.ingest.celery_app:celery_app', 'worker',
                                              '--loglevel=WARNING', '--concurrency=1']
    extra['services']['beat']['command'] = ['celery', '-A', 'app.ingest.celery_app:celery_app', 'beat',
                                            '--loglevel=WARNING', '--schedule=/tmp/beat']
    override = evidence / 'smoke.json'
    override.write_text(json.dumps(extra), encoding='utf-8')
    compose = ['docker', 'compose', '--env-file', str(env_file), '-p', c['project'], '-f', str(REPO_DIR / 'docker-compose.yml'),
               '-f', str(REPO_DIR / 'scripts' / 'compose.worker-runtime.yml'), '-f', str(override)]
    started = False
    try:
        started = True
        command(evidence, compose + ['up', '-d', '--no-deps', '--no-build', 'worker', 'beat'], label='start')
        wait(lambda: list(evidence.glob('in-batch-2-*')), 90, 'second batch was not reached')
        assert state(h)[0] == 2
        command(evidence, compose + ['kill', '-s', 'SIGKILL', 'worker'], label='kill-worker')
        # The killed batch never committed. ``restart: unless-stopped`` may already have started a new
        # worker whose own first batch (two other references) commits before it parks again.
        assert state(h)[0] in (2, 4)
        (evidence / 'allow-resume').write_text('1')
        command(evidence, compose + ['up', '-d', '--no-deps', '--no-build', 'worker'], label='resume')
        wait(lambda: state(h)[0] == 5, 90, 'not resumed after restart')
        count, stamp = state(h)
        command(evidence, compose + ['restart', 'worker', 'beat'], label='restart')
        time.sleep(6)  # several beat ticks reach the worker
        assert state(h) == (5, stamp)  # idempotent: no row, no timestamp bump
        facts = identities(evidence, 'worker')
        assert len(facts) >= 2 and all(fact == IDENTITY for fact in facts)
        logs = command(evidence, compose + ['logs', '--no-color', 'worker', 'beat'], label='processes')
        assert 'RAWCANARY' not in logs.stdout + logs.stderr
        (evidence / 'result.json').write_text(json.dumps({'links': count, 'identities': facts, 'image': IMAGE,
            'override': 'scripts/compose.worker-runtime.yml'}), encoding='utf-8')
    finally:
        if started:
            command(evidence, compose + ['logs', '--no-color', 'worker', 'beat'], label='processes-final', check=False)
            command(evidence, compose + ['stop', 'worker', 'beat'], label='stop', check=False)
            command(evidence, compose + ['rm', '-f', 'worker', 'beat'], label='remove', check=False)
        env_file.unlink(missing_ok=True)
