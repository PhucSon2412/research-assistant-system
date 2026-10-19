"""PR #64 P2-3 shutdown: a resolver that never returns must not keep the process alive.

Each case runs reconcile in a real child interpreter (key on stdin only) and requires a result
within the deadline AND process exit within a bound; the parent kills/reaps on overrun so the
suite cannot hang. DNS hang is fault injection (sitecustomize), not a real DNS failure.
"""
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SECRET = 'sk-or-v1-synthetic-not-a-real-key-0123456789'
DEADLINE = 3.0
EXIT_BOUND = 20.0  # deadline + child start-up/teardown on a loaded Windows host

HANG = textwrap.dedent('''
    import os, socket, threading
    if os.environ.get('M7A_TEST_DNS_HANG') == '1':
        def getaddrinfo(*args, **kwargs):
            threading.Event().wait()  # fault injection: the resolver never returns
        socket.getaddrinfo = getaddrinfo
''')

CHILD = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1] + '/api']
    import m7a_live
    m7a_live.BASE = sys.argv[3]
    key = sys.stdin.readline().strip()
    state = Path(sys.argv[2])
    m7a_live.Ledger(state).append({'event': 'outcome', 'seq': 1, 'observed': [{'id': 'gen-synthetic-1'}]})
    start = time.monotonic()
    out = m7a_live.reconcile(state, key, float(sys.argv[4]))
    print('RESULT ' + json.dumps({'elapsed': time.monotonic() - start, 'pending': out['pending'],
          'found': sorted(out['found']), 'deadline_reached': out['deadline_reached'],
          'key_status': out['key_status']}), flush=True)
''')


def run_child(tmp_path, base, hang):
    site = tmp_path / 'site'
    site.mkdir()
    (site / 'sitecustomize.py').write_text(HANG, encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OPENROUTER', 'RAS_'))}
    env.update(PYTHONPATH=str(site), M7A_TEST_DNS_HANG='1' if hang else '0', PYTHONUNBUFFERED='1')
    proc = subprocess.Popen([sys.executable, '-c', CHILD, str(REPO), str(tmp_path / 'state'), base, str(DEADLINE)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    start = time.monotonic()
    try:
        out, err = proc.communicate(SECRET + '\n', timeout=EXIT_BOUND)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()  # reap
        raise AssertionError(f'process did not exit within {EXIT_BOUND}s; stdout={out[-500:]!r}')
    exited = time.monotonic() - start
    lines = [line for line in out.splitlines() if line.startswith('RESULT ')]
    assert proc.returncode == 0 and lines, (proc.returncode, err[-2000:])
    assert SECRET not in out and SECRET not in err
    return json.loads(lines[-1][7:]), exited


def test_hanging_resolver_returns_pending_by_deadline_and_process_exits(tmp_path):
    result, exited = run_child(tmp_path, 'http://m7a-hang.invalid/api/v1', hang=True)
    assert result['elapsed'] < DEADLINE + 1.0, result
    assert result['pending'] == ['gen-synthetic-1'] and result['deadline_reached'] is True
    assert exited < EXIT_BOUND


def test_normal_resolver_control_finds_id_and_exits(tmp_path):
    body = json.dumps({'data': {'total_cost': 0.001, 'usage': 1.0, 'usage_daily': 0.01}}).encode()
    server = socket.create_server(('127.0.0.1', 0))
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            with conn:
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        # 'localhost' goes through the (non-hanging) resolver like a real host name.
        result, exited = run_child(tmp_path, f'http://localhost:{port}/api/v1', hang=False)
    finally:
        stop.set(); server.close(); thread.join(5)
    assert result['found'] == ['gen-synthetic-1'] and result['pending'] == []
    assert result['key_status'] == 200 and result['deadline_reached'] is False
    assert exited < EXIT_BOUND
