"""M8a SMTP adapter against an isolated TLS capture server (no DB, no real mail).

Covers verified TLS (implicit and required STARTTLS), wrong CA and wrong
hostname, a server without STARTTLS, slow server / total deadline, recipient
reject, connection drop after DATA, AUTH from a password file, and that only
stable codes (never recipient, link or token) reach exceptions and logs.
"""

from __future__ import annotations

import email
import email.policy
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import Delivery, Settings, SmtpSettings
from app.delivery import DeliveryFailed, MessageKind, OutgoingMessage, SmtpDelivery, make_delivery
from tests.m8a_tls import make_ca, make_leaf
from tests.smtp_capture import Behaviour, CaptureServer

TOKEN = "SyntheticTokenValue_ABCdef0123456789"
LINK = f"https://ras.test/invite#token={TOKEN}"
RECIPIENT = "invitee@example.test"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("m8a-pki")
    ca = make_ca(root)
    other = make_ca(root, "other-ca", "Unrelated synthetic root")
    return {"ca": ca, "other": other, "good": make_leaf(root, ca, "smtp", ["smtp.test", "127.0.0.1"]),
            "wrong_name": make_leaf(root, ca, "wrong", ["elsewhere.test"]), "root": root}


@pytest.fixture
def capture(pki):
    servers = []

    def start(behaviour: Behaviour = Behaviour(), cert="good"):
        server = CaptureServer(str(pki[cert].cert), str(pki[cert].key), behaviour=behaviour).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def smtp(pki, server, **overrides) -> SmtpSettings:
    values = dict(host="127.0.0.1", port=server.port, security="starttls", sender="no-reply@ras.test",
                  sender_name="RAS thử", ca_file=pki["ca"].cert, connect_timeout_seconds=2, total_timeout_seconds=3)
    values.update(overrides)
    return SmtpSettings(**values)


def message(kind=MessageKind.INVITATION) -> OutgoingMessage:
    return OutgoingMessage(kind, RECIPIENT, LINK, datetime(2026, 10, 22, 8, 30, tzinfo=UTC))


def send(cfg, msg=None):
    SmtpDelivery(cfg).send(msg or message(), now=datetime.now(UTC))


def assert_code(exc_info, code):
    assert exc_info.value.code == code
    text = f"{exc_info.value!r} {exc_info.value}"
    assert RECIPIENT not in text and TOKEN not in text and exc_info.value.__cause__ is None


@pytest.mark.parametrize("security", ["starttls", "implicit_tls"])
def test_verified_tls_delivers_one_message_with_fragment_link(pki, capture, security):
    server = capture(Behaviour(implicit_tls=security == "implicit_tls"))
    send(smtp(pki, server, security=security))
    [captured] = server.messages
    assert captured.tls and captured.tls_version in ("TLSv1.2", "TLSv1.3")
    assert captured.rcpt == [f"TO:<{RECIPIENT}>"] and captured.mail_from.startswith("FROM:<no-reply@ras.test>")
    parsed = email.message_from_bytes(captured.data, policy=email.policy.default)
    assert parsed["To"] == RECIPIENT and "no-reply@ras.test" in parsed["From"]
    assert parsed["Subject"] == "Lời mời tham gia Research Assistant System"
    assert parsed["Auto-Submitted"] == "auto-generated" and parsed["Message-ID"].endswith("@ras.test>")
    body = parsed.get_content()
    assert LINK in body and "?token" not in body and "2026-10-22 08:30" in body


def test_reset_message_subject_and_link(pki, capture):
    server = capture()
    send(smtp(pki, server), message(MessageKind.PASSWORD_RESET))
    parsed = email.message_from_bytes(server.messages[0].data, policy=email.policy.default)
    assert parsed["Subject"].startswith("Đặt lại mật khẩu") and LINK in parsed.get_content()


def test_unknown_ca_is_refused_before_any_mail_command(pki, capture):
    server = capture()
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server, ca_file=pki["other"].cert))
    assert_code(exc, "smtp_tls_verify_failed")
    assert server.messages == []


def test_system_trust_store_does_not_trust_the_test_ca(pki, capture):
    server = capture(Behaviour(implicit_tls=True))
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server, security="implicit_tls", ca_file=None))
    assert_code(exc, "smtp_tls_verify_failed")


def test_hostname_mismatch_is_refused(pki, capture):
    server = capture(cert="wrong_name")
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server))
    assert_code(exc, "smtp_tls_verify_failed")
    assert server.messages == []


def test_starttls_required_when_server_does_not_offer_it(pki, capture):
    server = capture(Behaviour(offer_starttls=False))
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server))
    assert_code(exc, "smtp_starttls_unavailable")
    assert server.messages == []


@pytest.mark.timing
def test_slow_server_stops_at_the_total_deadline(pki, capture):
    server = capture(Behaviour(data_delay_seconds=5))
    started = time.monotonic()
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server, total_timeout_seconds=1.5, connect_timeout_seconds=1))
    elapsed = time.monotonic() - started
    assert_code(exc, "smtp_timeout")
    assert elapsed < 3.5, elapsed


def test_slow_greeting_counts_against_the_deadline(pki, capture):
    server = capture(Behaviour(greeting_delay_seconds=4))
    started = time.monotonic()
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server, total_timeout_seconds=1, connect_timeout_seconds=1))
    assert_code(exc, "smtp_timeout")
    assert time.monotonic() - started < 3


def test_recipient_reject_is_a_failure(pki, capture):
    server = capture(Behaviour(reject_rcpt=True))
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server))
    assert_code(exc, "smtp_rejected")


def test_connection_drop_after_data_is_a_failure_not_success(pki, capture):
    server = capture(Behaviour(drop_after_data=True))
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server))
    assert_code(exc, "smtp_connection_failed")


def test_refused_connection_is_a_failure(pki, capture):
    server = capture()
    port = server.port
    server.stop()
    with pytest.raises(DeliveryFailed) as exc:
        # Windows answers a closed local port only after SYN retries (~2 s).
        send(smtp(pki, server, port=port, connect_timeout_seconds=8, total_timeout_seconds=9))
    assert_code(exc, "smtp_connection_failed")


def test_auth_password_comes_from_file_and_wrong_password_fails(pki, capture, tmp_path):
    secret = tmp_path / "smtp-password"
    secret.write_text("synthetic-smtp-secret\n", encoding="utf-8")
    server = capture(Behaviour(require_auth=("relay-user", "synthetic-smtp-secret")))
    send(smtp(pki, server, username="relay-user", password_file=secret))
    assert server.messages[0].auth_user == "relay-user"
    secret.write_text("other", encoding="utf-8")
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server, username="relay-user", password_file=secret))
    assert_code(exc, "smtp_auth_failed")
    secret.unlink()
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server, username="relay-user", password_file=secret))
    assert_code(exc, "smtp_secret_unavailable")


def test_header_injection_in_recipient_is_refused(pki, capture):
    server = capture()
    bad = OutgoingMessage(MessageKind.INVITATION, "a@example.test\r\nBcc: x@example.test", LINK,
                          datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(DeliveryFailed) as exc:
        send(smtp(pki, server), bad)
    assert exc.value.code == "smtp_invalid_message" and server.messages == []


def test_failures_log_nothing_from_the_adapter(pki, capture, caplog):
    server = capture(Behaviour(reject_rcpt=True))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DeliveryFailed):
            send(smtp(pki, server))
    assert RECIPIENT not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("overrides", [
    {"security": "none"}, {"security": "plain"}, {"verify": False}, {"verify_certificate": False},
    {"ca_file": "relative/ca.pem"}, {"username": "only-user"}, {"sender": "Name <x@y.test>"},
    {"host": "smtp.test\r\nRCPT"}, {"connect_timeout_seconds": 20, "total_timeout_seconds": 5},
])
def test_settings_refuse_plaintext_unverified_or_unsafe_values(overrides):
    values = dict(host="smtp.test", port=587, security="starttls", sender="no-reply@ras.test")
    values.update(overrides)
    with pytest.raises(ValidationError):
        SmtpSettings(**values)


def test_smtp_mode_requires_settings_and_https_link_base_in_production(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith("RAS_"):
            monkeypatch.delenv(key)
    base = dict(database_url="postgresql+psycopg://u@h/db")
    with pytest.raises(ValidationError):
        Delivery(mode="smtp")
    cfg = {"host": "smtp.test", "port": 465, "security": "implicit_tls", "sender": "no-reply@ras.test"}
    ok = Settings(**base, delivery={"mode": "smtp", "smtp": cfg},
                  http={"public_base_url": "https://ras.test/", "allowed_origins": ["https://ras.test"]})
    assert ok.http.public_base_url == "https://ras.test"
    assert isinstance(make_delivery(ok), SmtpDelivery)
    for http in ({"public_base_url": "http://ras.test", "allowed_origins": ["http://ras.test"]},
                 {"public_base_url": "https://ras.test", "allowed_origins": ["https://other.test"]},
                 {"public_base_url": "https://ras.test/app", "allowed_origins": ["https://ras.test"]},
                 {"public_base_url": "https://ras.test", "allowed_origins": ["https://ras.test", "http://ras.test"]},
                 {"allowed_origins": ["https://ras.test"]}):
        with pytest.raises(ValidationError):
            Settings(**base, delivery={"mode": "smtp", "smtp": cfg}, http=http)
    with pytest.raises(ValidationError):
        Settings(**base, environment="production", http={"cookie_secure": False})
    with pytest.raises(ValidationError):
        Settings(**base, rate_limit={"mode": "disabled"})
    dev = Settings(**base, environment="development", rate_limit={"mode": "disabled"},
                   http={"cookie_secure": False, "public_base_url": "http://127.0.0.1:3000",
                         "allowed_origins": ["http://127.0.0.1:3000"]}, delivery={"mode": "dev_outbox"})
    assert dev.rate_limit.mode == "disabled"
