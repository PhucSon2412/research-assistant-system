"""Isolated SMTP capture server for tests and the M8a smoke only (stdlib).

Never part of the API image or a production mode: it is a test transport that
lets the real ``SmtpDelivery`` talk TLS to a server whose CA exists only in the
test context. Behaviour per connection comes from ``Behaviour`` or, when run as
a script, from ``<store>/control.json`` (so the smoke can switch faults without
restarting): ``reject_rcpt``, ``drop_after_data``, ``data_delay_seconds``,
``greeting_delay_seconds``, ``offer_starttls``.

Captured messages are kept in memory and, with ``store``, written as
``<store>/msg-<n>.eml``. Only synthetic addresses and links are ever captured.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Behaviour:
    implicit_tls: bool = False
    offer_starttls: bool = True
    require_auth: tuple[str, str] | None = None
    reject_rcpt: bool = False
    drop_after_data: bool = False
    data_delay_seconds: float = 0.0
    greeting_delay_seconds: float = 0.0


@dataclass
class Captured:
    mail_from: str
    rcpt: list[str]
    data: bytes
    tls: bool
    auth_user: str | None
    tls_version: str | None = None


@dataclass
class _State:
    messages: list[Captured] = field(default_factory=list)
    connections: int = 0
    errors: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def setup(self):
        server = self.server
        self.behaviour = server.current_behaviour()
        with server.state.lock:
            server.state.connections += 1
        self.tls = False
        if self.behaviour.implicit_tls:
            self.request = server.tls_context.wrap_socket(self.request, server_side=True)
            self.tls = True
        super().setup()

    def _reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def _starttls(self) -> None:
        self._reply("220 ready to start TLS")
        self.request = self.server.tls_context.wrap_socket(self.request, server_side=True)
        self.tls = True
        self.rfile = self.request.makefile("rb", -1)
        self.wfile = self.request.makefile("wb", 0)

    def handle(self):
        b = self.behaviour
        if b.greeting_delay_seconds:
            time.sleep(b.greeting_delay_seconds)
        self._reply("220 capture.test ESMTP synthetic")
        mail_from, rcpt, user = None, [], None
        while True:
            raw = self.rfile.readline(4096)
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            arg = line[len(verb):].strip()
            if verb in ("EHLO", "HELO"):
                caps = ["250-capture.test", "250-8BITMIME", "250-SMTPUTF8"]
                if b.offer_starttls and not self.tls and not b.implicit_tls:
                    caps.append("250-STARTTLS")
                if b.require_auth and self.tls:
                    caps.append("250-AUTH PLAIN")
                caps.append("250 SIZE 10485760")
                self.wfile.write(("\r\n".join(caps) + "\r\n").encode())
                self.wfile.flush()
            elif verb == "STARTTLS" and b.offer_starttls and not self.tls:
                self._starttls()
            elif verb == "AUTH" and b.require_auth and self.tls:
                parts = arg.split(" ", 1)
                try:
                    _, login, secret = base64.b64decode(parts[1]).decode().split("\0")
                except Exception:  # noqa: BLE001
                    self._reply("501 bad auth")
                    continue
                if (login, secret) == b.require_auth:
                    user = login
                    self._reply("235 authenticated")
                else:
                    self._reply("535 authentication failed")
            elif verb == "MAIL":
                if not self.tls:
                    self._reply("530 must issue STARTTLS first")
                elif b.require_auth and user is None:
                    self._reply("530 authentication required")
                else:
                    mail_from, rcpt = arg, []
                    self._reply("250 ok")
            elif verb == "RCPT":
                if b.reject_rcpt:
                    self._reply("550 synthetic recipient rejected")
                else:
                    rcpt.append(arg)
                    self._reply("250 ok")
            elif verb == "DATA":
                if not rcpt:
                    self._reply("503 need RCPT")
                    continue
                self._reply("354 end with <CRLF>.<CRLF>")
                chunks = []
                while True:
                    part = self.rfile.readline(1 << 20)
                    if not part:
                        return
                    if part in (b".\r\n", b".\n"):
                        break
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                if b.data_delay_seconds:
                    time.sleep(b.data_delay_seconds)
                if b.drop_after_data:
                    self.request.shutdown(socket.SHUT_RDWR)
                    return
                version = self.request.version() if self.tls else None
                self.server.store(Captured(mail_from, list(rcpt), b"".join(chunks), self.tls, user, version))
                self._reply("250 queued as synthetic")
            elif verb == "RSET":
                mail_from, rcpt = None, []
                self._reply("250 ok")
            elif verb == "NOOP":
                self._reply("250 ok")
            elif verb == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("502 not implemented")


class CaptureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cert_file: str, key_file: str, *, host: str = "127.0.0.1", port: int = 0,
                 behaviour: Behaviour = Behaviour(), store: Path | None = None) -> None:
        super().__init__((host, port), _Handler)
        self.tls_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls_context.load_cert_chain(cert_file, key_file)
        self.behaviour = behaviour
        self.state = _State()
        self.store_dir = store
        self._thread: threading.Thread | None = None

    def handle_error(self, request, client_address) -> None:
        # Refused handshakes and client aborts are expected in fault tests.
        with self.state.lock:
            self.state.errors += 1

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def messages(self) -> list[Captured]:
        with self.state.lock:
            return list(self.state.messages)

    def current_behaviour(self) -> Behaviour:
        if self.store_dir is not None:
            control = self.store_dir / "control.json"
            if control.exists():
                values = json.loads(control.read_text(encoding="utf-8"))
                return replace(self.behaviour, **{k: v for k, v in values.items() if k in Behaviour.__dataclass_fields__})
        return self.behaviour

    def store(self, message: Captured) -> None:
        with self.state.lock:
            self.state.messages.append(message)
            n = len(self.state.messages)
        if self.store_dir is not None:
            meta = {"mail_from": message.mail_from, "rcpt": message.rcpt, "tls": message.tls,
                    "tls_version": message.tls_version, "auth_user": message.auth_user}
            # Time-ordered names survive a capture restart without overwriting earlier mail.
            stem = f"msg-{time.time_ns():020d}-{n:04d}"
            (self.store_dir / f"{stem}.eml").write_bytes(message.data)
            (self.store_dir / f"{stem}.json").write_text(json.dumps(meta), encoding="utf-8")

    def start(self) -> "CaptureServer":
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--mode", choices=["implicit_tls", "starttls"], required=True)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--auth-user")
    p.add_argument("--auth-password-file")
    a = p.parse_args()
    auth = None
    if a.auth_user:
        auth = (a.auth_user, Path(a.auth_password_file).read_text(encoding="utf-8").rstrip("\r\n"))
    server = CaptureServer(a.cert, a.key, host="0.0.0.0", port=a.port, store=a.store,
                           behaviour=Behaviour(implicit_tls=a.mode == "implicit_tls", require_auth=auth))
    print(f"smtp capture listening on {a.port} ({a.mode})", flush=True)
    server.serve_forever(poll_interval=0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
