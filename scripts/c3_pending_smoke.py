"""C3 unpatched pending smoke: real API (uvicorn, runtime LOGIN) and Next.js proxy on loopback.

Uses the isolated rasc3 context only (no .env, dev stack, provider or pricing).
Creates its own DB/journal in the context, runs a real operator decision with the
provisioned operator LOGIN through the CLI, and checks /healthz, /readyz and
protected routes via API and proxy during the maintenance hold and after recover.
Never opens safety. Prints codes only.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
API = REPO / 'api'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def fetch(base, path, method='GET'):
    request = Request(base + path, data=b'{}' if method == 'POST' else None, method=method,
                      headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', type=Path, required=True)
    a = p.parse_args()
    if Path(sys.executable).resolve() != Path(r'C:\Users\ADMIN\.conda\envs\ras\python.exe').resolve():
        raise SystemExit('Conda ras required')
    root = a.context.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit('external context required')
    c = json.loads((root / 'credentials.json').read_text(encoding='utf-8'))
    if not c['project'].startswith('rasc3'):
        raise SystemExit('task-owned project required')
    smoke = root / ('smoke-' + secrets.token_hex(3))
    smoke.mkdir()
    journal = smoke / 'journal' / 'revocations.jsonl'
    name = 'c3_smoke_' + secrets.token_hex(4)
    base = {k: v for k, v in os.environ.items() if not k.startswith('RAS_')}
    base.update({'PYTHONPATH': str(API), 'PYTHONUTF8': '1', 'RAS_ENVIRONMENT': 'development', 'RAS_HTTP__COOKIE_SECURE': 'false',
                 'RAS_DB__HOST': '127.0.0.1', 'RAS_DB__PORT': str(c['db_port']), 'RAS_DB__NAME': name,
                 'RAS_RECOVERY__JOURNAL_PATH': str(journal),
                 'RAS_STORAGE__ENDPOINT_URL': f"http://127.0.0.1:{c['minio_port']}",
                 'RAS_STORAGE__ACCESS_KEY': 'task_minio', 'RAS_STORAGE__SECRET_KEY': c['minio']})
    def env(user, password):
        return base | {'RAS_DB__USER': user, 'RAS_DB__PASSWORD': password}
    owner = env('task_admin', c['password'])
    def py(args, e, **kw):
        result = subprocess.run([sys.executable, *args], cwd=API, env=e, capture_output=True, text=True, **kw)
        if result.returncode:
            raise SystemExit(f'step failed: {args[:3]} exit={result.returncode} {result.stderr.strip()[-400:]}')
        return result
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    admin = create_engine(URL.create('postgresql+psycopg', username='task_admin', password=c['password'], host='127.0.0.1',
                                     port=c['db_port'], database='task_control'), isolation_level='AUTOCOMMIT', hide_parameters=True)
    processes = []
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            for role, password in (('c1_runtime', c['runtime']), ('c1_recovery', c['recovery']), ('c3_operator', c['operator'])):
                if not conn.scalar(text('SELECT 1 FROM pg_roles WHERE rolname=:n'), {'n': role}):
                    conn.execute(text(f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS'))
                conn.execute(text(f"ALTER ROLE \"{role}\" PASSWORD '{password}'"))  # disposable synthetic secret
        py(['-m', 'alembic', 'upgrade', 'head'], owner)
        py(['-m', 'app.cli.recovery', 'init'], owner)
        py(['-m', 'app.cli.provision_auth_roles', '--runtime', 'c1_runtime', '--recovery', 'c1_recovery', '--definer', 'c1_definer'], owner)
        operator_id = uuid.uuid4()
        py(['-m', 'app.cli.provision_auth_operator', 'operator', '--login', 'c3_operator', '--operator-id', str(operator_id)], owner)
        # A legacy Admin row (labelled synthetic snapshot data): recover leaves it quarantined/held.
        user_id = uuid.uuid4()
        from app.identity.passwords import hash_password
        db = create_engine(URL.create('postgresql+psycopg', username='task_admin', password=c['password'], host='127.0.0.1',
                                      port=c['db_port'], database=name), hide_parameters=True)
        with db.begin() as conn:
            conn.execute(text("INSERT INTO users(id,email,role,password_verifier) VALUES(:i,'smoke-admin@example.test','admin',:v)"),
                         {'i': user_id, 'v': hash_password(secrets.token_urlsafe(24))})
        recovery_env = env('c1_recovery', c['recovery'])
        py(['-m', 'app.cli.recovery', 'hold'], recovery_env)
        py(['-m', 'app.cli.recovery', 'recover', '--hold-for-operator'], owner)
        with db.connect() as conn:
            journal_id = conn.execute(text('SELECT journal_id FROM recovery_registrations')).scalar_one()
        db.dispose()
        from datetime import UTC, datetime, timedelta
        now = datetime.now(UTC).replace(microsecond=0)
        case = {'schema': 'ras.auth_recovery_case.v1', 'case_id': str(uuid.uuid4()), 'action': 'resolve_initial_status',
                'installation_journal_id': str(journal_id), 'user_id': str(user_id), 'expected_role': 'admin',
                'expected_credential_operation_id': None, 'expected_status_operation_id': None,
                'operator_id': str(operator_id), 'approver_id': str(uuid.uuid4()), 'verification_method': 'synthetic_smoke',
                'approved_at': (now - timedelta(minutes=1)).isoformat(), 'expires_at': (now + timedelta(hours=1)).isoformat()}
        case_file = smoke / 'case.json'
        case_file.write_text(json.dumps(case), encoding='utf-8')
        py(['-m', 'app.cli.provision_auth_operator', 'register-case', '--case-file', str(case_file)], owner)
        operator_env = env('c3_operator', c['operator'])
        result = py(['-m', 'app.cli.auth_recovery', 'resolve-initial-status', '--user-id', str(user_id), '--case-id', case['case_id'],
                     '--case-file', str(case_file), '--expected-installation', str(journal_id)], operator_env)
        assert 'initial status decided:' in result.stdout and 'status_safety=proven' in result.stdout, result.stdout
        print('operator CLI: resolve-initial-status exit 0 (credential still quarantined)', flush=True)
        api_port, web_port = free_port(), free_port()
        runtime_env = env('c1_runtime', c['runtime'])
        log = (smoke / 'api.log').open('w', encoding='utf-8')
        processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(api_port)],
                                          cwd=API, env=runtime_env, stdout=log, stderr=subprocess.STDOUT))
        web_env = {k: v for k, v in os.environ.items() if not k.startswith('RAS_')}
        web_env.update({'API_INTERNAL_URL': f'http://127.0.0.1:{api_port}', 'PORT': str(web_port), 'HOSTNAME': '127.0.0.1'})
        web_log = (smoke / 'web.log').open('w', encoding='utf-8')
        processes.append(subprocess.Popen(['node', str(REPO / 'web/.next/standalone/server.js')], cwd=REPO / 'web/.next/standalone',
                                          env=web_env, stdout=web_log, stderr=subprocess.STDOUT))
        apibase, webbase = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{web_port}'
        deadline = time.monotonic() + 60
        while True:
            try:
                if fetch(apibase, '/healthz')[0] == 200 and fetch(webbase, '/login')[0] == 200:
                    break
            except (URLError, TimeoutError, ConnectionError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('isolated services failed startup')
            time.sleep(1)
        def check(phase):
            checks = [(apibase, '/healthz', 'GET', 200), (apibase, '/readyz', 'GET', 503)]
            for target, prefix in ((apibase, ''), (webbase, '/api')):
                for path, method in (('/auth/login', 'POST'), ('/auth/csrf', 'GET'), ('/library/items', 'GET'),
                                     ('/admin/users', 'GET'), ('/search', 'POST'), ('/rag/ask', 'POST')):
                    checks.append((target, prefix + path, method, 503))
            for target, path, method, expected in checks:
                code, body = fetch(target, path, method)
                assert code == expected, (phase, path, code, expected)
                if expected == 503 and path != '/readyz':
                    assert json.loads(body) == {'error': 'service_recovering'}, (phase, path)
                print(f'pending HTTP [{phase}]:', 'web' if target == webbase else 'api', method, path, code, flush=True)
        check('maintenance-hold')
        py(['-m', 'app.cli.recovery', 'recover'], owner)
        status = subprocess.run([sys.executable, '-m', 'app.cli.recovery', 'status'], cwd=API, env=runtime_env,
                                capture_output=True, text=True)
        assert status.returncode == 3 and 'gate=auth_safety_pending' in status.stdout, status.stdout
        check('after-recover-pending')
        print('safety remains pending; no product bypass; pricing unchanged', flush=True)
    finally:
        for proc in processes:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == '__main__':
    main()
