"""Admin-only endpoints: invitations, accounts, dev outbox.

Every route depends on ``require_admin``: guests get 401, Users get 403.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_now, get_clock, require_admin
from app.api.cookies import CSRF_HEADER
from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal
from app.config import Settings, get_settings
from app.delivery import DeliveryAdapter, DevOutbox, get_delivery
from app.identity import invitations, users
from app.identity.final_caller import final_caller
from app.identity.session_commit import commit_activity
from app.identity.emails import EmailFormatError
from app.identity.errors import AccountExists, Forbidden, InvitationConflict, NotFound
from app.models import Invitation, User
from app.models.enums import AuditActorType, AuditOutcome, UserStatus

router = APIRouter(prefix="/admin", tags=["admin"])


class InvitationIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)


def _invitation_out(inv: Invitation, now: datetime) -> dict:
    return {
        "id": str(inv.id),
        "email": inv.email,
        "display_name": inv.display_name,
        "status": invitations.invitation_status(inv, now),
        "created_at": inv.created_at.isoformat(),
        "expires_at": inv.expires_at.isoformat(),
        "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
        # Invitations only ever create User accounts (see identity/invitations.py).
        "role": "user",
    }


def _user_out(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat(),
    }


@router.post("/invitations", status_code=status.HTTP_201_CREATED)
def create_invitation(
    request: Request,
    body: InvitationIn,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
    delivery: DeliveryAdapter = Depends(get_delivery),
    clock=Depends(get_clock),
) -> dict:
    if not delivery.enabled:
        raise ApiError(503, "delivery_unavailable")
    try:
        created = invitations.create_invitation(
            db,
            inviter_id=admin.user_id,
            actor_session_id=admin.session_id,
            csrf_token=request.headers.get(CSRF_HEADER) or "",
            email=body.email,
            display_name=body.display_name,
            now=now,
            lifetime=settings.auth.invite,
            clock=clock,
        )
    except EmailFormatError:
        raise ApiError(422, "invalid_email") from None
    except AccountExists:
        raise ApiError(409, "account_exists") from None
    except InvitationConflict:
        raise ApiError(409, "invitation_conflict") from None
    inv = invitations.deliver_invitation(db, created, actor_id=admin.user_id, actor_session_id=admin.session_id,
        csrf_token=request.headers.get(CSRF_HEADER) or "", delivery=delivery, settings=settings, clock=clock)
    return {"invitation": _invitation_out(inv, clock())}


@router.get("/invitations")
def list_invitations(
    admin: Principal = Depends(require_admin), db: Session = Depends(get_db), clock=Depends(get_clock)
) -> dict:
    rows = invitations.list_invitations(db, actor_id=admin.user_id, actor_session_id=admin.session_id, clock=clock)
    return {"invitations": [_invitation_out(inv, clock()) for inv in rows]}


@router.post("/invitations/{invitation_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invitation(
    request: Request,
    invitation_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    clock=Depends(get_clock),
) -> Response:
    try:
        invitations.revoke_invitation(db, invitation_id=invitation_id, actor_id=admin.user_id, now=now,
            actor_session_id=admin.session_id, csrf_token=request.headers.get(CSRF_HEADER) or "", clock=clock)
    except NotFound:
        raise ApiError(404, "not_found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users")
def list_users(admin: Principal = Depends(require_admin), db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings), clock=Depends(get_clock)) -> dict:
    rows = [_user_out(u) for u in users.list_users(db)]
    final_caller(db, session_id=admin.session_id, user_id=admin.user_id,
                 clock=clock, idle=settings.auth.session_idle, admin=True)
    return {"users": rows}


def _set_status(db: Session, admin: Principal, user_id: uuid.UUID, new_status: UserStatus, now: datetime,
                request: Request, clock) -> dict:
    try:
        user = users.set_user_status(db, user_id=user_id, actor_id=admin.user_id, status=new_status, now=now,
            actor_session_id=admin.session_id, csrf_token=request.headers.get(CSRF_HEADER) or "", clock=clock)
    except NotFound:
        raise ApiError(404, "not_found") from None
    except Forbidden:
        raise ApiError(400, "cannot_change_own_status") from None
    return {"user": _user_out(user)}


@router.post("/users/{user_id}/disable")
def disable_user(
    request: Request,
    user_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    clock=Depends(get_clock),
) -> dict:
    return _set_status(db, admin, user_id, UserStatus.DISABLED, now, request, clock)


@router.post("/users/{user_id}/enable")
def enable_user(
    request: Request,
    user_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    clock=Depends(get_clock),
) -> dict:
    return _set_status(db, admin, user_id, UserStatus.ACTIVE, now, request, clock)


@router.get("/dev-outbox")
def dev_outbox(
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    delivery: DeliveryAdapter = Depends(get_delivery),
    settings: Settings = Depends(get_settings),
    clock=Depends(get_clock),
) -> dict:
    """Development only (RAS_DELIVERY__MODE=dev_outbox, which the settings
    refuse outside RAS_ENVIRONMENT=development). 404 otherwise."""
    if settings.environment != "development" or not isinstance(delivery, DevOutbox):
        raise ApiError(404, "not_found")
    record_event(
        db,
        action="dev_outbox.read",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=admin.user_id,
    )
    # Flush the audit FK before the final shared journal lock. No link is read
    # until both the audit commit and a new transaction's authority check pass.
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise ApiError(503, "auth_unavailable") from None
    final_caller(db, session_id=admin.session_id, user_id=admin.user_id,
                 clock=clock, idle=settings.auth.session_idle, admin=True)
    commit_activity(db)
    caller = final_caller(db, session_id=admin.session_id, user_id=admin.user_id,
                          clock=clock, idle=settings.auth.session_idle, admin=True)
    entries = delivery.entries(now=caller.checked_at)
    caller = final_caller(db, session_id=admin.session_id, user_id=admin.user_id,
                          clock=clock, idle=settings.auth.session_idle, admin=True)
    return {
        "messages": [
            {
                "id": e.id,
                "kind": e.message.kind.value,
                "recipient": e.message.recipient,
                "link": e.message.link,
                "created_at": e.created_at.isoformat(),
                "expires_at": e.message.expires_at.isoformat(),
            }
            for e in entries if e.message.expires_at > caller.checked_at
        ]
    }
