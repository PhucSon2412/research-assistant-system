"""CSRF protection on state-changing requests and cookie settings for HTTPS."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import ADMIN_EMAIL, ADMIN_PASSWORD, WEB_ORIGIN, Browser, Harness


@pytest.fixture
def h(api: Harness) -> Harness:
    api.bootstrap_admin()
    return api


def test_login_without_csrf_header_is_rejected(h: Harness) -> None:
    b = h.browser()
    b.csrf()  # cookie present, header missing
    r = b.post("/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, csrf=False)
    assert (r.status_code, r.json()) == (403, {"error": "csrf_failed"})
    assert h.scalar("SELECT count(*) FROM sessions") == 0


def test_header_must_match_cookie(h: Harness) -> None:
    b = h.browser()
    b.csrf()
    r = b.post(
        "/auth/login",
        {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        csrf=False,
        headers={"X-CSRF-Token": "attacker-guess"},
    )
    assert r.status_code == 403


def test_authenticated_write_requires_session_bound_token(h: Harness) -> None:
    """Cookie tossing: an attacker who plants their own ras_csrf cookie and
    matching header still fails, because the token is bound to the session."""
    victim = h.admin_browser()
    planted = "planted-by-attacker"
    victim.client.cookies.set("ras_csrf", planted)
    r = victim.post("/auth/sessions/revoke-others", csrf=False, headers={"X-CSRF-Token": planted})
    assert (r.status_code, r.json()) == (403, {"error": "csrf_failed"})


def test_foreign_origin_is_rejected_even_with_valid_token(h: Harness) -> None:
    b = h.admin_browser()
    r = b.post("/auth/sessions/revoke-others", headers={"Origin": "https://evil.example"})
    assert (r.status_code, r.json()) == (403, {"error": "csrf_failed"})
    assert b.post("/auth/sessions/revoke-others", headers={"Origin": WEB_ORIGIN}).status_code == 200


def test_writes_on_every_router_need_csrf(h: Harness) -> None:
    admin = h.admin_browser()
    item = h.add_item(owner=None, title="Common")
    for path, method in (
        ("/auth/logout", "POST"),
        ("/admin/invitations", "POST"),
        (f"/library/items/{item}", "PATCH"),
        ("/auth/password-reset/request", "POST"),
    ):
        r = admin.client.request(method, path, json={"email": "x@example.org", "title": "t"})
        assert r.status_code == 403, path
    assert h.scalar("SELECT title FROM library_items WHERE id = :i", i=item) == "Common"
    assert admin.get("/auth/session").status_code == 200  # logout did not happen


def test_lost_csrf_cookie_is_rotated_for_live_session(h: Harness) -> None:
    b = h.admin_browser()
    old = b.client.cookies.get("ras_csrf")
    b.client.cookies.delete("ras_csrf")
    assert b.get("/auth/csrf").status_code == 200
    new = b.client.cookies.get("ras_csrf")
    assert new and new != old
    assert b.post("/auth/sessions/revoke-others").status_code == 200


def test_https_mode_uses_secure_host_prefixed_cookie(monkeypatch: pytest.MonkeyPatch, api: Harness, app_env, migrated_db_url) -> None:
    monkeypatch.setenv("RAS_HTTP__COOKIE_SECURE", "true")
    app_env(migrated_db_url)
    api.bootstrap_admin()
    b = Browser(base_url="https://testserver")
    r = b.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("__Host-ras_session="))
    assert "Secure" in session and "HttpOnly" in session and "Path=/" in session and "Domain" not in session
    assert "Secure" in next(c for c in cookies if c.startswith("ras_csrf="))
    assert b.get("/auth/session").status_code == 200
    # The plain-HTTP cookie name is not accepted in HTTPS mode.
    plain = TestClient(app, base_url="https://testserver")
    plain.cookies.set("ras_session", b.client.cookies.get("__Host-ras_session"))
    assert plain.get("/auth/session").status_code == 401


def test_validation_errors_do_not_echo_submitted_values(h: Harness) -> None:
    secret = "my-secret-password-value"
    # Both fields invalid (password over the 4096-char cap): FastAPI's
    # default body would echo each submitted value back.
    r = h.browser().post("/auth/login", {"email": ["not", "a", "string"], "password": secret * 200})
    assert r.status_code == 422
    assert secret not in r.text
