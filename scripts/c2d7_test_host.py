"""Disposable PG/MinIO/Redis setup and Conda pytest runner; never reads .env.

Usage: --context <external dir> setup | test <pytest arguments>
Credentials, journal and raw evidence stay in that directory. No dev operations.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def run(argv, **kw):
    return subprocess.run(argv, check=True, text=True, encoding='utf-8', **kw)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('action', choices=['setup', 'test'])
    p.add_argument('args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if Path(sys.executable).resolve() != Path(r'C:\Users\ADMIN\.conda\envs\ras\python.exe').resolve():
        raise SystemExit('Conda ras required')
    root = a.context.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit('external context required')
    root.mkdir(parents=True, exist_ok=True)
    credentials = root / 'credentials.json'
    if a.action == 'setup':
        if credentials.exists():
            raise SystemExit('context already exists')
        c = {k: secrets.token_hex(24) for k in ('password', 'runtime', 'recovery', 'minio', 'redis')}
        c['project'] = 'rasc2d7' + secrets.token_hex(4)
        for service in ('db', 'minio', 'redis'):
            c[service+'_port'] = free_port()
        credentials.write_text(json.dumps(c), encoding='utf-8')
        (root / 'empty.env').write_text('', encoding='utf-8')
        label = {'ras.task': c['project']}
        compose = {'services': {
            'db': {'image': 'pgvector/pgvector:pg16', 'environment': {'POSTGRES_USER': 'task_admin',
                'POSTGRES_PASSWORD': c['password'], 'POSTGRES_DB': 'task_control'},
                'ports': [f"127.0.0.1:{c['db_port']}:5432"], 'labels': label,
                'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U task_admin -d task_control'], 'interval': '1s', 'retries': 60}},
            'minio': {'image': 'quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z',
                'command': ['server', '/data'], 'environment': {'MINIO_ROOT_USER': 'task_minio', 'MINIO_ROOT_PASSWORD': c['minio']},
                'ports': [f"127.0.0.1:{c['minio_port']}:9000"], 'labels': label,
                'healthcheck': {'test': ['CMD', 'mc', 'ready', 'local'], 'interval': '1s', 'retries': 60}},
            'redis': {'image': 'redis:7-alpine', 'command': ['sh', '-c', 'exec redis-server --appendonly yes --requirepass "$$REDIS_PASSWORD"'],
                'environment': {'REDIS_PASSWORD': c['redis'], 'REDISCLI_AUTH': c['redis']},
                'ports': [f"127.0.0.1:{c['redis_port']}:6379"], 'labels': label,
                'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'], 'interval': '1s', 'retries': 60}}},
            'networks': {'default': {'labels': label}}}
        (root / 'compose.json').write_text(json.dumps(compose), encoding='utf-8')
        command = ['docker', 'compose', '--env-file', str(root/'empty.env'), '-p', c['project'], '-f', str(root/'compose.json')]
        run(command + ['up', '-d', '--wait'], capture_output=True)
        for service, port in [('db',5432),('minio',9000),('redis',6379)]:
            c[service+'_port'] = int(run(command + ['port',service,str(port)], capture_output=True).stdout.strip().rsplit(':',1)[1])
        credentials.write_text(json.dumps(c), encoding='utf-8')
        ids = run(command + ['ps','-q'], capture_output=True).stdout.split()
        facts = json.loads(run(['docker','inspect',*ids], capture_output=True).stdout)
        public = [dict(Id=f['Id'], Name=f['Name'], Image=f['Image'], StartedAt=f['State']['StartedAt'],
                       Mounts=[m.get('Name') for m in f['Mounts']], Labels=f['Config']['Labels']) for f in facts]
        (root/'isolated-containers.json').write_text(json.dumps(public, indent=2), encoding='utf-8')
        print('isolated setup ready: ' + c['project'])
        return 0
    c = json.loads(credentials.read_text(encoding='utf-8'))
    if not c['project'].startswith('rasc2d7'):
        raise SystemExit('task-owned project required')
    if any(c[s+'_port'] <= 1024 or c[s+'_port'] in (5432, 9000, 6379) for s in ('db','minio','redis')):
        raise SystemExit('refusing absent/default host port')
    command = ['docker','compose','--env-file',str(root/'empty.env'),'-p',c['project'],'-f',str(root/'compose.json')]
    for service, port in [('db',5432),('minio',9000),('redis',6379)]:
        observed=int(run(command+['port',service,str(port)],capture_output=True).stdout.strip().rsplit(':',1)[1])
        if observed!=c[service+'_port']:
            raise SystemExit('isolated port binding changed')
    from sqlalchemy.engine import URL
    env = os.environ.copy()
    env.pop('RAS_DATABASE_URL', None)
    env.update({'RAS_TEST_DATABASE_ADMIN_URL': URL.create('postgresql+psycopg', username='task_admin',
        password=c['password'],host='127.0.0.1',port=c['db_port'],database='task_control').render_as_string(hide_password=False),
        'RAS_DB__HOST':'127.0.0.1','RAS_DB__PORT':str(c['db_port']), 'RAS_DB__USER':'task_admin',
        'RAS_DB__PASSWORD':c['password'],'RAS_DB__NAME':'task_control',
        'RAS_C1_RUNTIME_PASSWORD':c['runtime'],'RAS_C1_RECOVERY_PASSWORD':c['recovery'],
        'MINIO_ROOT_USER':'task_minio','MINIO_ROOT_PASSWORD':c['minio'],'MINIO_API_HOST_PORT':str(c['minio_port']),
        'RAS_STORAGE__ENDPOINT_URL':f"http://127.0.0.1:{c['minio_port']}",
        'RAS_STORAGE__ACCESS_KEY':'task_minio','RAS_STORAGE__SECRET_KEY':c['minio'],
        'RAS_REDIS_HOST':'127.0.0.1','RAS_REDIS_PORT':str(c['redis_port']), 'RAS_REDIS_PASSWORD':c['redis'],
        'RAS_EMBEDDING__API_KEY':'','RAS_EMBEDDING__PRICING_VERIFIED':'false',
        'RAS_C2D5D2A_CONTEXT':str(root), 'RAS_C2D5D2B_CONTEXT':str(root), 'RAS_C2D5E1_CONTEXT':str(root), 'RAS_C2D5E2_CONTEXT':str(root), 'RAS_C2D5F1_CONTEXT':str(root), 'RAS_C2D5F2_CONTEXT':str(root), 'RAS_C2D7_CONTEXT':str(root),
        'PYTHONPATH':str(REPO/'api'), 'PYTHONUTF8':'1', 'PYTHONUNBUFFERED':'1'})
    # A dedicated bootstrap module blocks outbound httpx in pytest AND its children.
    code = """import httpx, ipaddress, pytest, sys
def guard(original):
 def checked(self, request, *a, **kw):
  try: allowed=ipaddress.ip_address(request.url.host).is_loopback
  except ValueError: allowed=request.url.host in ('testserver','web.test')
  if not allowed: raise AssertionError('external HTTP blocked')
  return original(self,request,*a,**kw)
 return checked
httpx.HTTPTransport.handle_request=guard(httpx.HTTPTransport.handle_request)
httpx.AsyncHTTPTransport.handle_async_request=guard(httpx.AsyncHTTPTransport.handle_async_request)
raise SystemExit(pytest.main(sys.argv[1:]))
"""
    return subprocess.call([sys.executable,'-c',code,*a.args], cwd=REPO, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
