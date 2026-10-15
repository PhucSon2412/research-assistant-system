"""Linux Celery worker+beat cleanup on the runtime Compose override, isolated PG/MinIO/Redis.

The mounted sitecustomize is TEST ONLY: it scopes cutover and the beat schedule,
blocks egress and parks the FIRST object delete so the host can SIGKILL the
worker between the committed fence and the delete. No production switch,
operator credential, provider or dev service is involved.
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
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes  # noqa: F401
from tests.resource_fixtures import runtime_revoke

IMAGE = os.environ.get('RAS_TEST_PROCESS_IMAGE', 'rasc2d5e1-worker:20261010')


def test_real_worker_beat_cleanup_kill_between_fence_and_delete(indexes):
    b = indexes
    context = Path(os.environ['RAS_C2D5E1_CONTEXT'])
    c = json.loads((context / 'credentials.json').read_text())
    evidence = context / ('process-' + uuid.uuid4().hex)
    evidence.mkdir()
    runtime_revoke(b.w, b.copy.item, clock=b.clock)
    b.raw_storage.size(b.copy.key)  # the copy's object exists before cleanup
    values = {'POSTGRES_USER': 'task_admin', 'POSTGRES_PASSWORD': c['password'],
              'POSTGRES_DB': b.w.runtime.url.database, 'MINIO_ROOT_USER': 'task_minio', 'MINIO_ROOT_PASSWORD': c['minio'],
              'REDIS_PASSWORD': c['redis'], 'RAS_WORKER_RUNTIME_USER': b.w.runtime.url.username,
              'RAS_WORKER_RUNTIME_PASSWORD': c['runtime'], 'RAS_JOURNAL_SOURCE': str(b.w.path.parent).replace('\\', '/')}
    env_file = evidence / 'runtime.env'
    env_file.write_text('\n'.join(k + '=' + v for k, v in values.items()) + '\n', encoding='utf-8')
    harness = evidence / 'harness'
    harness.mkdir()
    (harness / 'sitecustomize.py').write_text('''
import json, os, socket, sys, time
from pathlib import Path
allowed={socket.gethostbyname(name) for name in ('db','minio')}
original=socket.socket.connect
def connect(self,address):
 if isinstance(address,tuple) and address[0] not in allowed and address[0]!='127.0.0.1':
  if address[1]!=6379 or address[0]!=socket.gethostbyname('redis'):
   raise OSError('test egress blocked')
 return original(self,address)
socket.socket.connect=connect
if sys.argv[0].endswith('celery'):
 from app import auth_safety
 auth_safety.cutover=lambda db:'ready'
 from app.ingest.celery_app import celery_app
 celery_app.conf.beat_schedule={'cleanup-test-only':{'task':'ras.revocation.cleanup','schedule':1.0}}
 from app import publication
 real=publication._remove_object
 def parked(storage,key):
  from app.db import get_engine
  assert get_engine().pool.checkedout()==0
  Path('/evidence/in-delete-'+socket.gethostname()+'-'+str(os.getpid())).write_text('1')
  while not Path('/evidence/allow-delete').exists():
   time.sleep(.2)
  return real(storage,key)
 publication._remove_object=parked
 from app.db import get_sessionmaker
 from app.ingest import runtime
 from sqlalchemy import text
 db=get_sessionmaker()()
 def proof(_):
  runtime.require_runtime(db)
  return dict(db.execute(text("SELECT current_user AS login,has_table_privilege('users','UPDATE') AS raw_user_write,"
   "has_database_privilege(current_database(),'TEMP') AS temp,"
   "has_table_privilege('object_delete_fences','INSERT,UPDATE,DELETE,TRUNCATE') AS raw_fence_write,"
   "has_function_privilege('ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)','EXECUTE') AS fence_routine")).mappings().one())
 fact=runtime.short(db,proof)
 Path('/evidence/identity-'+socket.gethostname()+'-'+str(os.getpid())+'.json').write_text(json.dumps(fact))
''', encoding='utf-8')
    extra = {'services': {}, 'networks': {'default': {'external': True, 'name': c['project'] + '_default'}}}
    for name in ('worker', 'beat'):
        extra['services'][name] = {'image': IMAGE, 'build': str(REPO_DIR / 'api'),
            'labels': {'ras.task': c['project']}, 'environment': {'PYTHONPATH': '/harness:/srv/api',
                'RAS_REDIS_PORT': '6379'},
            'volumes': [str(REPO_DIR / 'api' / 'app').replace('\\', '/') + ':/srv/api/app:ro',
                        str(harness).replace('\\', '/') + ':/harness:ro', str(evidence).replace('\\', '/') + ':/evidence']}
    extra['services']['worker']['environment']['RAS_STORAGE__BUCKET'] = b.raw_storage.bucket  # beat has no storage
    extra['services']['beat']['command'] = ['celery', '-A', 'app.ingest.celery_app:celery_app', 'beat',
                                            '--loglevel=WARNING', '--schedule=/tmp/beat']
    override = evidence / 'smoke.json'
    override.write_text(json.dumps(extra), encoding='utf-8')
    compose = ['docker', 'compose', '--env-file', str(env_file), '-p', c['project'], '-f', str(REPO_DIR / 'docker-compose.yml'),
               '-f', str(REPO_DIR / 'scripts' / 'compose.worker-runtime.yml'), '-f', str(override)]

    def command(args, *, name):
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=90)
        (evidence / (name + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        assert result.returncode == 0, name + ' failed; see external raw log'
        return result.stdout

    def state():
        with b.w.runtime.connect() as conn:
            t = conn.execute(text('SELECT purged_at,object_outcome,cleanup_attempts,last_cleanup_error FROM deletion_tombstones'
                                  ' WHERE item_id=:i'), {'i': b.copy.item}).one()
            f = conn.scalar(text('SELECT count(*) FROM object_delete_fences WHERE storage_key=:k'), {'k': b.copy.key})
            a = conn.scalar(text("SELECT count(*) FROM audit_events WHERE action='common_copy.cleanup'"))
        return t, f, a

    def wait(predicate, seconds, label):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.2)
        raise AssertionError(label)

    started = False
    try:
        started = True
        command(compose + ['up', '-d', '--no-deps', '--no-build', 'worker', 'beat'], name='start')
        wait(lambda: list(evidence.glob('in-delete-*')), 60, 'delete was not reached')
        t, f, a = state()
        assert t.purged_at is None and f == 1 and a == 0  # fence durable before the delete
        b.raw_storage.size(b.copy.key)  # not deleted yet
        command(compose + ['kill', '-s', 'SIGKILL', 'worker'], name='kill-worker')
        for marker in evidence.glob('in-delete-*'):
            marker.unlink()
        (evidence / 'allow-delete').write_text('1')
        command(compose + ['up', '-d', '--no-deps', '--no-build', 'worker'], name='resume')
        wait(lambda: state()[0].purged_at is not None, 60, 'not purged after restart')
        t, f, a = state()
        assert t.object_outcome == 'deleted' and t.last_cleanup_error is None and f == 1 and a == 1
        try:
            b.raw_storage.size(b.copy.key)
            raise AssertionError('object still present')
        except Exception as exc:
            from app.storage import is_missing_object
            assert is_missing_object(exc)
        command(compose + ['restart', 'worker', 'beat'], name='restart')
        time.sleep(4)
        assert state() == (t, 1, 1)  # idempotent: no second finalisation or audit
        facts = [json.loads(p.read_text()) for p in evidence.glob('identity-*.json')]
        assert len(facts) >= 2 and all(fact == {'login': 'c1_runtime', 'raw_user_write': False, 'temp': False,
                                                'raw_fence_write': False, 'fence_routine': True} for fact in facts)
        (evidence / 'result.json').write_text(json.dumps({'tombstone': [str(v) for v in t], 'fences': f, 'audits': a,
            'identities': facts, 'override': 'scripts/compose.worker-runtime.yml'}), encoding='utf-8')
    finally:
        if started:
            command(compose + ['logs', '--no-color', 'worker', 'beat'], name='processes')
            command(compose + ['stop', 'worker', 'beat'], name='stop')
            command(compose + ['rm', '-f', 'worker', 'beat'], name='remove')
        env_file.unlink(missing_ok=True)
