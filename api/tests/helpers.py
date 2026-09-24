"""Helpers for API tests: a controllable clock and one cookie jar per person."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.config import PasswordPolicy
from app.delivery import DevOutbox, get_delivery
from app.identity.bootstrap import bootstrap_admin
from app.identity.passwords import hash_password
from app.main import app

ADMIN_EMAIL = "admin@example.org"
ADMIN_PASSWORD = "admin-Passw0rd-long"
USER_PASSWORD = "user-Passw0rd-long"
WEB_ORIGIN = "http://web.test"

# Development settings for API tests (HTTP cookies, dev outbox).
DEV_ENV = {
    "RAS_ENVIRONMENT": "development",
    "RAS_HTTP__COOKIE_SECURE": "false",
    "RAS_HTTP__PUBLIC_BASE_URL": WEB_ORIGIN,
    "RAS_HTTP__ALLOWED_ORIGINS": f'["{WEB_ORIGIN}"]',
    "RAS_DELIVERY__MODE": "dev_outbox",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class Browser:
    """One person's browser: its own cookie jar; sends the CSRF header the
    way the web client does (read the ``ras_csrf`` cookie, echo it)."""

    def __init__(self, base_url: str = "http://testserver") -> None:
        self.client = TestClient(app, base_url=base_url)

    def csrf(self) -> str:
        token = self.client.cookies.get("ras_csrf")
        if token is None:
            self.client.get("/auth/csrf")
            token = self.client.cookies.get("ras_csrf")
        assert token
        return token

    def get(self, path: str, **kwargs):
        return self.client.get(path, **kwargs)

    def _unsafe(self, method: str, path: str, json=None, headers: dict | None = None, csrf: bool = True):
        h = {"X-CSRF-Token": self.csrf()} if csrf else {}
        h.update(headers or {})
        return self.client.request(method, path, json=json, headers=h)

    def post(self, path: str, json=None, **kwargs):
        return self._unsafe("POST", path, json, **kwargs)

    def patch(self, path: str, json=None, **kwargs):
        return self._unsafe("PATCH", path, json, **kwargs)

    def login(self, email: str, password: str):
        return self.post("/auth/login", {"email": email, "password": password})

    def session_token(self) -> str | None:
        return self.client.cookies.get("ras_session")


@dataclass
class Harness:
    engine: Engine
    clock: FakeClock

    def browser(self) -> Browser:
        return Browser()

    def outbox(self) -> DevOutbox:
        delivery = get_delivery()
        assert isinstance(delivery, DevOutbox)
        return delivery

    def last_link_token(self, kind: str, recipient: str) -> str:
        for entry in self.outbox().entries(now=self.clock.now):
            if entry.message.kind.value == kind and entry.message.recipient == recipient:
                match = re.search(r"#token=([A-Za-z0-9_\-]+)$", entry.message.link)
                assert match, "link must carry the token in the URL fragment"
                return match.group(1)
        raise AssertionError(f"no {kind} message for {recipient}")

    def bootstrap_admin(self) -> uuid.UUID:
        return bootstrap_admin(self.engine, email=ADMIN_EMAIL, password=ADMIN_PASSWORD, policy=PasswordPolicy()).user_id

    def admin_browser(self) -> Browser:
        b = self.browser()
        assert b.login(ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
        return b

    def create_user(self, email: str, password: str = USER_PASSWORD) -> uuid.UUID:
        """Direct insert (fixture), bypassing the invitation flow."""
        with self.engine.begin() as conn:
            return conn.scalar(
                text("INSERT INTO users (email, password_verifier) VALUES (:e, :v) RETURNING id"),
                {"e": email, "v": hash_password(password)},
            )

    def user_browser(self, email: str, password: str = USER_PASSWORD) -> Browser:
        b = self.browser()
        response = b.login(email, password)
        assert response.status_code == 200, response.text
        return b

    def add_item(self, *, owner: uuid.UUID | None, title: str, created_offset_s: int = 0) -> uuid.UUID:
        """Fixture item (M1b adds no import path)."""
        with self.engine.begin() as conn:
            item = conn.scalar(
                text(
                    "INSERT INTO library_items (visibility, owner_user_id, title, created_at)"
                    " VALUES (:v, :o, :t, now() + make_interval(secs => :s)) RETURNING id"
                ),
                {"v": "private" if owner else "common", "o": owner, "t": title, "s": created_offset_s},
            )
            conn.execute(
                text("INSERT INTO paper_versions (item_id, version_label) VALUES (:i, 'v1')"), {"i": item}
            )
            return item

    def scalar(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.scalar(text(sql), params)

    def dump_all_text(self) -> str:
        with self.engine.connect() as conn:
            tables = conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
            return "\n".join(str(row) for t in tables for row in conn.execute(text(f'SELECT * FROM "{t}"')).all())
