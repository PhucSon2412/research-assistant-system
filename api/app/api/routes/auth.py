"""Public auth endpoints: login/logout, own sessions, invitation acceptance,
password reset. All state-changing calls pass the CSRF middleware."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.cookies import (
    CSRF_COOKIE,
    clear_session_cookie,
    session_cookie_name,
    set_csrf_cookie,
    set_session_cookie,
)
from app.api.deps import get_clock, get_db, get_now, get_session_context
from app.api.errors import ApiError
from app.audit import record_event
from app.config import Settings, get_settings
from app.delivery import DeliveryAdapter, MessageKind, OutgoingMessage, build_link, get_delivery
from app.identity import invitations, password_reset
from app.identity.errors import InvalidCredentials, InvalidToken
from app.identity.login import login
from app.identity.session_commit import commit_auth
from app.identity.passwords import PasswordPolicyError
from app.identity.sessions import (
    SessionContext,
    list_live_sessions,
    resolve_session,
    revoke_session,
    revoke_user_sessions,
    rotate_csrf,
)
from app.identity.tokens import digest_matches, generate_token
from app.models import User
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# Generous bounds: they only cap request size, policy lives in Settings.
EmailField = Field(min_length=1, max_length=320)
PasswordField = Field(min_length=1, max_length=4096)
TokenField = Field(min_length=1, max_length=256)


class LoginIn(BaseModel):
    email: str = EmailField
    password: str = PasswordField


class TokenIn(BaseModel):
    token: str = TokenField


class AcceptInvitationIn(BaseModel):
    token: str = TokenField
    password: str = PasswordField
    display_name: str | None = Field(default=None, max_length=200)


class ResetRequestIn(BaseModel):
    email: str = EmailField


class ResetConfirmIn(BaseModel):
    token: str = TokenField
    new_password: str = PasswordField


def user_out(user_id: uuid.UUID, email: str, display_name: str | None, role: str) -> dict:
    return {"id": str(user_id), "email": email, "display_name": display_name, "role": role}


def _weak_password(exc: PasswordPolicyError) -> ApiError:
    return ApiError(422, "weak_password", message=str(exc))


@router.get("/csrf")
def csrf(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Ensure the browser holds a usable CSRF cookie (anonymous or bound to
    the live session). Returns no token in the body; the page reads the cookie."""
    cookie = request.cookies.get(CSRF_COOKIE)
    raw_session = request.cookies.get(session_cookie_name(settings))
    ctx = resolve_session(db, raw_session, now=now, idle=settings.auth.session_idle) if raw_session else None
    if ctx is not None:
        if not (cookie and digest_matches(cookie, ctx.csrf_token_hash)):
            set_csrf_cookie(response, settings, rotate_csrf(db, ctx.session_id))
        db.commit()
    elif not cookie:
        set_csrf_cookie(response, settings, generate_token()[0])
    return {"status": "ok"}


@router.post("/login")
def login_route(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        user, issued = login(db, email=body.email, password=body.password, now=now, settings=settings,
                             previous_token=request.cookies.get(session_cookie_name(settings)))
    except InvalidCredentials:
        raise ApiError(401, "invalid_credentials") from None
    set_session_cookie(response, settings, issued.token)
    set_csrf_cookie(response, settings, issued.csrf_token)
    return {"user": user_out(user.id, user.email, user.display_name, user.role)}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    ctx: SessionContext = Depends(get_session_context),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> Response:
    revoke_session(db, session_id=ctx.session_id, user_id=ctx.user_id, reason=SessionRevokeReason.LOGOUT, now=now)
    _audit_session(db, ctx, "auth.logout", ctx.session_id)
    commit_auth(db)
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_session_cookie(response, settings)
    return response


@router.get("/session")
def current_session(ctx: SessionContext = Depends(get_session_context)) -> dict:
    return {
        "user": user_out(ctx.user_id, ctx.email, ctx.display_name, ctx.role),
        "session": {
            "id": str(ctx.session_id),
            "idle_expires_at": ctx.idle_expires_at.isoformat(),
            "absolute_expires_at": ctx.absolute_expires_at.isoformat(),
        },
    }


@router.get("/sessions")
def my_sessions(
    ctx: SessionContext = Depends(get_session_context),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    rows = list_live_sessions(db, user_id=ctx.user_id, now=now, idle=settings.auth.session_idle)
    return {
        "sessions": [
            {
                "id": str(s.id),
                "created_at": s.created_at.isoformat(),
                "last_activity_at": s.last_activity_at.isoformat(),
                "absolute_expires_at": s.absolute_expires_at.isoformat(),
                "current": s.id == ctx.session_id,
            }
            for s in rows
        ]
    }


@router.post("/sessions/{session_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_session(
    session_id: uuid.UUID,
    response: Response,
    ctx: SessionContext = Depends(get_session_context),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Scoped to the caller's own sessions: another user's session ID is
    # indistinguishable from an unknown one.
    if not revoke_session(
        db, session_id=session_id, user_id=ctx.user_id, reason=SessionRevokeReason.USER_REVOKED, now=now
    ):
        db.rollback()
        raise ApiError(404, "not_found")
    _audit_session(db, ctx, "auth.session.revoke", session_id)
    commit_auth(db)
    response.status_code = status.HTTP_204_NO_CONTENT
    if session_id == ctx.session_id:
        clear_session_cookie(response, settings)
    return response


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    ctx: SessionContext = Depends(get_session_context),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict:
    count = revoke_user_sessions(
        db, user_id=ctx.user_id, reason=SessionRevokeReason.USER_REVOKED, now=now, keep_session_id=ctx.session_id
    )
    _audit_session(db, ctx, "auth.session.revoke_others", ctx.session_id, details={"revoked": count})
    commit_auth(db)
    return {"revoked": count}


@router.post("/invitations/inspect")
def inspect_invitation(body: TokenIn, db: Session = Depends(get_db), now: datetime = Depends(get_now)) -> dict:
    """Show the invitee which email they are activating (token in the body,
    never in a URL)."""
    inv = invitations.find_open_invitation(db, body.token, now=now)
    if inv is None:
        raise ApiError(400, "invalid_token")
    return {"email": inv.email, "display_name": inv.display_name, "expires_at": inv.expires_at.isoformat()}


@router.post("/invitations/accept", status_code=status.HTTP_201_CREATED)
def accept_invitation(
    body: AcceptInvitationIn,
    clock=Depends(get_clock),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        user: User = invitations.accept_invitation(
            db,
            raw_token=body.token,
            password=body.password,
            display_name=body.display_name,
            now=now,
            policy=settings.password,
            clock=clock,
        )
    except InvalidToken:
        raise ApiError(400, "invalid_token") from None
    except PasswordPolicyError as exc:
        raise _weak_password(exc) from None
    return {"user": user_out(user.id, user.email, user.display_name, user.role)}


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
def request_password_reset(
    body: ResetRequestIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
    delivery: DeliveryAdapter = Depends(get_delivery),
) -> dict:
    """Generic acceptance, not a guarantee of delivery. Only a known committed
    token reaches the adapter. This does not claim to prevent timing enumeration."""
    if delivery.enabled:
        try:
            issued = password_reset.request_reset(db, email=body.email, now=now, lifetime=settings.auth.password_reset)
        except Exception:  # No account-dependent status/body or delivery after an uncertain commit.
            db.rollback()
            issued = None
            log.error("password reset request unavailable")
        if issued is not None:
            message = OutgoingMessage(
                kind=MessageKind.PASSWORD_RESET,
                recipient=issued.email,
                link=build_link(settings.http.public_base_url or "", "/reset-password", issued.token),
                expires_at=issued.expires_at,
            )
            background.add_task(_send_quietly, delivery, message, now)
    else:
        log.warning("password reset requested but no delivery channel is configured")
    return {"status": "accepted"}


@router.post("/password-reset/confirm")
def confirm_password_reset(
    body: ResetConfirmIn,
    response: Response,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        password_reset.confirm_reset(
            db, raw_token=body.token, new_password=body.new_password, now=now, policy=settings.password
        )
    except InvalidToken:
        raise ApiError(400, "invalid_token") from None
    except PasswordPolicyError as exc:
        raise _weak_password(exc) from None
    clear_session_cookie(response, settings)
    return {"status": "password_changed"}


def _send_quietly(delivery: DeliveryAdapter, message: OutgoingMessage, now: datetime) -> None:
    try:
        delivery.send(message, now=now)
    except Exception as exc:  # noqa: BLE001
        # Type only: the message holds the link (token) and the recipient.
        log.error("password reset delivery failed (%s)", type(exc).__name__)


def _audit_session(db: Session, ctx: SessionContext, action: str, session_id: uuid.UUID, details=None) -> None:
    record_event(
        db,
        action=action,
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=ctx.user_id,
        target_type="session",
        target_id=str(session_id),
        details=details,
    )
