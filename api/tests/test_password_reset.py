"""Password reset by single-use link: no account enumeration, 30-minute
expiry, concurrency, and every old session revoked."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.orm import Session

from app.config import PasswordPolicy
from app.identity.errors import InvalidToken
from app.identity.password_reset import confirm_reset
from app.identity.passwords import verify_password
from tests.helpers import USER_PASSWORD, Harness

EMAIL = "reset.me@example.org"
NEW_PASSWORD = "brand-new-Passw0rd"


@pytest.fixture
def h(api: Harness) -> Harness:
    api.bootstrap_admin()
    api.create_user(EMAIL)
    return api


def request_reset(h: Harness, email: str = EMAIL):
    return h.browser().post("/auth/password-reset/request", {"email": email})


def confirm(h: Harness, token: str, password: str = NEW_PASSWORD):
    return h.browser().post("/auth/password-reset/confirm", {"token": token, "new_password": password})


def test_request_response_is_identical_for_known_unknown_and_malformed(h: Harness) -> None:
    responses = [request_reset(h, e) for e in (EMAIL, "ghost@example.org", "not-an-email", EMAIL.upper())]
    assert {(r.status_code, r.text) for r in responses} == {(202, '{"status":"accepted"}')}
    # Only the real account got links (the original and the upper-cased spelling).
    kinds = [(e.message.kind.value, e.message.recipient) for e in h.outbox().entries(now=h.clock.now)]
    assert kinds == [("password_reset", EMAIL)] * 2
    # A second request supersedes the first: one open token.
    assert h.scalar("SELECT count(*) FROM password_reset_tokens WHERE used_at IS NULL AND revoked_at IS NULL") == 1


def test_disabled_account_gets_no_reset_link(h: Harness) -> None:
    uid = h.scalar("SELECT id FROM users WHERE email = :e", e=EMAIL)
    h.admin_browser().post(f"/admin/users/{uid}/disable")
    assert request_reset(h).status_code == 202
    assert [e for e in h.outbox().entries(now=h.clock.now) if e.message.kind.value == "password_reset"] == []


def test_reset_changes_password_and_revokes_every_session(h: Harness) -> None:
    laptop, phone = h.user_browser(EMAIL), h.user_browser(EMAIL)
    assert request_reset(h).status_code == 202
    token = h.last_link_token("password_reset", EMAIL)
    assert "/reset-password#token=" in h.outbox().entries(now=h.clock.now)[0].message.link

    r = confirm(h, token)
    assert (r.status_code, r.json()) == (200, {"status": "password_changed"})

    for b in (laptop, phone):
        assert b.get("/auth/session").status_code == 401
    assert h.scalar("SELECT count(*) FROM sessions s JOIN users u ON u.id=s.user_id"
                    " WHERE u.email=:e AND s.revoked_at IS NULL", e=EMAIL) == 0
    assert h.scalar("SELECT count(*) FROM sessions WHERE revoked_reason = 'password_reset'") == 2
    assert h.browser().login(EMAIL, USER_PASSWORD).status_code == 401
    assert h.browser().login(EMAIL, NEW_PASSWORD).status_code == 200


def test_reset_token_is_single_use(h: Harness) -> None:
    request_reset(h)
    token = h.last_link_token("password_reset", EMAIL)
    assert confirm(h, token).status_code == 200
    r = confirm(h, token, "yet-another-Passw0rd")
    assert (r.status_code, r.json()) == (400, {"error": "invalid_token"})
    verifier = h.scalar("SELECT password_verifier FROM users WHERE email = :e", e=EMAIL)
    assert verify_password(verifier, NEW_PASSWORD)


def test_reset_expiry_boundary_30_minutes(h: Harness) -> None:
    request_reset(h)
    token = h.last_link_token("password_reset", EMAIL)
    h.clock.advance(minutes=29, seconds=59)
    assert confirm(h, token, "short").json()["error"] == "weak_password"  # still valid at 29:59
    h.clock.advance(seconds=1)
    r = confirm(h, token)
    assert (r.status_code, r.json()) == (400, {"error": "invalid_token"})
    assert h.browser().login(EMAIL, USER_PASSWORD).status_code == 200


def test_new_request_invalidates_older_link(h: Harness) -> None:
    request_reset(h)
    old = h.last_link_token("password_reset", EMAIL)
    request_reset(h)
    new = h.last_link_token("password_reset", EMAIL)
    assert old != new
    assert confirm(h, old).status_code == 400
    assert confirm(h, new).status_code == 200


def test_concurrent_confirms_of_same_token_succeed_once(h: Harness) -> None:
    request_reset(h)
    token = h.last_link_token("password_reset", EMAIL)
    n = 6
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        with Session(h.engine, expire_on_commit=False) as db:
            barrier.wait()
            try:
                confirm_reset(db, raw_token=token, new_password=f"{NEW_PASSWORD}-{i}", now=h.clock.now, policy=PasswordPolicy())
                result = f"ok:{i}"
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

    winners = [o for o in outcomes if o.startswith("ok:")]
    assert len(winners) == 1 and outcomes.count("invalid") == n - 1, outcomes
    winner = int(winners[0].split(":")[1])
    verifier = h.scalar("SELECT password_verifier FROM users WHERE email = :e", e=EMAIL)
    assert verify_password(verifier, f"{NEW_PASSWORD}-{winner}")


def test_login_racing_a_reset_cannot_keep_an_old_password_session(h: Harness) -> None:
    """A login verified with the old password but committing after the reset
    must not leave a live session (row lock + verifier re-check)."""
    from app.config import get_settings
    from app.identity import login as login_mod
    from app.identity.errors import InvalidCredentials

    request_reset(h)
    token = h.last_link_token("password_reset", EMAIL)
    real_verify = login_mod.verify_password
    verified = threading.Event()
    reset_done = threading.Event()

    def slow_verify(verifier: str, password: str) -> bool:
        ok = real_verify(verifier, password)
        verified.set()
        reset_done.wait(timeout=30)  # the reset commits while login is "hashing"
        return ok

    login_mod.verify_password = slow_verify
    result: list[str] = []
    try:
        def do_login() -> None:
            with Session(h.engine, expire_on_commit=False) as db:
                try:
                    login_mod.login(db, email=EMAIL, password=USER_PASSWORD, now=h.clock.now, settings=get_settings())
                    result.append("session")
                except InvalidCredentials:
                    result.append("refused")

        t = threading.Thread(target=do_login)
        t.start()
        assert verified.wait(timeout=30)
        with Session(h.engine, expire_on_commit=False) as db:
            confirm_reset(db, raw_token=token, new_password=NEW_PASSWORD, now=h.clock.now, policy=PasswordPolicy())
        reset_done.set()
        t.join(timeout=30)
    finally:
        login_mod.verify_password = real_verify

    assert result == ["refused"]
    assert h.scalar("SELECT count(*) FROM sessions s JOIN users u ON u.id=s.user_id"
                    " WHERE u.email=:e AND s.revoked_at IS NULL", e=EMAIL) == 0


def test_raw_reset_token_is_not_stored(h: Harness) -> None:
    request_reset(h)
    token = h.last_link_token("password_reset", EMAIL)
    assert token not in h.dump_all_text()


def test_reset_ttl_comes_from_config(api: Harness, monkeypatch: pytest.MonkeyPatch, app_env, migrated_db_url) -> None:
    from datetime import timedelta

    monkeypatch.setenv("RAS_AUTH__PASSWORD_RESET", "PT5M")
    app_env(api.engine.url)
    api.create_user(EMAIL)
    request_reset(api)
    created, expires = api.scalar("SELECT ARRAY[created_at, expires_at] FROM password_reset_tokens")
    assert expires - created == timedelta(minutes=5)
