"""DB commit receipts and rebuildable journal projection (C1, pending)."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class AuthHashPolicy(Base):
    __tablename__ = "auth_hash_policies"
    __table_args__ = (CheckConstraint("version > 0 AND memory_kib >= 8 * parallelism AND time_cost > 0 AND parallelism > 0 AND salt_bytes > 0 AND hash_bytes > 0", name="parameters_valid"),)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    memory_kib: Mapped[int] = mapped_column(Integer)
    time_cost: Mapped[int] = mapped_column(Integer)
    parallelism: Mapped[int] = mapped_column(Integer)
    salt_bytes: Mapped[int] = mapped_column(Integer)
    hash_bytes: Mapped[int] = mapped_column(Integer)


class AuthChangeReceipt(Base):
    __tablename__ = "auth_change_receipts"
    __table_args__ = (
        UniqueConstraint("operation_id", "user_id", "domain", "generation", name="uq_auth_receipt_binding"),
        UniqueConstraint("generation", name="uq_auth_receipt_generation"),
        CheckConstraint("domain IN ('credential','status')", name="domain_valid"),
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    domain: Mapped[str] = mapped_column(Text)
    generation: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    decision: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transaction_id: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))


class AuthSafetyHead(Base):
    __tablename__ = "auth_safety_heads"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    projection_head_seq: Mapped[int] = mapped_column(BigInteger)
    credential_seq: Mapped[int | None] = mapped_column(BigInteger)
    credential_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    credential_generation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    credential_reason_code: Mapped[str | None] = mapped_column(Text)
    credential_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_seq: Mapped[int | None] = mapped_column(BigInteger)
    status_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status_generation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status_decision: Mapped[str | None] = mapped_column(Text)
    status_reason_code: Mapped[str | None] = mapped_column(Text)
    status_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSafetyState(Base):
    __tablename__ = "auth_safety_state"
    __table_args__ = (
        CheckConstraint("id = 1 AND version = 1 AND cutover_state = 'pending'", name="pending_version"),
        CheckConstraint("projection_seq >= 0", name="projection_seq_valid"),
    )
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    version: Mapped[int] = mapped_column(SmallInteger)
    cutover_state: Mapped[str] = mapped_column(Text)
    projection_seq: Mapped[int] = mapped_column(BigInteger)
    backfill_complete: Mapped[bool] = mapped_column(Boolean)


class AuthRecoveryOperator(Base):
    """C3: trusted provisioning maps one real LOGIN role to one operator UUID.

    Only the migrator/provisioner writes it; routines resolve the operator from
    ``session_user``, never from a caller argument or role name supplied by CLI.
    A row may only be revoked once; it is never deleted or rebound.
    """

    __tablename__ = "auth_recovery_operators"
    __table_args__ = (
        UniqueConstraint("login_role", name="uq_auth_recovery_operators_login_role"),
        CheckConstraint("login_role ~ '^[a-z][a-z0-9_]{0,62}$'", name="login_role_valid"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= provisioned_at", name="revoked_after_provisioned"),
    )
    operator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    login_role: Mapped[str] = mapped_column(Text)
    provisioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthRecoveryCase(Base):
    """C3: one approved, single-use decision registered by trusted provisioning.

    Mirrors the restricted case file outside repo/backup: IDs, codes and times
    only. Consumption is recorded in the independent journal, not here.
    """

    __tablename__ = "auth_recovery_cases"
    __table_args__ = (
        CheckConstraint("case_id::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
                        name="case_id_v4"),
        CheckConstraint("action IN ('set_credential','resolve_initial_status')", name="action_valid"),
        CheckConstraint("expected_role IN ('admin','user')", name="expected_role_valid"),
        CheckConstraint("verification_method ~ '^[a-z][a-z0-9_]{2,40}$'", name="verification_method_code"),
        CheckConstraint("isfinite(approved_at) AND isfinite(expires_at) AND expires_at > approved_at",
                        name="window_valid"),
    )
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    action: Mapped[str] = mapped_column(Text)
    operator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth_recovery_operators.operator_id", ondelete="RESTRICT"))
    approver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    journal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    expected_role: Mapped[str] = mapped_column(Text)
    expected_credential_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expected_status_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    verification_method: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
