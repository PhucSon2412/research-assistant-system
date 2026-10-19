"""Delivery of invitation/reset links.

- ``SmtpDelivery`` (M8a): one plain-text email per link over SMTP with
  mandatory, verified TLS (SMTPS or required STARTTLS). A server accepting a
  message is not proof the recipient received it. Failures raise
  ``DeliveryFailed`` carrying only a stable code; nothing is retried here.
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

import email.policy
import itertools
import re
import smtplib
import socket
import ssl
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

    The caller holds no database session, row or journal lock while ``send``
    runs (the invitation and reset writers release them first). The whole send
    is bounded by ``total_timeout_seconds``: every socket wait gets at most the
    time left."""

    enabled = True

    def __init__(self, cfg: SmtpSettings) -> None:
        self._cfg = cfg

    def _context(self) -> ssl.SSLContext:
        try:
            ctx = ssl.create_default_context(cafile=str(self._cfg.ca_file) if self._cfg.ca_file else None)
        except (OSError, ssl.SSLError):
            raise DeliveryFailed("smtp_ca_unavailable") from None
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

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

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise DeliveryFailed("smtp_timeout")
            return left

        msg = compose_message(message, cfg, now=now)
        context = self._context()
        password = self._password() if cfg.username else None
        client: smtplib.SMTP | None = None
        try:
            connect = min(cfg.connect_timeout_seconds, remaining())
            if cfg.security == "implicit_tls":
                client = smtplib.SMTP_SSL(cfg.host, cfg.port, local_hostname=cfg.helo_name,
                                          timeout=connect, context=context)
            else:
                client = smtplib.SMTP(cfg.host, cfg.port, local_hostname=cfg.helo_name, timeout=connect)

            def step() -> None:
                client.sock.settimeout(remaining())

            step()
            client.ehlo()
            if cfg.security == "starttls":
                if not client.has_extn("starttls"):
                    raise DeliveryFailed("smtp_starttls_unavailable")
                step()
                code, _ = client.starttls(context=context)
                if code != 220:
                    raise DeliveryFailed("smtp_starttls_unavailable")
                step()
                client.ehlo()
            if cfg.username:
                step()
                client.login(cfg.username, password)
            step()
            refused = client.send_message(msg, from_addr=cfg.sender, to_addrs=[message.recipient])
            if refused:
                raise DeliveryFailed("smtp_rejected")
        except DeliveryFailed:
            raise
        except ssl.SSLCertVerificationError:
            raise DeliveryFailed("smtp_tls_verify_failed") from None
        except ssl.SSLError:
            raise DeliveryFailed("smtp_tls_failed") from None
        except (socket.timeout, TimeoutError):
            raise DeliveryFailed("smtp_timeout") from None
        except smtplib.SMTPAuthenticationError:
            raise DeliveryFailed("smtp_auth_failed") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                smtplib.SMTPNotSupportedError, smtplib.SMTPHeloError):
            raise DeliveryFailed("smtp_rejected") from None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            # smtplib turns a socket timeout while reading a reply into a disconnect.
            timed_out = isinstance(exc.__context__, (socket.timeout, TimeoutError))
            raise DeliveryFailed("smtp_timeout" if timed_out else "smtp_connection_failed") from None
        except smtplib.SMTPException:  # before OSError: SMTPException derives from it
            raise DeliveryFailed("smtp_failed") from None
        except OSError:
            raise DeliveryFailed("smtp_connection_failed") from None
        else:
            # DATA was accepted; a failed QUIT does not undo that.
            try:
                client.sock.settimeout(max(0.1, min(1.0, deadline - time.monotonic())))
                client.quit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
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
