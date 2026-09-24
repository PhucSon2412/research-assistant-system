"""Login, logout, server-side session expiry (idle/absolute), revocation and
disabled users."""

from __future__ import annotations

import pytest

from tests.helpers import ADMIN_EMAIL, ADMIN_PASSWORD, USER_PASSWORD, Harness


@pytest.fixture
def h(api: Harness) -> Harness:
    api.bootstrap_admin()
    return api


def test_bootstrapped_admin_can_log_in(h: Harness) -> None:
    b = h.browser()
    r = b.login(" Admin@Example.org ", ADMIN_PASSWORD)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "admin"
    me = b.get("/auth/session").json()
    assert me["user"]["email"] == ADMIN_EMAIL
    assert h.scalar("SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == 1


def test_session_cookie_flags_in_local_http_mode(h: Harness) -> None:
    b = h.browser()
    r = b.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("ras_session="))
    csrf = next(c for c in cookies if c.startswith("ras_csrf="))
    assert "HttpOnly" in session and "SameSite=lax" in session and "Path=/" in session
    assert "Secure" not in session  # RAS_HTTP__COOKIE_SECURE=false for local HTTP
    assert "HttpOnly" not in csrf  # the page must read it to echo the header
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (ADMIN_EMAIL, "wrong-password-123"),
        ("nobody@example.org", ADMIN_PASSWORD),
        ("not-an-email", ADMIN_PASSWORD),
    ],
    ids=["wrong-password", "unknown-account", "malformed-email"],
)
def test_failed_login_does_not_reveal_which_part_was_wrong(h: Harness, email: str, password: str) -> None:
    r = h.browser().login(email, password)
    assert (r.status_code, r.json()) == (401, {"error": "invalid_credentials"})
    assert h.scalar("SELECT count(*) FROM sessions") == 0


def test_disabled_account_login_looks_like_bad_credentials(h: Harness) -> None:
    uid = h.create_user("u@example.org")
    admin = h.admin_browser()
    assert admin.post(f"/admin/users/{uid}/disable").status_code == 200
    r = h.browser().login("u@example.org", USER_PASSWORD)
    assert (r.status_code, r.json()) == (401, {"error": "invalid_credentials"})


def test_disabling_a_user_ends_their_live_sessions_immediately(h: Harness) -> None:
    uid = h.create_user("u@example.org")
    user = h.user_browser("u@example.org")
    old_token = user.session_token()
    assert user.get("/library/items").status_code == 200
    admin = h.admin_browser()
    assert admin.post(f"/admin/users/{uid}/disable").status_code == 200

    r = user.get("/library/items")
    assert (r.status_code, r.json()) == (401, {"error": "session_expired"})
    assert h.scalar("SELECT revoked_reason FROM sessions WHERE user_id = :u", u=uid) == "user_disabled"
    # Re-enabling does not resurrect the old session; a new login is needed.
    assert admin.post(f"/admin/users/{uid}/enable").status_code == 200
    replay = h.browser()
    replay.client.cookies.set("ras_session", old_token)
    assert replay.get("/auth/session").status_code == 401
    assert h.user_browser("u@example.org").get("/auth/session").status_code == 200


def test_admin_cannot_disable_self(h: Harness) -> None:
    admin = h.admin_browser()
    admin_id = admin.get("/auth/session").json()["user"]["id"]
    r = admin.post(f"/admin/users/{admin_id}/disable")
    assert (r.status_code, r.json()) == (400, {"error": "cannot_change_own_status"})


def test_logout_revokes_server_side_even_if_cookie_is_replayed(h: Harness) -> None:
    b = h.admin_browser()
    token = b.session_token()
    assert b.post("/auth/logout").status_code == 204
    assert b.get("/auth/session").status_code == 401

    replay = h.browser()
    replay.client.cookies.set("ras_session", token)
    r = replay.get("/auth/session")
    assert (r.status_code, r.json()) == (401, {"error": "session_expired"})
    assert h.scalar("SELECT revoked_reason FROM sessions") == "logout"


def test_unauthenticated_requests_are_rejected(h: Harness) -> None:
    b = h.browser()
    for path in ("/auth/session", "/auth/sessions", "/library/items", "/library/items/count"):
        r = b.get(path)
        assert (r.status_code, r.json()) == (401, {"error": "not_authenticated"}), path
    b.client.cookies.set("ras_session", "forged-token")
    assert b.get("/auth/session").status_code == 401


# --- expiry boundaries -------------------------------------------------------


def test_idle_timeout_boundary_30_minutes(h: Harness) -> None:
    b = h.admin_browser()
    h.clock.advance(minutes=29, seconds=59)
    assert b.get("/auth/session").status_code == 200  # activity at t=29:59 extends the session
    h.clock.advance(minutes=29, seconds=59)
    assert b.get("/auth/session").status_code == 200
    h.clock.advance(minutes=30)  # exactly 30 min after the last request
    r = b.get("/auth/session")
    assert (r.status_code, r.json()) == (401, {"error": "session_expired"})
    # Expired stays expired: later activity cannot revive it.
    assert b.get("/auth/session").status_code == 401


def test_idle_expiry_blocks_writes_too(h: Harness) -> None:
    b = h.admin_browser()
    h.clock.advance(minutes=31)
    r = b.post("/auth/sessions/revoke-others")
    assert r.status_code == 401


def test_absolute_timeout_boundary_8_hours_despite_activity(h: Harness) -> None:
    b = h.admin_browser()
    # Stay active every 20 minutes until just before 8 hours.
    for _ in range(23):
        h.clock.advance(minutes=20)
        assert b.get("/auth/session").status_code == 200
    h.clock.advance(minutes=19, seconds=59)  # t = 7:59:59
    body = b.get("/auth/session").json()
    assert body["session"]["idle_expires_at"] == body["session"]["absolute_expires_at"]
    h.clock.advance(seconds=1)  # t = 8:00:00
    r = b.get("/auth/session")
    assert (r.status_code, r.json()) == (401, {"error": "session_expired"})


def test_expired_session_cookie_is_cleared(h: Harness) -> None:
    b = h.admin_browser()
    h.clock.advance(hours=9)
    r = b.get("/auth/session")
    assert r.status_code == 401
    assert any(c.startswith("ras_session=") and "Max-Age=0" in c for c in r.headers.get_list("set-cookie"))


# --- revoking sessions -------------------------------------------------------


def test_user_can_list_and_revoke_own_sessions(h: Harness) -> None:
    laptop, phone = h.admin_browser(), h.admin_browser()
    sessions = laptop.get("/auth/sessions").json()["sessions"]
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    phone_id = next(s["id"] for s in sessions if not s["current"])

    assert laptop.post(f"/auth/sessions/{phone_id}/revoke").status_code == 204
    assert phone.get("/auth/session").status_code == 401
    assert laptop.get("/auth/session").status_code == 200
    # Already revoked: 404, not a second success.
    assert laptop.post(f"/auth/sessions/{phone_id}/revoke").status_code == 404


def test_revoke_others_keeps_current(h: Harness) -> None:
    keep, a, b = h.admin_browser(), h.admin_browser(), h.admin_browser()
    assert keep.post("/auth/sessions/revoke-others").json() == {"revoked": 2}
    assert keep.get("/auth/session").status_code == 200
    assert a.get("/auth/session").status_code == 401 and b.get("/auth/session").status_code == 401


def test_cannot_revoke_another_users_session(h: Harness) -> None:
    h.create_user("a@example.org")
    h.create_user("b@example.org")
    a, b = h.user_browser("a@example.org"), h.user_browser("b@example.org")
    b_session = b.get("/auth/session").json()["session"]["id"]
    r = a.post(f"/auth/sessions/{b_session}/revoke")
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})
    assert b.get("/auth/session").status_code == 200


def test_new_login_in_same_browser_replaces_old_session(h: Harness) -> None:
    b = h.admin_browser()
    old = b.session_token()
    assert b.login(ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
    assert b.session_token() != old
    assert h.scalar("SELECT count(*) FROM sessions WHERE revoked_reason = 'replaced'") == 1


def test_session_tokens_are_stored_only_as_hashes(h: Harness) -> None:
    b = h.admin_browser()
    token, csrf = b.session_token(), b.client.cookies.get("ras_csrf")
    dump = h.dump_all_text()
    assert token not in dump and csrf not in dump
