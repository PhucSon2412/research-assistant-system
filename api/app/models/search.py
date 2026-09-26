"""Durable search request limits and external-call accounting."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SearchRequestDay(Base):
    __tablename__ = "search_request_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, server_default="0")


class SearchUsage(Base):
    __tablename__ = "search_usage"
    __table_args__ = (
        CheckConstraint("task_type IN ('embed_query', 'rerank')", name="task_type_valid"),
        CheckConstraint("status IN ('reserved', 'completed', 'uncertain', 'rejected')", name="status_valid"),
        CheckConstraint("reserved_usd >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)", name="cost_non_negative"),
        Index("ix_search_usage_day_user", "day", "user_id"),
        Index("ix_search_usage_reserved_lease", "lease_until",
              postgresql_where=text("status = 'reserved'")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    day: Mapped[date] = mapped_column(Date)
    task_type: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    response_model_id: Mapped[str | None] = mapped_column(Text)
    response_provider: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    search_units: Mapped[int | None] = mapped_column(Integer)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    cost_status: Mapped[str] = mapped_column(Text)
    warnings: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
