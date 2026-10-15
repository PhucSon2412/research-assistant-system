"""Passwords, raw tokens and session secrets never reach logs, audit rows or
the database (other than as Argon2/SHA-256 digests)."""

from __future__ import annotations

import logging

from tests.helpers import ADMIN_PASSWORD, Harness

INVITEE = "secretive@example.org"
FIRST_PW = "first-Secret-passw0rd"
SECOND_PW = "second-Secret-passw0rd"


def test_full_auth_flow_leaks_no_secret(api: Harness, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.DEBUG)
    try:
        api.bootstrap_admin()
        admin = api.admin_browser()
        admin.post("/admin/invitations", {"email": INVITEE})
        invite_token = api.last_link_token("invitation", INVITEE)
        guest = api.browser()
        guest.post("/auth/invitations/inspect", {"token": invite_token})
        guest.post("/auth/invitations/accept", {"token": invite_token, "password": FIRST_PW})
        user = api.user_browser(INVITEE, FIRST_PW)
        session_token, csrf_token = user.session_token(), user.client.cookies.get("ras_csrf")
        user.get("/library/items")
        guest.post("/auth/password-reset/request", {"email": INVITEE})
        reset_token = api.last_link_token("password_reset", INVITEE)
        guest.post("/auth/password-reset/confirm", {"token": reset_token, "new_password": SECOND_PW})
        guest.login(INVITEE, "wrong-" + SECOND_PW)
        api.user_browser(INVITEE, SECOND_PW).post("/auth/logout")
        admin.get("/admin/dev-outbox")
    finally:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    assert "SELECT ras_auth_session_create" in caplog.text  # SQL logging really was on through the runtime routine
    secrets = {
        "admin password": ADMIN_PASSWORD,
        "first password": FIRST_PW,
        "second password": SECOND_PW,
        "invite token": invite_token,
        "reset token": reset_token,
        "session token": session_token,
        "csrf token": csrf_token,
    }
    dump = api.dump_all_text()
    audit = api.scalar("SELECT string_agg(details::text || coalesce(target_id, ''), ' ') FROM audit_events")
    for name, value in secrets.items():
        assert value not in caplog.text, f"{name} in logs"
        assert value not in dump, f"{name} in database"
        assert value not in audit, f"{name} in audit"
    # Failed login records a reason code, not the attempted email.
    assert INVITEE not in audit
