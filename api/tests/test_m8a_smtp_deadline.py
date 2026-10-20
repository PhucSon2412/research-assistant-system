"""PR #66 P2-1: one hard deadline for the whole SMTP send (reviewer repro + extensions).

Synthetic loopback SMTP over TLS verified with a throwaway CA. Faults: a
DATA reply dripped byte by byte, MAIL/RCPT/DATA replies each slow so that
their sum exceeds the deadline, and DNS slowed or hung inside the actual
sending child process (a ``sitecustomize`` on its ``PYTHONPATH``). Each must
end as ``smtp_timeout`` within deadline + a small scheduling margin, the
child must be reaped and no further connection may follow. The server may
already hold the DATA it received: that is the known unknown-outcome case
the invitation compensation covers; it is never reported as success.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.config import SmtpSettings
from app.delivery import DeliveryFailed, MessageKind, OutgoingMessage, SmtpDelivery
from tests.m8a_tls import make_ca, make_leaf
from tests.smtp_capture import Behaviour, CaptureServer, _Handler

DEADLINE = 0.6
MARGIN = 0.5  # scheduling + kill/reap of the child; far below the multi-second repro


class DripReply(_Handler):
    def _reply(self, line):
        if line.startswith("250 queued"):
            for c in (line + "\r\n").encode():
                self.wfile.write(bytes([c]))
                self.wfile.flush()
                time.sleep(0.08)
        else:
            super()._reply(line)


class CumulativeReply(_Handler):
    def _reply(self, line):
        if line in ("250 ok", "354 end with <CRLF>.<CRLF>") or line.startswith("250 queued"):
            time.sleep(0.18)
        super()._reply(line)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("m8a-deadline-pki")
    ca = make_ca(root)
    return {"ca": ca, "ip": make_leaf(root, ca, "smtp", ["127.0.0.1"]),
            "dns": make_leaf(root, ca, "dns", ["dns.fixture.test"]), "root": root}


def message():
    return OutgoingMessage(MessageKind.INVITATION, "fixture@ras.test", "https://ras.test/invite#token=fixture-only",
                           datetime.now(UTC) + timedelta(hours=1))


def run(pki, *, handler=_Handler, security="implicit_tls", host="127.0.0.1", leaf="ip", deadline=DEADLINE):
    server = CaptureServer(str(pki[leaf].cert), str(pki[leaf].key),
                           behaviour=Behaviour(implicit_tls=security == "implicit_tls"))
    server.RequestHandlerClass = handler
    server.start()
    cfg = SmtpSettings(host=host, port=server.port, security=security, sender="noreply@ras.test",
                       helo_name="review.test", ca_file=pki["ca"].cert,
                       connect_timeout_seconds=min(0.5, deadline), total_timeout_seconds=deadline)
    adapter = SmtpDelivery(cfg)
    started = time.monotonic()
    outcome = "success"
    try:
        adapter.send(message(), now=datetime.now(UTC))
    except DeliveryFailed as exc:
        outcome = exc.code
    elapsed = time.monotonic() - started
    try:
        time.sleep(0.8)  # nothing may keep talking to the server after the outcome
        return {"outcome": outcome, "elapsed": elapsed, "connections": server.state.connections,
                "child_exit": child.poll() if (child := getattr(adapter, "last_child", None)) else "no-child"}
    finally:
        server.stop()


def test_fast_control_succeeds_within_the_deadline(pki):
    facts = run(pki)
    assert facts["outcome"] == "success" and facts["elapsed"] < DEADLINE, facts
    assert facts["child_exit"] in (0, "no-child") and facts["connections"] == 1


@pytest.mark.parametrize("security", ["implicit_tls", "starttls"])
def test_dripped_data_reply_cannot_succeed_after_the_deadline(pki, security):
    facts = run(pki, handler=DripReply, security=security)
    assert facts["outcome"] == "smtp_timeout", facts
    assert facts["elapsed"] < DEADLINE + MARGIN, facts
    assert facts["child_exit"] is not None and facts["connections"] == 1, facts


def test_internal_commands_share_one_deadline(pki):
    facts = run(pki, handler=CumulativeReply)
    assert facts["outcome"] == "smtp_timeout" and facts["elapsed"] < DEADLINE + MARGIN, facts
    assert facts["child_exit"] is not None and facts["connections"] == 1, facts


def test_the_same_slow_commands_succeed_with_a_longer_deadline(pki):
    facts = run(pki, handler=CumulativeReply, deadline=3)
    assert facts["outcome"] == "success" and facts["child_exit"] in (0, "no-child"), facts


@pytest.mark.parametrize("dns_seconds", [2, 3600])
def test_slow_or_hung_dns_is_inside_the_deadline(pki, tmp_path, monkeypatch, dns_seconds):
    # Fault injection inside the real sending child: its interpreter loads this
    # sitecustomize from PYTHONPATH (no secret is involved in the hook).
    hook = tmp_path / "dnshook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import socket, time\n_orig = socket.getaddrinfo\n"
        "def _slow(host, *a, **k):\n"
        "    if host == 'dns.fixture.test':\n"
        f"        time.sleep({dns_seconds})\n"
        "        return _orig('127.0.0.1', *a, **k)\n"
        "    return _orig(host, *a, **k)\n"
        "socket.getaddrinfo = _slow\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(hook), os.environ.get("PYTHONPATH", "")]))
    facts = run(pki, host="dns.fixture.test", leaf="dns")
    assert facts["outcome"] == "smtp_timeout" and facts["elapsed"] < DEADLINE + MARGIN, facts
    assert facts["child_exit"] is not None and facts["connections"] == 0, facts


def test_a_result_after_the_deadline_is_never_success(pki, monkeypatch):
    """The child reports DATA accepted, but the adapter's clock says the deadline passed."""
    import types
    from app import delivery

    calls = []

    def monotonic():
        calls.append(None)
        return 0.0 if len(calls) <= 2 else 1000.0  # deadline + timeout computed, then "late"

    monkeypatch.setattr(delivery, "time", types.SimpleNamespace(monotonic=monotonic))
    server = CaptureServer(str(pki["ip"].cert), str(pki["ip"].key), behaviour=Behaviour(implicit_tls=True)).start()
    try:
        with pytest.raises(DeliveryFailed) as exc:
            SmtpDelivery(SmtpSettings(host="127.0.0.1", port=server.port, security="implicit_tls",
                                      sender="noreply@ras.test", ca_file=pki["ca"].cert,
                                      total_timeout_seconds=5)).send(message(), now=datetime.now(UTC))
    finally:
        server.stop()
    assert exc.value.code == "smtp_timeout" and len(server.messages) == 1


def test_child_gets_no_service_secrets_and_password_only_on_stdin(pki, tmp_path, monkeypatch):
    hook = tmp_path / "envhook"
    hook.mkdir()
    out = tmp_path / "child-env.txt"
    (hook / "sitecustomize.py").write_text(
        "import os, sys\n"
        f"open({str(out)!r}, 'w').write(repr(sorted(os.environ)) + '\\n' + repr(sys.argv))\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(hook), os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("RAS_DB__PASSWORD", "must-not-reach-the-child")
    secret = tmp_path / "smtp-password"
    secret.write_text("synthetic-smtp-secret", encoding="utf-8")
    server = CaptureServer(str(pki["ip"].cert), str(pki["ip"].key),
                           behaviour=Behaviour(implicit_tls=True, require_auth=("relay", "synthetic-smtp-secret")))
    server.start()
    try:
        SmtpDelivery(SmtpSettings(host="127.0.0.1", port=server.port, security="implicit_tls",
                                  sender="noreply@ras.test", ca_file=pki["ca"].cert, username="relay",
                                  password_file=secret, total_timeout_seconds=5)).send(message(), now=datetime.now(UTC))
    finally:
        server.stop()
    seen = out.read_text(encoding="utf-8")
    assert server.messages[0].auth_user == "relay"
    assert "RAS_DB__PASSWORD" not in seen and "synthetic-smtp-secret" not in seen
