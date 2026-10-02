from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

JOURNAL_KINDS = "'genesis', 'revocation', 'recovery_begin', 'recovery_end', 'review_closure'"
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
        # 0029: the row is filed under the request its own line names (checked at insert, before the append).
        CheckConstraint("CASE WHEN kind = 'review_closure' AND review_request_id IS NOT NULL"
                        " THEN review_request_id::text = (line::jsonb #>> '{body,request_id}') ELSE true END",
                        name="closure_request_matches_body"),
        Index("ix_revocation_journal_records_unconfirmed", "seq", postgresql_where=text("NOT journal_confirmed")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(Text)
    record_hash: Mapped[str] = mapped_column(Text)
    line: Mapped[str] = mapped_column(Text)
    tombstone_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    applied_via: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    journal_confirmed: Mapped[bool] = mapped_column(Boolean)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
