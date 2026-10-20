"""Delivery of invitation/reset links.

- ``SmtpDelivery`` (M8a): one plain-text email per link over SMTP with
  mandatory, verified TLS (SMTPS or required STARTTLS), sent by a child
  process under one hard deadline. A server accepting a message is not proof
  the recipient received it. Failures raise ``DeliveryFailed`` carrying only
  a stable code; nothing is retried here.
- ``DisabledDelivery``: nothing can be sent. Creating an invitation is
  refused; a reset request still answers generically but sends nothing.
- ``DevOutbox`` (RAS_ENVIRONMENT=development only): messages stay in API
  process memory, bounded and dropped at token expiry, readable only by an
  Admin through ``GET /admin/dev-outbox``. They are never written to the
  database or to logs, so raw tokens stay out of both. Restarting the API
  empties it.

A link carries the token in the URL fragment (``#token=``), which browsers
do not send to servers, so it never reaches web/API access logs.
"""

from __future__ import annotations

import base64
import email.policy
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

from app.config import Settings, SmtpSettings, get_settings


class MessageKind(StrEnum):
    INVITATION = "invitation"
    PASSWORD_RESET = "password_reset"


@dataclass(frozen=True)
class OutgoingMessage:
    kind: MessageKind
    recipient: str = field(repr=False)
    link: str = field(repr=False)
    expires_at: datetime


class DeliveryUnavailable(Exception):
    pass


class DeliveryFailed(Exception):
    """A send that did not end with the server accepting the message.

    ``code`` is the only detail kept: SMTP replies and exception text can echo
    the recipient, so they never reach logs or callers. A timeout or dropped
    connection after DATA may still have queued the message: callers treat
    every failure as not delivered and do not retry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeliveryAdapter(Protocol):
    enabled: bool

    def send(self, message: OutgoingMessage, *, now: datetime) -> None: ...


class DisabledDelivery:
    enabled = False

    def send(self, message: OutgoingMessage, *, now: datetime) -> None:
        raise DeliveryUnavailable("no delivery channel is configured")


@dataclass(frozen=True)
class OutboxEntry:
    id: int
    created_at: datetime
    message: OutgoingMessage


class DevOutbox:
    enabled = True

    def __init__(self, max_entries: int) -> None:
        self._entries: deque[OutboxEntry] = deque(maxlen=max_entries)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def send(self, message: OutgoingMessage, *, now: datetime) -> None:
        with self._lock:
            self._entries.append(OutboxEntry(id=next(self._ids), created_at=now, message=message))

    def entries(self, *, now: datetime) -> list[OutboxEntry]:
        """Unexpired entries, newest first; expired ones are dropped."""
        with self._lock:
            live = [e for e in self._entries if e.message.expires_at > now]
            self._entries.clear()
            self._entries.extend(live)
            return list(reversed(live))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_SUBJECTS = {
    MessageKind.INVITATION: "Lời mời tham gia Research Assistant System",
    MessageKind.PASSWORD_RESET: "Đặt lại mật khẩu Research Assistant System",
}
_BODIES = {
    MessageKind.INVITATION: (
        "Bạn được mời tạo tài khoản Research Assistant System.\n\n"
        "Mở liên kết sau để đặt mật khẩu. Liên kết dùng một lần và hết hạn lúc {expires} (UTC):\n\n{link}\n\n"
        "Nếu bạn không chờ lời mời này, hãy bỏ qua email.\n"),
    MessageKind.PASSWORD_RESET: (
        "Có yêu cầu đặt lại mật khẩu cho tài khoản Research Assistant System của bạn.\n\n"
        "Mở liên kết sau để đặt mật khẩu mới. Liên kết dùng một lần và hết hạn lúc {expires} (UTC):\n\n{link}\n\n"
        "Nếu bạn không yêu cầu, hãy bỏ qua email; mật khẩu hiện tại không đổi.\n"),
}
_RECIPIENT = re.compile(r"[^@\s<>,;\"]+@[^@\s<>,;\"]+")


def compose_message(message: OutgoingMessage, cfg: SmtpSettings, *, now: datetime) -> EmailMessage:
    if not _RECIPIENT.fullmatch(message.recipient) or any(c in message.link for c in "\r\n"):
        raise DeliveryFailed("smtp_invalid_message")
    try:
        msg = EmailMessage(policy=email.policy.SMTP)
        msg["From"] = Address(cfg.sender_name or "", addr_spec=cfg.sender)
        msg["To"] = Address(addr_spec=message.recipient)
        msg["Subject"] = _SUBJECTS[message.kind]
        msg["Date"] = format_datetime(now.astimezone(UTC))
        msg["Message-ID"] = make_msgid(domain=cfg.sender.rsplit("@", 1)[1])
        msg["Auto-Submitted"] = "auto-generated"
        expires = message.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        msg.set_content(_BODIES[message.kind].format(expires=expires, link=message.link))
    except (ValueError, TypeError, IndexError):
        raise DeliveryFailed("smtp_invalid_message") from None
    return msg


class SmtpDelivery:
    """SMTP over verified TLS; never plaintext, never unverified.

    The whole send (DNS, connect, greeting, TLS, EHLO, AUTH, MAIL/RCPT/DATA,
    every reply) runs in one child process (``app.smtp_transport``) that this
    adapter owns. ``total_timeout_seconds`` is enforced here: at the deadline
    the child is killed and reaped and the outcome is ``smtp_timeout``, even if
    the server may already have accepted DATA (callers compensate, never
    retry). A result that arrives after the deadline is never a success. The
    job and password go to the child over its stdin only. The caller holds no
    database session, row or journal lock while ``send`` runs.
    """

    enabled = True

    def __init__(self, cfg: SmtpSettings) -> None:
        self._cfg = cfg
        # Observability for tests: the last child process (exit state only).
        self.last_child: subprocess.Popen | None = None

    def _password(self) -> str:
        try:
            with open(self._cfg.password_file, "rb") as handle:  # type: ignore[arg-type]
                raw = handle.read(self._cfg.password_max_bytes + 1)
            if len(raw) > self._cfg.password_max_bytes:
                raise ValueError
            value = raw.decode("utf-8").rstrip("\r\n")
        except (OSError, ValueError, UnicodeDecodeError):
            raise DeliveryFailed("smtp_secret_unavailable") from None
        if not value:
            raise DeliveryFailed("smtp_secret_unavailable")
        return value

    def send(self, message: OutgoingMessage, *, now: datetime) -> None:
        cfg = self._cfg
        deadline = time.monotonic() + cfg.total_timeout_seconds
        msg = compose_message(message, cfg, now=now)
        password = self._password() if cfg.username else None
        job = json.dumps({
            "host": cfg.host, "port": cfg.port, "security": cfg.security, "helo_name": cfg.helo_name,
            "ca_file": str(cfg.ca_file) if cfg.ca_file else None, "sender": cfg.sender,
            "recipient": message.recipient, "username": cfg.username, "password": password,
            "connect_timeout_seconds": cfg.connect_timeout_seconds,
            "total_timeout_seconds": cfg.total_timeout_seconds,
            "message": base64.b64encode(msg.as_bytes(policy=email.policy.SMTP)).decode("ascii"),
        }).encode("utf-8")
        try:
            child = subprocess.Popen([sys.executable, "-m", "app.smtp_transport"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_child_env())
        except OSError:
            raise DeliveryFailed("smtp_transport_unavailable") from None
        self.last_child = child
        try:
            out, _ = child.communicate(job, timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _reap(child)
            raise DeliveryFailed("smtp_timeout") from None
        except BaseException:
            _reap(child)
            raise
        if time.monotonic() > deadline:
            raise DeliveryFailed("smtp_timeout")  # late answers are never a success
        try:
            result = json.loads(out)["result"]
        except (ValueError, KeyError, TypeError):
            result = "smtp_failed"
        if child.returncode != 0 or not isinstance(result, str):
            result = "smtp_failed"
        if result != "ok":
            raise DeliveryFailed(result if _CODE.fullmatch(result) else "smtp_failed")


_CODE = re.compile(r"smtp_[a-z_]{1,40}")
_CHILD_ENV = ("PATH", "SYSTEMROOT", "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR", "TEMP", "TMP")


def _child_env() -> dict[str, str]:
    """Only what the interpreter and TLS need; no database or service secrets."""
    env = {k: v for k, v in os.environ.items() if k in _CHILD_ENV}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


def _reap(child: subprocess.Popen) -> None:
    """Kill and wait for exactly the child this adapter started."""
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def build_link(base_url: str, path: str, token: str) -> str:
    return f"{base_url}{path}#token={token}"


def make_delivery(settings: Settings) -> DeliveryAdapter:
    if settings.delivery.mode == "dev_outbox":
        return DevOutbox(settings.delivery.dev_outbox_max_entries)
    if settings.delivery.mode == "smtp":
        return SmtpDelivery(settings.delivery.smtp)  # type: ignore[arg-type]
    return DisabledDelivery()


@lru_cache
def get_delivery() -> DeliveryAdapter:
    return make_delivery(get_settings())
