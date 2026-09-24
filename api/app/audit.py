from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.models.enums import AuditActorType, AuditOutcome


def record_event(
    session: Session,
    *,
    action: str,
    outcome: AuditOutcome,
    actor_type: AuditActorType,
    actor_user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the caller's transaction.

    ``details`` is for identifiers and short reason codes only: never
    passwords, verifiers, tokens or paper content.
    """
    session.add(
        AuditEvent(
            action=action,
            outcome=outcome.value,
            actor_type=actor_type.value,
            actor_user_id=actor_user_id,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
        )
    )
