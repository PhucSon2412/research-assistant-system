"""Prepare shared Section/Chunk locator slots for M2b PDF pages.

Backfills M2a Markdown rows already produced on a development database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCATOR_CHECK = (
    "(locator_kind = 'markdown' AND start_offset IS NOT NULL AND end_offset IS NOT NULL"
    " AND start_line IS NOT NULL AND end_line IS NOT NULL AND start_offset >= 0 AND end_offset > start_offset"
    " AND start_line > 0 AND end_line >= start_line AND start_page IS NULL AND end_page IS NULL)"
    " OR (locator_kind = 'pdf' AND start_page IS NOT NULL AND end_page IS NOT NULL"
    " AND start_page > 0 AND end_page >= start_page"
    " AND start_offset IS NULL AND end_offset IS NULL AND start_line IS NULL AND end_line IS NULL)"
)
METADATA_CHECK = (
    "(locator_kind = 'markdown' AND start_offset IS NOT NULL AND end_offset IS NOT NULL"
    " AND start_offset >= 0 AND end_offset > start_offset AND start_page IS NULL)"
    " OR (locator_kind = 'pdf' AND start_page IS NOT NULL AND start_page > 0"
    " AND start_offset IS NULL AND end_offset IS NULL)"
)


def upgrade() -> None:
    for table in ("metadata_candidates", "sections", "chunks"):
        op.add_column(table, sa.Column("locator_kind", sa.Text(), nullable=True))
        op.execute(f"UPDATE {table} SET locator_kind = 'markdown'")
        op.alter_column(table, "locator_kind", existing_type=sa.Text(), nullable=False)
        op.add_column(table, sa.Column("start_page", sa.Integer()))
        if table != "metadata_candidates":
            op.add_column(table, sa.Column("end_page", sa.Integer()))
            op.add_column(table, sa.Column("locator_detail", JSONB()))
        op.drop_constraint(op.f(f"ck_{table}_offsets_valid"), table, type_="check")
        if table != "metadata_candidates":
            op.drop_constraint(op.f(f"ck_{table}_lines_valid"), table, type_="check")
        for name in (("start_offset", "end_offset") if table == "metadata_candidates" else
                     ("start_offset", "end_offset", "start_line", "end_line")):
            op.alter_column(table, name, existing_type=sa.Integer(), nullable=True)
        op.create_check_constraint(op.f(f"ck_{table}_locator_kind_valid"), table,
                                   "locator_kind IN ('markdown', 'pdf')")
        op.create_check_constraint(op.f(f"ck_{table}_locator_valid"), table,
                                   METADATA_CHECK if table == "metadata_candidates" else LOCATOR_CHECK)


def downgrade() -> None:
    for table in ("chunks", "sections", "metadata_candidates"):
        # Downgrade is safe for M2a data; PDF rows from a later milestone need
        # explicit archival/migration before removing page locators.
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table} WHERE locator_kind = 'pdf')"
                   " THEN RAISE EXCEPTION 'PDF locators must be migrated before downgrade'; END IF; END $$")
        op.drop_constraint(op.f(f"ck_{table}_locator_valid"), table, type_="check")
        op.drop_constraint(op.f(f"ck_{table}_locator_kind_valid"), table, type_="check")
        for name in (("start_offset", "end_offset") if table == "metadata_candidates" else
                     ("start_offset", "end_offset", "start_line", "end_line")):
            op.alter_column(table, name, existing_type=sa.Integer(), nullable=False)
        op.create_check_constraint(op.f(f"ck_{table}_offsets_valid"), table,
                                   "start_offset >= 0 AND end_offset > start_offset")
        if table != "metadata_candidates":
            op.create_check_constraint(op.f(f"ck_{table}_lines_valid"), table,
                                       "start_line > 0 AND end_line >= start_line")
            op.drop_column(table, "locator_detail")
            op.drop_column(table, "end_page")
        op.drop_column(table, "start_page")
        op.drop_column(table, "locator_kind")
