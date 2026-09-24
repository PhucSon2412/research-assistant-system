from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import UserRole, UserStatus, sql_in


class User(Base):
    """Internal account. Invitations, sessions and reset tokens (M1b) reference
    ``users.id``; there is no built-in/default account."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"role IN ({sql_in(UserRole)})", name="role_valid"),
        CheckConstraint(f"status IN ({sql_in(UserStatus)})", name="status_valid"),
        # Stored normalised so the plain unique constraint is case-insensitive.
        CheckConstraint("email = lower(btrim(email)) AND email LIKE '_%@_%'", name="email_normalised"),
        # Only Argon2id PHC strings are accepted: a plaintext password can
        # never be stored by mistake.
        CheckConstraint("password_verifier LIKE '$argon2id$%'", name="password_verifier_argon2id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default=UserRole.USER.value)
    status: Mapped[str] = mapped_column(Text, server_default=UserStatus.ACTIVE.value)
    password_verifier: Mapped[str] = mapped_column(Text)
    # M1b: sessions issued before this instant are invalid after a reset.
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BootstrapState(Base):
    """Singleton row written in the same transaction as the first Admin.

    The boolean primary key constrained to TRUE admits exactly one row, so
    concurrent bootstrap attempts cannot both commit.
    """

    __tablename__ = "bootstrap_state"
    __table_args__ = (CheckConstraint("id", name="singleton"),)

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
