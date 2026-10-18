from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProcessingAcknowledgement(Base):
    __tablename__ = "processing_acknowledgements"
    __table_args__ = (CheckConstraint("length(notice_version) > 0", name="notice_version_not_empty"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="RESTRICT"))
    notice_version: Mapped[str] = mapped_column(Text)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UploadIntent(Base):
    __tablename__ = "upload_intents"
    __table_args__ = (
        CheckConstraint("declared_size > 0", name="declared_size_positive"),
        CheckConstraint("declared_sha256 ~ '^[0-9a-f]{64}$'", name="declared_sha256_hex"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_hex"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_upload_intents_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    acknowledgement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("processing_acknowledgements.id", ondelete="RESTRICT"), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="RESTRICT"))
    notice_version: Mapped[str] = mapped_column(Text)
    file_kind: Mapped[str] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    declared_size: Mapped[int] = mapped_column(BigInteger)
    declared_sha256: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("library_items.id", ondelete="RESTRICT"), unique=True)
