"""Page text/coverage and bibliography entries for private extraction."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity(name: str):
    return (
        sa.ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                                name=f"fk_{name}_version_item", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_file_id", "version_id"], ["source_files.id", "source_files.version_id"],
                                name=f"fk_{name}_file_version", ondelete="RESTRICT"),
    )


def upgrade() -> None:
    op.create_table("pdf_pages",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("body_chars", sa.Integer(), nullable=False),
        sa.Column("body_page", sa.Boolean(), nullable=False),
        sa.Column("qualifies", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("layout_mode", sa.Text(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        *_identity("pdf_page"),
        sa.CheckConstraint("page_number > 0 AND body_chars >= 0", name=op.f("ck_pdf_pages_page_and_chars_valid")),
        sa.UniqueConstraint("source_file_id", "pipeline_version", "page_number", name="uq_pdf_page_number"))
    op.create_index("ix_pdf_pages_item_id", "pdf_pages", ["item_id"])
    op.create_table("reference_entries",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("raw_citation", sa.Text(), nullable=False),
        sa.Column("fields", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("locator_kind", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        *_identity("reference"),
        sa.CheckConstraint("ordinal >= 0 AND start_offset >= 0 AND end_offset > start_offset",
                           name=op.f("ck_reference_entries_span_valid")),
        sa.CheckConstraint("(locator_kind = 'markdown' AND page_number IS NULL) OR "
                           "(locator_kind = 'pdf' AND page_number > 0)",
                           name=op.f("ck_reference_entries_locator_valid")),
        sa.UniqueConstraint("source_file_id", "pipeline_version", "ordinal", name="uq_reference_ordinal"))
    op.create_index("ix_reference_entries_item_id", "reference_entries", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_reference_entries_item_id", table_name="reference_entries")
    op.drop_table("reference_entries")
    op.drop_index("ix_pdf_pages_item_id", table_name="pdf_pages")
    op.drop_table("pdf_pages")
