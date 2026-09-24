from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AuditActorType, AuditOutcome, sql_in


class AuditEvent(Base):
    """Append-only (DB trigger blocks UPDATE/DELETE).

    ``details`` must never hold secrets, tokens or paper content; callers pass
    only identifiers and short reason codes.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(f"actor_type IN ({sql_in(AuditActorType)})", name="actor_type_valid"),
        CheckConstraint(
            "(actor_type = 'user') = (actor_user_id IS NOT NULL)", name="actor_user_matches_type"
        ),
        CheckConstraint(f"outcome IN ({sql_in(AuditOutcome)})", name="outcome_valid"),
        CheckConstraint("length(action) > 0", name="action_not_empty"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
        Index("ix_audit_events_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_type: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
