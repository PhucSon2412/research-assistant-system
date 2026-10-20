"""M8a invitation/reset through the real SmtpDelivery to an isolated TLS capture.

Reuses the existing token, final-caller, journal and compensation contracts;
only the adapter changes. Checks: link only after a known commit and live
proof, compensation on reject/timeout (a mail the server may still have
queued carries a link that no longer works), generic reset answers, and that
email/token never reach logs. No real mailbox or provider is contacted.
"""

from __future__ import annotations

import email
import email.policy
import logging
import re

import pytest

from app.api.errors import ApiError
from app.config import SmtpSettings
from app.delivery import SmtpDelivery, get_delivery
from tests.helpers import ADMIN_EMAIL, USER_PASSWORD, Harness
from tests.m8a_tls import make_ca, make_leaf
from tests.smtp_capture import Behaviour, CaptureServer

INVITEE = "pilot.member@example.test"


@pytest.fixture
def mail(api: Harness, tmp_path):
    from app.main import app
    ca = make_ca(tmp_path / "pki")
    leaf = make_leaf(tmp_path / "pki", ca, "smtp", ["127.0.0.1"])
    servers = []

    def use(behaviour: Behaviour = Behaviour(), total=3.0):
        server = CaptureServer(str(leaf.cert), str(leaf.key), behaviour=behaviour).start()
        servers.append(server)
        cfg = SmtpSettings(host="127.0.0.1", port=server.port, security="starttls", sender="no-reply@ras.test",
                           ca_file=ca.cert, connect_timeout_seconds=min(2, total), total_timeout_seconds=total)
        adapter = SmtpDelivery(cfg)
        app.dependency_overrides[get_delivery] = lambda: adapter
        return server

    yield use
    for server in servers:
        server.stop()


def token_of(server: CaptureServer, index: int = -1) -> tuple[str, str]:
    parsed = email.message_from_bytes(server.messages[index].data, policy=email.policy.default)
    match = re.search(r"(https?://\S+)#token=([A-Za-z0-9_\-]+)", parsed.get_content())
    assert match and "?" not in match.group(1)
    return parsed["To"], match.group(2)


def invitation_state(h: Harness) -> tuple:
    return tuple(h.scalar("SELECT ROW(closure_reason, revoked_at IS NOT NULL, accepted_at IS NOT NULL)::text "
                          "FROM invitations WHERE email=:e ORDER BY created_at DESC LIMIT 1", e=INVITEE)
                 .strip("()").split(","))


def test_invite_reaches_capture_and_link_activates_one_account(api: Harness, mail, caplog):
    server = mail()
    api.bootstrap_admin()
    admin = api.admin_browser()
    with caplog.at_level(logging.DEBUG):
        response = admin.post("/admin/invitations", {"email": INVITEE})
    assert response.status_code == 201 and response.json()["invitation"]["status"] == "pending"
    to, token = token_of(server)
    assert to == INVITEE and server.messages[0].tls
    assert token not in caplog.text and INVITEE not in caplog.text
    guest = api.browser()
    assert guest.post("/auth/invitations/inspect", {"token": token}).json()["email"] == INVITEE
    assert guest.post("/auth/invitations/accept", {"token": token, "password": USER_PASSWORD}).status_code == 201
    assert api.user_browser(INVITEE).get("/auth/session").json()["user"]["role"] == "user"
    assert admin.get("/admin/dev-outbox").status_code == 404  # no outbox behind SMTP


@pytest.mark.parametrize("behaviour,total", [(Behaviour(reject_rcpt=True), 3.0),
                                             (Behaviour(drop_after_data=True), 3.0),
                                             (Behaviour(offer_starttls=False), 3.0)])
def test_rejected_delivery_compensates_the_invitation(api: Harness, mail, behaviour, total, caplog):
    server = mail(behaviour, total)
    api.bootstrap_admin()
    admin = api.admin_browser()
    with caplog.at_level(logging.WARNING):
        response = admin.post("/admin/invitations", {"email": INVITEE})
    assert (response.status_code, response.json()) == (503, {"error": "delivery_failed"})
    assert invitation_state(api) == ("delivery_failed", "t", "f")
    assert server.messages == []
    assert "invitation delivery failed (smtp_" in caplog.text and INVITEE not in caplog.text
    mail()  # a fresh, working channel: Admin may invite again
    assert admin.post("/admin/invitations", {"email": INVITEE}).status_code == 201


def test_timeout_after_data_may_leave_a_mail_whose_link_no_longer_works(api: Harness, mail):
    server = mail(Behaviour(data_delay_seconds=2.5), total=1.5)
    api.bootstrap_admin()
    admin = api.admin_browser()
    response = admin.post("/admin/invitations", {"email": INVITEE})
    assert (response.status_code, response.json()) == (503, {"error": "delivery_failed"})
    assert invitation_state(api) == ("delivery_failed", "t", "f")
    deadline = __import__("time").monotonic() + 5
    while not server.messages and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.1)
    _, token = token_of(server)  # the server queued it after the client gave up
    guest = api.browser()
    assert guest.post("/auth/invitations/accept", {"token": token, "password": USER_PASSWORD}).status_code == 400
    assert api.scalar("SELECT count(*) FROM users WHERE email=:e", e=INVITEE) == 0


def test_no_mail_when_the_invitation_commit_is_not_known(api: Harness, mail, monkeypatch):
    from app.identity import invitations
    server = mail()
    api.bootstrap_admin()
    admin = api.admin_browser()

    def uncertain(db, **kw):
        db.rollback()
        raise ApiError(503, "auth_outcome_uncertain")

    monkeypatch.setattr(invitations, "commit_auth", uncertain)
    response = admin.post("/admin/invitations", {"email": INVITEE})
    assert (response.status_code, response.json()) == (503, {"error": "auth_outcome_uncertain"})
    assert server.messages == [] and api.scalar("SELECT count(*) FROM invitations") == 0


def test_no_mail_when_the_proof_is_lost_before_sending(api: Harness, mail, monkeypatch):
    from app.identity import invitations
    server = mail()
    api.bootstrap_admin()
    admin = api.admin_browser()
    real = invitations._delivery_check
    calls = []

    def lost(db, created, actor, clock, *, response=False):
        calls.append(response)
        if len(calls) == 2:  # the check right before the adapter
            raise ApiError(409, "invitation_unavailable")
        return real(db, created, actor, clock, response=response)

    monkeypatch.setattr(invitations, "_delivery_check", lost)
    response = admin.post("/admin/invitations", {"email": INVITEE})
    assert response.status_code == 409 and server.messages == []
    assert invitation_state(api) == ("delivery_failed", "t", "f")


def test_reset_through_smtp_ends_old_credential_and_sessions(api: Harness, mail):
    server = mail()
    api.bootstrap_admin()
    api.create_user(INVITEE)
    old = api.user_browser(INVITEE)
    guest = api.browser()
    response = guest.post("/auth/password-reset/request", {"email": INVITEE})
    assert (response.status_code, response.json()) == (202, {"status": "accepted"})
    to, token = token_of(server)
    assert to == INVITEE
    assert guest.post("/auth/password-reset/confirm",
                      {"token": token, "new_password": "Fresh-Passw0rd-long"}).status_code == 200
    assert api.browser().login(INVITEE, USER_PASSWORD).status_code == 401
    assert old.get("/auth/session").status_code == 401
    assert guest.post("/auth/password-reset/confirm",
                      {"token": token, "new_password": "Other-Passw0rd-long"}).status_code == 400
    assert api.browser().login(INVITEE, "Fresh-Passw0rd-long").status_code == 200


@pytest.mark.parametrize("behaviour", [Behaviour(reject_rcpt=True), Behaviour(offer_starttls=False)])
def test_reset_answers_the_same_whether_mail_fails_or_no_account(api: Harness, mail, behaviour, caplog):
    server = mail(behaviour)
    api.bootstrap_admin()
    api.create_user(INVITEE)
    guest = api.browser()
    with caplog.at_level(logging.DEBUG):
        answers = [guest.post("/auth/password-reset/request", {"email": e})
                   for e in (INVITEE, "nobody@example.test", "not-an-email")]
    assert {(r.status_code, r.text) for r in answers} == {(202, '{"status":"accepted"}')}
    assert server.messages == []
    assert "password reset delivery failed (smtp_" in caplog.text
    assert INVITEE not in caplog.text and "#token=" not in caplog.text


def test_reset_token_no_longer_live_is_never_sent(api: Harness, mail, monkeypatch):
    from app.identity import password_reset
    server = mail()
    api.bootstrap_admin()
    api.create_user(INVITEE)
    real = password_reset.token_live
    seen = []

    def expired_at_send(db, tid, uid, at):
        seen.append(tid)
        return real(db, tid, uid, at) if len(seen) < 3 else False

    monkeypatch.setattr(password_reset, "token_live", expired_at_send)
    assert api.browser().post("/auth/password-reset/request", {"email": INVITEE}).status_code == 202
    assert len(seen) >= 3 and server.messages == []
    assert ADMIN_EMAIL
