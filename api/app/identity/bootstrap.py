"""One-time creation of the first Admin.

Concurrency: the transaction takes a PostgreSQL advisory lock, checks that no
bootstrap has happened and no user exists, then inserts the Admin together
with the singleton ``bootstrap_state`` row. Even without the lock, the
singleton primary key lets only one transaction commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, exists, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_event
from app.config import PasswordPolicy
from app.identity.emails import normalise_email
from app.identity.passwords import hash_password, validate_password
from app.models import BootstrapState, User
from app.models.enums import AuditActorType, AuditOutcome, UserRole, UserStatus

AUDIT_ACTION = "admin.bootstrap"
# Arbitrary constant key for pg_advisory_xact_lock ("RASBOOT1" as int64).
_ADVISORY_LOCK_KEY = 0x524153424F4F5431


class BootstrapRefused(Exception):
    """Bootstrap already happened (or users exist); nothing was created."""


@dataclass(frozen=True)
class BootstrapResult:
    user_id: uuid.UUID
    email: str


def bootstrap_admin(
    engine: Engine,
    *,
    email: str,
    password: str,
    policy: PasswordPolicy,
    display_name: str | None = None,
) -> BootstrapResult:
    from app.auth_safety import require_writer
    with Session(engine) as gate_db:
        require_writer(gate_db)
    email = normalise_email(email)
    validate_password(password, policy, email=email)
    # Hash before opening the transaction to keep the lock window short.
    verifier = hash_password(password)

    try:
        with Session(engine) as session, session.begin():
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
            already = session.scalar(select(exists().select_from(BootstrapState))) or session.scalar(
                select(exists().select_from(User))
            )
            if already:
                raise BootstrapRefused("bootstrap has already been completed")

            user = User(
                email=email,
                display_name=display_name,
                role=UserRole.ADMIN.value,
                status=UserStatus.ACTIVE.value,
                password_verifier=verifier,
            )
            session.add(user)
            session.flush()
            session.add(BootstrapState(admin_user_id=user.id))
            record_event(
                session,
                action=AUDIT_ACTION,
                outcome=AuditOutcome.SUCCESS,
                actor_type=AuditActorType.SYSTEM,
                target_type="user",
                target_id=str(user.id),
                details={"via": "cli"},
            )
            result = BootstrapResult(user_id=user.id, email=email)
    except IntegrityError as exc:
        # Lost a race that bypassed the advisory lock: the singleton row won.
        # Any other constraint failure is a real error, not a refusal.
        if _constraint_name(exc) not in _RACE_CONSTRAINTS:
            raise
        _record_refusal(engine)
        raise BootstrapRefused("bootstrap has already been completed") from exc
    except BootstrapRefused:
        _record_refusal(engine)
        raise
    return result


_RACE_CONSTRAINTS = {"pk_bootstrap_state", "uq_users_email"}


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _record_refusal(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        record_event(
            session,
            action=AUDIT_ACTION,
            outcome=AuditOutcome.DENIED,
            actor_type=AuditActorType.SYSTEM,
            details={"via": "cli", "reason": "already_bootstrapped"},
        )
