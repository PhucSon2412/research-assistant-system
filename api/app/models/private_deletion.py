"""Permanent private deletion decisions, independent of target/FK lifetime."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class PrivateItemDeletion(Base):
    __tablename__ = 'private_item_deletions'
    __table_args__ = (
        CheckConstraint('rights_epoch > 0', name='epoch_positive'),
        CheckConstraint('purge_due_at >= deleted_at', name='deadline_valid'),
        CheckConstraint('cleanup_attempts >= 0', name='attempts_non_negative'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text('gen_random_uuid()'))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    rights_epoch: Mapped[int] = mapped_column(BigInteger)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purge_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    manifest: Mapped[dict] = mapped_column(JSONB)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_attempts: Mapped[int] = mapped_column(Integer, server_default=text('0'))
    last_cleanup_error: Mapped[str | None] = mapped_column(Text)


class PrivateObjectFence(Base):
    __tablename__ = 'private_object_fences'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text('gen_random_uuid()'))
    deletion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
