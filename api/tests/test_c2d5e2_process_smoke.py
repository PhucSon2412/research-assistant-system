"""Linux CLI process smoke: ``python -m app.cli.reconcile_originals --apply`` in a container
on the isolated PG/MinIO network, with the provisioned runtime login.

The mounted sitecustomize is TEST ONLY: it uses the real C4 gate, blocks egress,
shifts the reconcile clock past the grace (the environment is quiescent by
construction: no writer runs, so the shift only avoids waiting 24 h and
proves nothing about settling), and in the first process parks the first
object delete so the host can SIGKILL the process between the committed
fence and the delete. No operator credential, provider or dev service.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from sqlalchemy import text

from tests.conftest import REPO_DIR
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import operational_world as world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d5e2_orphan_reconcile import orphan, put, present, body, refer, business  # noqa: F401
from tests.resource_fixtures import BODY

IMAGE = os.environ.get('RAS_TEST_PROCESS_IMAGE', 'rasc2d5e2-cli:20261010')
HARNESS = '''
import json, os, socket, time
from pathlib import Path
allowed = {socket.gethostbyname(name) for name in ('db', 'minio')}
original = socket.socket.connect
def connect(self, address):
    if isinstance(address, tuple) and address[0] not in allowed and address[0] != '127.0.0.1':
        raise OSError('test egress blocked')
    return original(self, address)
socket.socket.connect = connect
if os.environ.get('SMOKE_ROLE') == 'reconcile':
    from datetime import UTC, datetime, timedelta
    from app import auth_safety

    from app.ingest import orphans, runtime
    orphans.utcnow = lambda: datetime.now(UTC) + timedelta(days=2)
    if os.environ.get('SMOKE_PARK') == '1':
        def parked(storage, proof):
            from app.db import get_engine
            assert get_engine().pool.checkedout() == 0
            Path('/evidence/in-delete').write_text('1')
            while True:
                time.sleep(.2)
        orphans.remove = parked
    from app.db import get_sessionmaker
    from sqlalchemy import text
    db = get_sessionmaker()()
    def proof(_):
        runtime.require_runtime(db)
        return dict(db.execute(text("SELECT current_user AS login, has_table_privilege('users','UPDATE') AS raw_user_write,"
            "has_database_privilege(current_database(),'TEMP') AS temp,"
            "has_table_privilege('orphan_object_fences','INSERT,UPDATE,DELETE,TRUNCATE') AS raw_fence_write,"
            "has_function_privilege('ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz)','EXECUTE')"
            " AS fence_routine")).mappings().one())
    fact = runtime.short(db, proof)
    Path('/evidence/identity-' + str(os.getpid()) + '-' + os.environ.get('SMOKE_RUN', '') + '.json').write_text(json.dumps(fact))
'''


def test_real_cli_process_kill_between_fence_and_delete_then_resume(orphan):
    b = orphan
    context = Path(os.environ['RAS_C2D5E2_CONTEXT'])
    c = json.loads((context / 'credentials.json').read_text())
    evidence = context / ('process-' + uuid.uuid4().hex)
    evidence.mkdir()
    (evidence / 'harness').mkdir()
    (evidence / 'harness' / 'sitecustomize.py').write_text(HARNESS, encoding='utf-8')
    first, second = sorted(put(b) for _ in range(2))  # two old orphans; the first is parked
    kept = put(b, 'originals/' + 'f' * 32)  # sorts last; a referenced original
    refer(b, kept, 'private_a')
    before = business(b)
    values = {'RAS_DB__HOST': 'db', 'RAS_DB__PORT': '5432', 'RAS_DB__USER': b.w.runtime.url.username,
              'RAS_DB__PASSWORD': c['runtime'], 'RAS_DB__NAME': b.w.runtime.url.database,
              'RAS_STORAGE__ENDPOINT_URL': 'http://minio:9000', 'RAS_STORAGE__ACCESS_KEY': 'task_minio',
              'RAS_STORAGE__SECRET_KEY': c['minio'], 'RAS_STORAGE__BUCKET': b.raw.bucket,
              'RAS_RECOVERY__JOURNAL_PATH': '/journal/' + b.w.path.name, 'RAS_EMBEDDING__API_KEY': '',
              'RAS_EMBEDDING__PRICING_VERIFIED': 'false', 'RAS_DISCOVERY__ENABLED': 'false',
              'SMOKE_ROLE': 'reconcile', 'PYTHONPATH': '/harness:/srv/api'}
    env_file = evidence / 'runtime.env'
    env_file.write_text('\n'.join(k + '=' + v for k, v in values.items()) + '\n', encoding='utf-8')
    mounts = []
    for source, target, mode in ((evidence / 'harness', '/harness', ':ro'),
                                 (evidence, '/evidence', ''), (b.w.path.parent, '/journal', '')):
        mounts += ['-v', str(source).replace('\\', '/') + ':' + target + mode]
    name = c['project'] + '-reconcile-' + uuid.uuid4().hex[:8]
    base = ['docker', 'run', '--label', 'ras.task=' + c['project'], '--network', c['project'] + '_default',
            '--env-file', str(env_file), *mounts]
    cli = [IMAGE, 'python', '-m', 'app.cli.reconcile_originals', '--apply']

    def command(args, *, label, check=True):
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=120)
        (evidence / (label + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        assert not check or result.returncode == 0, label + ' failed; see external raw log'
        return result

    def state():
        with b.w.engine.connect() as conn:  # labelled owner read
            f = conn.execute(text('SELECT storage_key FROM orphan_object_fences ORDER BY storage_key')).scalars().all()
            d = conn.execute(text('SELECT f.storage_key,c.outcome FROM orphan_object_completions c JOIN orphan_object_fences f'
                                  ' ON f.id=c.fence_id ORDER BY 1')).all()
            a = conn.scalar(text("SELECT count(*) FROM audit_events WHERE action='original.orphan_reconcile'"))
        return f, [tuple(r) for r in d], a

    started = False
    try:
        command(base + ['-d', '--name', name, '-e', 'SMOKE_PARK=1', '-e', 'SMOKE_RUN=1'] + cli, label='start-parked')
        started = True
        deadline = time.monotonic() + 60
        while not (evidence / 'in-delete').exists():
            assert time.monotonic() < deadline, 'delete was not reached'
            time.sleep(.2)
        assert state() == ([first], [], 0)  # the fence is durable before the delete
        assert present(b, first) and present(b, second)
        command(['docker', 'kill', '--signal', 'KILL', name], label='kill')
        killed = command(['docker', 'inspect', '--format', '{{.State.ExitCode}} {{.State.Running}}', name], label='inspect')
        assert killed.stdout.split() == ['137', 'false']
        assert present(b, first) and state() == ([first], [], 0)
        resumed = command(base + ['--rm', '-e', 'SMOKE_RUN=2'] + cli, label='resume')
        assert 'deleted=2' in resumed.stdout and 'kept:referenced=1' in resumed.stdout and 'listed=2' in resumed.stdout
        for key in (first, second, kept):
            assert key not in resumed.stdout + resumed.stderr
        assert not present(b, first) and not present(b, second) and body(b, kept) == BODY
        assert state() == ([first, second], [(first, 'deleted'), (second, 'deleted')], 2)
        again = command(base + ['--rm', '-e', 'SMOKE_RUN=3'] + cli, label='again')
        assert 'listed=1' in again.stdout and 'kept:referenced=1' in again.stdout and 'deleted' not in again.stdout
        assert state() == ([first, second], [(first, 'deleted'), (second, 'deleted')], 2)  # idempotent
        assert business(b) == before and body(b, kept) == BODY
        facts = [json.loads(p.read_text()) for p in evidence.glob('identity-*.json')]
        assert len(facts) == 3 and all(fact == {'login': 'c1_runtime', 'raw_user_write': False, 'temp': False,
                                                'raw_fence_write': False, 'fence_routine': True} for fact in facts)
        (evidence / 'result.json').write_text(json.dumps({'kill_exit': 137, 'fences': 2, 'completions': 2, 'audits': 2,
            'identities': facts, 'image': IMAGE}), encoding='utf-8')
    finally:
        if started:
            command(['docker', 'logs', name], label='parked-logs', check=False)
            command(['docker', 'rm', '-f', name], label='remove', check=False)
        env_file.unlink(missing_ok=True)
