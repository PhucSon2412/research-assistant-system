"""Delivery of invitation/reset links.

The production channel (email, chat, ...) is an open decision. Until then:

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

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

from app.config import Settings, get_settings


class MessageKind(StrEnum):
    INVITATION = "invitation"
    PASSWORD_RESET = "password_reset"


@dataclass(frozen=True)
class OutgoingMessage:
    kind: MessageKind
    recipient: str
    link: str = field(repr=False)
    expires_at: datetime


class DeliveryUnavailable(Exception):
    pass


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


def build_link(base_url: str, path: str, token: str) -> str:
    return f"{base_url}{path}#token={token}"


def make_delivery(settings: Settings) -> DeliveryAdapter:
    if settings.delivery.mode == "dev_outbox":
        return DevOutbox(settings.delivery.dev_outbox_max_entries)
    return DisabledDelivery()


@lru_cache
def get_delivery() -> DeliveryAdapter:
    return make_delivery(get_settings())
