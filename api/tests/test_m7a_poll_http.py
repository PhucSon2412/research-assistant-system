"""PR #64 P2-3 re-review: reconcile's deadline bounds whole HTTP requests on real loopback sockets.

A body or header that trickles in with every gap below the read timeout must still be cut at
the absolute deadline; connections close and no worker thread is left behind. No network/key.
"""
import json
import socket
import threading
import time

import pytest

from scripts import m7a_live

DEADLINE = 1.0
TOLERANCE = 0.6  # cancellation/teardown slack on a loaded Windows/Linux host


class Server:
    """Raw HTTP/1.1 loopback server; behaviour per path: fast, drip body, drip headers."""

    def __init__(self, modes):
        self.modes, self.connections, self.closed_by_peer = modes, 0, 0
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        self.connections += 1
        with conn:
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            path = data.split(b' ', 2)[1].decode().split('?')[0]
            mode = self.modes.get(path.rsplit('/', 1)[-1], 'fast')
            body = json.dumps({'data': {'total_cost': 0.001, 'usage': 1.0, 'usage_daily': 0.01}}).encode()
            head = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n')
            try:
                if mode == 'drip_headers':
                    for byte in head:
                        conn.sendall(bytes([byte])); time.sleep(0.08)
                else:
                    conn.sendall(head)
                if mode == 'drip_body':
                    for byte in body:
                        conn.sendall(bytes([byte])); time.sleep(0.08)
                else:
                    conn.sendall(body)
            except OSError:
                self.closed_by_peer += 1  # the client cancelled and closed the connection

    def close(self):
        self.stop.set(); self.sock.close(); self.thread.join(5)


def run(tmp_path, monkeypatch, modes, ids=('gen-synthetic-1',)):
    server = Server(modes)
    ledger = m7a_live.Ledger(tmp_path / 'state')
    ledger.append({'event': 'outcome', 'seq': 1, 'observed': [{'id': i} for i in ids]})
    monkeypatch.setattr(m7a_live, 'BASE', f'http://127.0.0.1:{server.port}/api/v1')
    before = {t.ident for t in threading.enumerate()}
    start = time.monotonic()
    try:
        out = m7a_live.reconcile(tmp_path / 'state', 'synthetic-not-a-key', DEADLINE)
    finally:
        elapsed = time.monotonic() - start
        time.sleep(0.3)
        server.close()
    leaked = [t for t in threading.enumerate() if t.ident not in before and not t.daemon]
    return out, elapsed, leaked


def test_fast_control_finds_ids_and_reads_key(tmp_path, monkeypatch):
    out, elapsed, leaked = run(tmp_path, monkeypatch, {})
    assert elapsed < DEADLINE and len(out['found']) == 1 and out['pending'] == []
    assert out['key_status'] == 200 and out['deadline_reached'] is False and not leaked


@pytest.mark.parametrize('mode', ['drip_body', 'drip_headers'])
def test_trickling_generation_response_is_cut_at_deadline(tmp_path, monkeypatch, mode):
    out, elapsed, leaked = run(tmp_path, monkeypatch, {'generation': mode})
    assert elapsed < DEADLINE + TOLERANCE, elapsed
    assert out['found'] == {} and out['pending'] == ['gen-synthetic-1']
    assert out['deadline_reached'] is True and out['key_status'] in ('skipped_deadline', 'deadline')
    assert not leaked


def test_slow_key_after_ids_found_is_cut_and_found_ids_kept(tmp_path, monkeypatch):
    out, elapsed, leaked = run(tmp_path, monkeypatch, {'key': 'drip_body'}, ids=('gen-a', 'gen-b'))
    assert elapsed < DEADLINE + TOLERANCE, elapsed
    assert set(out['found']) == {'gen-a', 'gen-b'} and out['pending'] == []
    assert out['key_status'] == 'deadline' and out['deadline_reached'] is True and out['key'] == {
        'usage': None, 'usage_daily': None, 'limit_remaining': None}
    assert not leaked
