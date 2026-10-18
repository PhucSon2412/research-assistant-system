from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, Index, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

JOURNAL_KINDS = "'genesis', 'revocation', 'recovery_begin', 'recovery_end', 'review_closure', 'session_revocation', 'reset_token_closure', 'invitation_closure', 'credential_transition', 'account_status_decision', 'operator_recovery_case', 'private_deletion'"
TOKEN_IDS_REQUIRED = ("CASE WHEN kind = 'reset_token_closure' THEN reset_token_id IS NOT NULL"
                      " AND reset_token_user_id IS NOT NULL AND reset_end_kind IS NOT NULL"
                      " AND reset_reason_code IS NOT NULL AND reset_ended_at IS NOT NULL"
                      " ELSE reset_token_id IS NULL AND reset_token_user_id IS NULL AND reset_end_kind IS NULL"
                      " AND reset_reason_code IS NULL AND reset_ended_at IS NULL END")
TOKEN_MATCHES_BODY = ("CASE WHEN kind = 'reset_token_closure' THEN"
    " (reset_token_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,token_id}'))"
    " AND (reset_token_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}'))"
    " AND (reset_end_kind IS NOT DISTINCT FROM (line::jsonb #>> '{body,end_kind}'))"
    " AND (reset_reason_code IS NOT DISTINCT FROM (line::jsonb #>> '{body,reason_code}'))"
    " AND (reset_ended_at IS NOT DISTINCT FROM (line::jsonb #>> '{body,ended_at}')::timestamptz)"
    " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
    " AND COALESCE((reset_end_kind = 'consumed' AND reset_reason_code IN ('password_reset', 'legacy_consumed'))"
    " OR (reset_end_kind = 'revoked' AND reset_reason_code IN"
    " ('reset_replaced', 'reset_completed', 'user_disabled', 'legacy_revoked', 'auth_safety_backfill', 'operator_recovery')), false) ELSE true END")
CASE_FIELDS = ("case_id", "case_operator_id", "case_user_id", "case_action", "case_operation_id", "case_recorded_at")
CASE_FIELDS_REQUIRED = (f"CASE WHEN kind = 'operator_recovery_case' THEN num_nonnulls({', '.join(CASE_FIELDS)}) = 6"
                        f" ELSE num_nonnulls({', '.join(CASE_FIELDS)}) = 0 END")
INVITATION_IDS_REQUIRED = ("CASE WHEN kind = 'invitation_closure' THEN invitation_id IS NOT NULL"
                      " AND invitation_end_kind IS NOT NULL"
                      " AND invitation_reason_code IS NOT NULL AND invitation_ended_at IS NOT NULL"
                      " ELSE invitation_id IS NULL AND invitation_end_kind IS NULL"
                      " AND invitation_reason_code IS NULL AND invitation_ended_at IS NULL END")
INVITATION_MATCHES_BODY = ("CASE WHEN kind = 'invitation_closure' THEN"
    " (invitation_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,invitation_id}'))"
    " AND (invitation_end_kind IS NOT DISTINCT FROM (line::jsonb #>> '{body,end_kind}'))"
    " AND (invitation_reason_code IS NOT DISTINCT FROM (line::jsonb #>> '{body,reason_code}'))"
    " AND (invitation_ended_at IS NOT DISTINCT FROM (line::jsonb #>> '{body,ended_at}')::timestamptz)"
    " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
    " AND COALESCE((invitation_end_kind = 'accepted' AND invitation_reason_code IN ('invitation_accepted', 'legacy_accepted'))"
    " OR (invitation_end_kind = 'revoked' AND invitation_reason_code IN"
    " ('invitation_replaced', 'admin_revoked', 'delivery_failed', 'legacy_revoked')), false) ELSE true END")
APPLIED_VIA = "'genesis', 'revoke', 'close', 'replay', 'import'"
REGISTRATION_MODES = "'new_install', 'existing_db', 'adopted', 'rebuilt_from_db'"


class RecoveryRegistration(Base):
    """Which revocation journal this database belongs to (M6c-1). At most one row, never changed.

    Being registered is **not** evidence that the database is current: the
    recovery gate (``app.recovery``) compares the journal file, kept outside
    the database and object backups, with ``RevocationJournalRecord`` on
    every check. A restored database keeps this row and its old records, so
    it is caught as behind the journal.
    """

    __tablename__ = "recovery_registrations"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("genesis_hash ~ '^[0-9a-f]{64}$'", name="genesis_hash_hex"),
        CheckConstraint(f"mode IN ({REGISTRATION_MODES})", name="mode_valid"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    journal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    genesis_hash: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RevocationJournalRecord(Base):
    """The database's copy of one journal record: its exact line, hash and how it got here.

    A revocation writes its record here and to the journal file in the same
    transaction (``app.recovery.record_revocation``); a replay after restore
    writes the record it applied. ``line`` holds only what the journal holds
    (IDs, codes, times, retention bases). Append-only; a tombstone cannot be
    committed without the record naming it (deferred trigger).

    ``journal_confirmed`` (0027) says the line was written **and** ``fsync``
    returned. A revocation whose write failed, even after a complete line,
    commits with ``false`` and keeps the recovery gate closed; the only change
    the DB allows on a row is ``false -> true``, made by ``recover`` after its
    own successful ``fsync`` of the file. No default: every writer states it.

    ``review_closure`` (0028, M6c-2b1) records that a review request's
    temporary Admin grant ended (withdraw, reject, approve), written by the
    closing transaction (``applied_via='close'``) like a revocation.
    ``review_request_id`` names the request (no FK: a restored snapshot may
    lack it); the closing ``UPDATE`` cannot commit without this row (deferred
    trigger on ``review_requests``). 0029: ``review_request_id`` must equal the
    line's ``body.request_id`` (CHECK, at insert, so before the line is appended).
    """

    __tablename__ = "revocation_journal_records"
    __table_args__ = (
        UniqueConstraint("record_hash", name="uq_revocation_journal_records_record_hash"),
        UniqueConstraint("tombstone_id", name="uq_revocation_journal_records_tombstone_id"),
        UniqueConstraint("review_request_id", name="uq_revocation_journal_records_review_request_id"),
        UniqueConstraint("session_id", name="uq_revocation_journal_records_session_id"),
        UniqueConstraint("reset_token_id", name="uq_revocation_journal_records_reset_token_id"),
        UniqueConstraint("invitation_id", name="uq_revocation_journal_records_invitation_id"),
        # C3 (0049): a case is consumed once and authorises exactly one auth operation.
        UniqueConstraint("case_id", name="uq_revocation_journal_records_case_id"),
        UniqueConstraint("case_operation_id", name="uq_revocation_journal_records_case_operation_id"),
        CheckConstraint(CASE_FIELDS_REQUIRED, name="case_fields_required"),
        CheckConstraint("CASE WHEN kind='private_deletion' THEN private_deletion_id IS NOT NULL"
                        " AND private_deletion_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,id}')"
                        " ELSE private_deletion_id IS NULL END", name="private_deletion_binding"),
        CheckConstraint(INVITATION_IDS_REQUIRED, name="invitation_fields_required"),
        CheckConstraint(INVITATION_MATCHES_BODY, name="invitation_matches_body"),
        CheckConstraint(TOKEN_IDS_REQUIRED, name="reset_fields_required"),
        CheckConstraint(TOKEN_MATCHES_BODY, name="reset_matches_body"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint(f"kind IN ({JOURNAL_KINDS})", name="kind_valid"),
        CheckConstraint("record_hash ~ '^[0-9a-f]{64}$'", name="record_hash_hex"),
        CheckConstraint("length(line) BETWEEN 2 AND 65535", name="line_length"),
        CheckConstraint(f"applied_via IN ({APPLIED_VIA})", name="applied_via_valid"),
        CheckConstraint("result ~ '^[a-z_]{1,64}$'", name="result_is_code"),
        CheckConstraint("kind = 'revocation' OR tombstone_id IS NULL", name="tombstone_only_for_revocation"),
        CheckConstraint("kind <> 'revocation' OR tombstone_id IS NOT NULL OR result = 'superseded'",
                        name="revocation_names_tombstone"),
        CheckConstraint("(kind = 'genesis') = (seq = 1)", name="genesis_first"),
        CheckConstraint("kind = 'review_closure' OR review_request_id IS NULL", name="request_only_for_closure"),
        CheckConstraint("kind <> 'review_closure' OR review_request_id IS NOT NULL OR result = 'superseded'",
                        name="closure_names_request"),
        # 0029: the row is filed under the request its own line names (checked at insert, before the append);
        # IS NOT DISTINCT FROM so a missing or null body.request_id is FALSE, never NULL (a CHECK passes NULL).
        CheckConstraint("CASE WHEN kind = 'review_closure' AND review_request_id IS NOT NULL"
                        " THEN review_request_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,request_id}')"
                        " ELSE true END",
                        name="closure_request_matches_body"),
        Index("ix_revocation_journal_records_unconfirmed", "seq", postgresql_where=text("NOT journal_confirmed")),
        CheckConstraint("CASE WHEN kind = 'session_revocation' THEN session_id IS NOT NULL"
                        " AND session_user_id IS NOT NULL ELSE session_id IS NULL AND session_user_id IS NULL END",
                        name="session_ids_required"),
        CheckConstraint("CASE WHEN kind = 'session_revocation' THEN"
                        " (session_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,session_id}'))"
                        " AND (session_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}'))"
                        " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
                        " AND COALESCE((line::jsonb #>> '{body,reason_code}') IN"
                        " ('logout', 'replaced', 'user_revoked', 'password_reset', 'user_disabled', 'auth_safety_backfill',"
                        " 'operator_recovery'), false)"
                        " AND COALESCE(length(line::jsonb #>> '{body,revoked_at}') > 0, false)"
                        " ELSE true END", name="session_matches_body"),
    )

    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    private_deletion_id = Column(UUID(as_uuid=True), unique=True)
    auth_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    auth_generation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    auth_domain: Mapped[str | None] = mapped_column(Text)
    auth_decision: Mapped[str | None] = mapped_column(Text)
    auth_reason_code: Mapped[str | None] = mapped_column(Text)
    auth_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(Text)
    record_hash: Mapped[str] = mapped_column(Text)
    line: Mapped[str] = mapped_column(Text)
    tombstone_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # No FKs: the snapshot may lack the session and even its user.
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reset_token_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reset_token_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reset_end_kind: Mapped[str | None] = mapped_column(Text)
    reset_reason_code: Mapped[str | None] = mapped_column(Text)
    reset_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    invitation_end_kind: Mapped[str | None] = mapped_column(Text)
    invitation_reason_code: Mapped[str | None] = mapped_column(Text)
    invitation_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # C3 (0049) typed binding of ``operator_recovery_case``; SQL triggers check the line body independently.
    # Table-only (not mapped): ORM INSERTs never name them, so current code still writes the other
    # kinds into a not-yet-migrated snapshot; case rows are written by SQL (recovery.insert_case_mirror).
    case_id = Column(UUID(as_uuid=True))
    case_operator_id = Column(UUID(as_uuid=True))
    case_user_id = Column(UUID(as_uuid=True))
    case_action = Column(Text)
    case_operation_id = Column(UUID(as_uuid=True))
    case_recorded_at = Column(DateTime(timezone=True))
    __mapper_args__ = {"exclude_properties": [*CASE_FIELDS,'private_deletion_id']}
    applied_via: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    journal_confirmed: Mapped[bool] = mapped_column(Boolean)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
