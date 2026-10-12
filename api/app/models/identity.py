from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Computed, Integer, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import SessionRevokeReason, UserRole, UserStatus, sql_in

# Tokens (invitation, reset, session, CSRF) are stored only as SHA-256 hex of
# a 256-bit random value; the raw token exists only in the link/cookie.
_SHA256_HEX = "~ '^[0-9a-f]{64}$'"


RECOVERY_VALID = ("(closed_by_recovery_at IS NULL AND recovery_end_kind IS NULL) OR"
                  " (closed_by_recovery_at IS NOT NULL AND recovery_end_kind IS NOT NULL"
                  " AND recovery_end_kind IN ('accepted','revoked') AND closure_reason IS NOT NULL"
                  " AND accepted_at IS NULL AND accepted_user_id IS NULL AND revoked_at IS NULL)")
REASON_VALID = ("closure_reason IS NULL OR (COALESCE(recovery_end_kind,"
                " CASE WHEN accepted_at IS NOT NULL THEN 'accepted' ELSE 'revoked' END) = 'accepted'"
                " AND closure_reason IN ('invitation_accepted','legacy_accepted')"
                " AND COALESCE(accepted_at,closed_by_recovery_at) IS NOT NULL) OR"
                " (COALESCE(recovery_end_kind,CASE WHEN revoked_at IS NOT NULL THEN 'revoked' END) = 'revoked'"
                " AND closure_reason IN ('admin_revoked','invitation_replaced','delivery_failed','legacy_revoked')"
                " AND COALESCE(revoked_at,closed_by_recovery_at) IS NOT NULL)")
OPEN_INDEX = "accepted_at IS NULL AND revoked_at IS NULL AND closed_by_recovery_at IS NULL"


class User(Base):
    """Internal account, created only by bootstrap (first Admin) or by
    accepting an invitation; there is no built-in/default account."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("credential_state IN ('usable','quarantined') AND status_safety IN ('proven','held') AND password_hash_policy_version > 0", name="auth_states"),
        CheckConstraint("credential_state <> 'usable' OR (credential_generation IS NOT NULL AND credential_operation_id IS NOT NULL)", name="credential_evidence"),
        CheckConstraint("status_safety <> 'proven' OR (status_generation IS NOT NULL AND (status = 'disabled' OR status_operation_id IS NOT NULL))", name="status_evidence"),
        ForeignKeyConstraint(["credential_operation_id", "id", "credential_domain", "credential_generation"], ["auth_change_receipts.operation_id", "auth_change_receipts.user_id", "auth_change_receipts.domain", "auth_change_receipts.generation"], name="fk_users_credential_receipt", deferrable=True, initially="DEFERRED", use_alter=True),
        ForeignKeyConstraint(["status_operation_id", "id", "status_domain", "status_generation"], ["auth_change_receipts.operation_id", "auth_change_receipts.user_id", "auth_change_receipts.domain", "auth_change_receipts.generation"], name="fk_users_status_receipt", deferrable=True, initially="DEFERRED", use_alter=True),
        CheckConstraint(f"role IN ({sql_in(UserRole)})", name="role_valid"),
        CheckConstraint(f"status IN ({sql_in(UserStatus)})", name="status_valid"),
        # Stored normalised so the plain unique constraint is case-insensitive.
        CheckConstraint("email = lower(btrim(email)) AND email LIKE '_%@_%'", name="email_normalised"),
        # Only Argon2id PHC strings are accepted: a plaintext password can
        # never be stored by mistake.
        CheckConstraint("password_verifier LIKE '$argon2id$%'", name="password_verifier_argon2id"),
    )

    credential_domain: Mapped[str] = mapped_column(Text, Computed("'credential'", persisted=True))
    status_domain: Mapped[str] = mapped_column(Text, Computed("'status'", persisted=True))
    credential_generation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    credential_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    credential_state: Mapped[str] = mapped_column(Text, server_default="quarantined")
    credential_reason_code: Mapped[str] = mapped_column(Text, server_default="legacy_unproven")
    status_generation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status_safety: Mapped[str] = mapped_column(Text, server_default="held")
    status_reason_code: Mapped[str] = mapped_column(Text, server_default="legacy_unproven")
    password_hash_policy_version: Mapped[int] = mapped_column(Integer, ForeignKey("auth_hash_policies.version", ondelete="RESTRICT"), server_default=text("1"))

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default=UserRole.USER.value)
    status: Mapped[str] = mapped_column(Text, server_default=UserStatus.ACTIVE.value)
    password_verifier: Mapped[str] = mapped_column(Text)
    # Updated on every password change. Sessions are invalidated by explicit
    # revocation in the same transaction (app/identity/password_reset.py),
    # not by comparing timestamps from two different clocks.
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
    creation_transaction_id: Mapped[int | None] = mapped_column(BigInteger)


class Invitation(Base):
    """Single-use invitation. Accepting it always creates a ``user`` account:
    there is no role column, so an invitation cannot grant Admin (granting
    Admin is an open decision, see docs/decisions.md)."""

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint("email = lower(btrim(email)) AND position('@' in email) > 1", name="email_normalised"),
        CheckConstraint(f"token_hash {_SHA256_HEX}", name="token_hash_sha256_hex"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint(
            "(accepted_at IS NULL) = (accepted_user_id IS NULL)", name="accepted_fields_consistent"
        ),
        CheckConstraint("accepted_at IS NULL OR revoked_at IS NULL", name="accepted_or_revoked"),
        CheckConstraint(RECOVERY_VALID, name="recovery_fields_consistent"),
        CheckConstraint("COALESCE((" + REASON_VALID + "), false)", name="closure_reason_valid"),
        # At most one open invitation per email; a new one revokes the old.
        Index(
            "uq_invitations_open_email",
            "email",
            unique=True,
            postgresql_where=text(OPEN_INDEX),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_end_kind: Mapped[str | None] = mapped_column(Text)
    closure_reason: Mapped[str | None] = mapped_column(Text)
    creation_transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    acceptance_transaction_id: Mapped[int | None] = mapped_column(BigInteger)


class UserSession(Base):
    """Server-side session. Valid while not revoked, the user is active,
    ``now < last_activity_at + idle`` and ``now < absolute_expires_at``."""

    credential_generation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(f"token_hash {_SHA256_HEX}", name="token_hash_sha256_hex"),
        CheckConstraint(f"csrf_token_hash {_SHA256_HEX}", name="csrf_token_hash_sha256_hex"),
        CheckConstraint("absolute_expires_at > created_at", name="expires_after_created"),
        CheckConstraint("last_activity_at >= created_at", name="activity_after_created"),
        CheckConstraint("(revoked_at IS NULL) = (revoked_reason IS NULL)", name="revoked_fields_consistent"),
        CheckConstraint(
            f"revoked_reason IS NULL OR revoked_reason IN ({sql_in(SessionRevokeReason)})",
            name="revoked_reason_valid",
        ),
        Index("ix_sessions_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class PasswordResetToken(Base):
    """Single-use reset token. A newer request or a password change revokes
    any other open token of the same user."""

    credential_fence_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    recovery_purpose: Mapped[str] = mapped_column(Text, server_default="normal")
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        CheckConstraint(f"token_hash {_SHA256_HEX}", name="token_hash_sha256_hex"),
        CheckConstraint("recovery_purpose IN ('normal','quarantine')", name="recovery_purpose_valid"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint("used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked"),
        CheckConstraint("closure_reason IS NULL OR (used_at IS NOT NULL AND closure_reason IN"
                        " ('password_reset', 'legacy_consumed')) OR (revoked_at IS NOT NULL AND closure_reason IN"
                        " ('reset_replaced', 'reset_completed', 'user_disabled', 'legacy_revoked', 'auth_safety_backfill',"
                        " 'operator_recovery'))",
                        name="closure_reason_valid"),
        Index(
            "uq_password_reset_tokens_open_user_id",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_reason: Mapped[str | None] = mapped_column(Text)
