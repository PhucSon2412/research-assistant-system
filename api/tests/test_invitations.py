"""Invitation flow: Admin invites, invitee sets own password; single use,
72-hour expiry, concurrency, Admin-only routes, no role elevation."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import PasswordPolicy
from app.identity.errors import InvalidToken
from app.identity.invitations import accept_invitation
from app.identity.tokens import hash_token
from tests.helpers import USER_PASSWORD, Harness

INVITEE = "new.user@example.org"


@pytest.fixture
def h(api: Harness) -> Harness:
    api.bootstrap_admin()
    return api


def invite(h: Harness, email: str = INVITEE) -> str:
    r = h.admin_browser().post("/admin/invitations", {"email": email})
    assert r.status_code == 201, r.text
    return h.last_link_token("invitation", email)


def accept(h: Harness, token: str, password: str = USER_PASSWORD, **extra):
    return h.browser().post("/auth/invitations/accept", {"token": token, "password": password, **extra})


def test_invite_accept_then_login_as_user(h: Harness) -> None:
    admin = h.admin_browser()
    r = admin.post("/admin/invitations", {"email": " New.User@Example.org ", "display_name": "New"})
    assert r.status_code == 201
    inv = r.json()["invitation"]
    assert (inv["email"], inv["status"], inv["role"]) == (INVITEE, "pending", "user")
    # Response carries no link/token: delivery goes through the adapter only.
    assert "token" not in r.text and "#token=" not in r.text

    token = h.last_link_token("invitation", INVITEE)
    peek = h.browser().post("/auth/invitations/inspect", {"token": token})
    assert peek.status_code == 200 and peek.json()["email"] == INVITEE

    r = accept(h, token)
    assert r.status_code == 201, r.text
    assert r.json()["user"]["role"] == "user"

    b = h.browser()
    assert b.login(INVITEE, USER_PASSWORD).status_code == 200
    assert b.get("/auth/session").json()["user"]["role"] == "user"
    listed = admin.get("/admin/invitations").json()["invitations"]
    assert listed[0]["status"] == "accepted"


def test_invitation_cannot_grant_admin(h: Harness) -> None:
    token = invite(h)
    r = accept(h, token, role="admin", is_admin=True)
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "user"
    assert h.scalar("SELECT role FROM users WHERE email = :e", e=INVITEE) == "user"
    # And the Admin endpoint has no role parameter either.
    admin = h.admin_browser()
    admin.post("/admin/invitations", {"email": "x@example.org", "role": "admin"})
    assert admin.get("/admin/invitations").json()["invitations"][0]["role"] == "user"


def test_token_is_single_use(h: Harness) -> None:
    token = invite(h)
    assert accept(h, token).status_code == 201
    r = accept(h, token, password="another-Passw0rd-long")
    assert (r.status_code, r.json()) == (400, {"error": "invalid_token"})
    assert h.browser().post("/auth/invitations/inspect", {"token": token}).status_code == 400
    assert h.scalar("SELECT count(*) FROM users WHERE email = :e", e=INVITEE) == 1


def test_expiry_boundary_72_hours(h: Harness) -> None:
    token = invite(h)
    h.clock.advance(hours=71, minutes=59, seconds=59)
    assert h.browser().post("/auth/invitations/inspect", {"token": token}).status_code == 200
    h.clock.advance(seconds=1)  # exactly 72 h
    r = accept(h, token)
    assert (r.status_code, r.json()) == (400, {"error": "invalid_token"})
    assert h.scalar("SELECT count(*) FROM users WHERE email = :e", e=INVITEE) == 0
    assert h.admin_browser().get("/admin/invitations").json()["invitations"][0]["status"] == "expired"


def test_weak_password_is_rejected_and_invitation_stays_usable(h: Harness) -> None:
    token = invite(h)
    r = accept(h, token, password="short")
    assert r.status_code == 422 and r.json()["error"] == "weak_password"
    assert "short" not in r.text
    assert accept(h, token).status_code == 201


def test_reinvite_supersedes_previous_link(h: Harness) -> None:
    first = invite(h)
    second = invite(h)
    assert first != second
    assert accept(h, first).status_code == 400
    assert accept(h, second).status_code == 201


def test_revoked_invitation_cannot_be_used(h: Harness) -> None:
    token = invite(h)
    admin = h.admin_browser()
    inv_id = admin.get("/admin/invitations").json()["invitations"][0]["id"]
    assert admin.post(f"/admin/invitations/{inv_id}/revoke").status_code == 204
    assert accept(h, token).status_code == 400
    assert admin.post(f"/admin/invitations/{inv_id}/revoke").status_code == 404


def test_inviting_an_existing_account_is_refused(h: Harness) -> None:
    h.create_user("taken@example.org")
    r = h.admin_browser().post("/admin/invitations", {"email": "Taken@example.org"})
    assert (r.status_code, r.json()) == (409, {"error": "account_exists"})


def test_concurrent_accepts_of_same_token_create_one_account(h: Harness) -> None:
    token = invite(h)
    n = 6
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        with Session(h.engine, expire_on_commit=False) as db:
            barrier.wait()
            try:
                accept_invitation(
                    db,
                    raw_token=token,
                    password=f"{USER_PASSWORD}-{i}",
                    display_name=None,
                    now=h.clock.now,
                    policy=PasswordPolicy(),
                    clock=lambda: h.clock.now,
                )
                result = "ok"
            except InvalidToken:
                result = "invalid"
            except Exception as exc:  # noqa: BLE001
                result = f"error:{type(exc).__name__}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(outcomes) == ["invalid"] * (n - 1) + ["ok"]
    assert h.scalar("SELECT count(*) FROM users WHERE email = :e", e=INVITEE) == 1
    assert h.scalar("SELECT count(*) FROM invitations WHERE accepted_at IS NOT NULL") == 1


def test_raw_invitation_token_is_not_stored(h: Harness) -> None:
    token = invite(h)
    dump = h.dump_all_text()
    assert token not in dump
    assert hash_token(token) in dump  # only the digest


# --- access control on Admin routes ------------------------------------------


ADMIN_CALLS = [
    ("GET", "/admin/invitations", None),
    ("POST", "/admin/invitations", {"email": "z@example.org"}),
    ("POST", "/admin/invitations/00000000-0000-0000-0000-000000000000/revoke", None),
    ("GET", "/admin/users", None),
    ("POST", "/admin/users/00000000-0000-0000-0000-000000000000/disable", None),
    ("POST", "/admin/users/00000000-0000-0000-0000-000000000000/enable", None),
    ("GET", "/admin/dev-outbox", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_CALLS)
def test_admin_routes_are_closed_to_users_and_guests(h: Harness, method: str, path: str, body) -> None:
    h.create_user("plain@example.org")
    user = h.user_browser("plain@example.org")
    guest = h.browser()
    for who, expected in ((user, (403, {"error": "forbidden"})), (guest, (401, {"error": "not_authenticated"}))):
        r = who.get(path) if method == "GET" else who.post(path, body)
        assert (r.status_code, r.json()) == expected, (path, r.text)
    assert h.scalar("SELECT count(*) FROM invitations") == 0


def test_dev_outbox_readable_by_admin_and_drops_expired(h: Harness) -> None:
    invite(h)
    admin = h.admin_browser()
    messages = admin.get("/admin/dev-outbox").json()["messages"]
    assert [m["kind"] for m in messages] == ["invitation"]
    assert messages[0]["link"].startswith("http://web.test/invite#token=")
    h.clock.advance(hours=72)
    # A fresh Admin session (the previous one idled out).
    assert h.admin_browser().get("/admin/dev-outbox").json()["messages"] == []


def test_dev_outbox_absent_and_invites_refused_without_delivery(
    api: Harness, monkeypatch: pytest.MonkeyPatch, app_env, migrated_db_url
) -> None:
    from app.delivery import get_delivery

    monkeypatch.setenv("RAS_DELIVERY__MODE", "disabled")
    app_env(migrated_db_url)
    get_delivery.cache_clear()
    api.bootstrap_admin()
    admin = api.admin_browser()
    assert admin.get("/admin/dev-outbox").status_code == 404
    r = admin.post("/admin/invitations", {"email": INVITEE})
    assert (r.status_code, r.json()) == (503, {"error": "delivery_unavailable"})
    assert api.scalar("SELECT count(*) FROM invitations") == 0


def test_invitation_ttl_comes_from_config(api: Harness, monkeypatch: pytest.MonkeyPatch, app_env, migrated_db_url) -> None:
    monkeypatch.setenv("RAS_AUTH__INVITE", "PT1H")
    app_env(migrated_db_url)
    api.bootstrap_admin()
    api.admin_browser().post("/admin/invitations", {"email": INVITEE})
    created, expires = api.scalar("SELECT ARRAY[created_at, expires_at] FROM invitations")
    assert expires - created == timedelta(hours=1)
