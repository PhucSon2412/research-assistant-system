from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EXACT_STATUSES = ("linked", "no_discovery_record", "discovery_record_deleted")
UNLINKED_STATUSES = ("no_arxiv_id", "bare_identifier", "invalid_arxiv_id",
                     "multiple_arxiv_ids", "conflicting_versions")
ID_FORMS = ("arxiv_prefix", "arxiv_url", "arxiv_doi", "abs_path")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class ReferenceCitationLink(Base):
    """One derived row per extracted reference: "citing paper lists cited work".

    Private like its reference. It is not evidence and never enters an index.
    ``raw_citation_sha256`` + ``matcher_version`` fence stale rows at read time.
    """
    __tablename__ = "reference_citation_links"
    __table_args__ = (
        CheckConstraint(f"match_status IN ({_in(EXACT_STATUSES + UNLINKED_STATUSES)})", name="status_valid"),
        CheckConstraint(f"(match_status IN ({_in(EXACT_STATUSES)})) = "
                        "(arxiv_id IS NOT NULL AND match_method IS NOT NULL AND id_form IS NOT NULL"
                        " AND id_start_offset IS NOT NULL AND id_end_offset IS NOT NULL)", name="exact_fields"),
        CheckConstraint("(match_status = 'linked') = (discovery_paper_id IS NOT NULL)", name="target_iff_linked"),
        CheckConstraint("match_method IS NULL OR match_method = 'arxiv_id_exact'", name="method_valid"),
        CheckConstraint(f"id_form IS NULL OR id_form IN ({_in(ID_FORMS)})", name="id_form_valid"),
        CheckConstraint("cited_version IS NULL OR (arxiv_id IS NOT NULL AND cited_version ~ '^v[1-9][0-9]*$')",
                        name="cited_version_valid"),
        CheckConstraint("id_start_offset IS NULL OR (id_start_offset >= 0 AND id_end_offset > id_start_offset)",
                        name="id_span_valid"),
        CheckConstraint("raw_citation_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_valid"),
        Index("ix_reference_citation_links_discovery_paper_id", "discovery_paper_id"),
        Index("ix_reference_citation_links_arxiv_id", "arxiv_id"),
    )
    reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reference_entries.id", ondelete="RESTRICT"), primary_key=True)
    matcher_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_citation_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    match_status: Mapped[str] = mapped_column(Text, nullable=False)
    match_method: Mapped[str | None] = mapped_column(Text)
    id_form: Mapped[str | None] = mapped_column(Text)
    arxiv_id: Mapped[str | None] = mapped_column(Text)
    cited_version: Mapped[str | None] = mapped_column(Text)
    id_start_offset: Mapped[int | None] = mapped_column(Integer)
    id_end_offset: Mapped[int | None] = mapped_column(Integer)
    discovery_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_papers.id", ondelete="RESTRICT"))
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
