"""Separate M2a extraction handoff from terminal job completion.

Old M2a workers wrote finished_at on awaiting_index. Preserve that instant
as extraction_finished_at and leave the job unfinished for M3 indexing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ingestion_jobs", sa.Column("extraction_finished_at", sa.DateTime(timezone=True)))
    op.execute(sa.text("""UPDATE ingestion_jobs
        SET extraction_finished_at = finished_at, finished_at = NULL
        WHERE state = 'awaiting_index' AND finished_at IS NOT NULL"""))
    op.create_check_constraint("ck_ingestion_jobs_awaiting_index_unfinished", "ingestion_jobs",
                               "state <> 'awaiting_index' OR finished_at IS NULL")


def downgrade() -> None:
    op.drop_constraint("ck_ingestion_jobs_awaiting_index_unfinished", "ingestion_jobs", type_="check")
    op.execute(sa.text("""UPDATE ingestion_jobs SET finished_at = extraction_finished_at
        WHERE state = 'awaiting_index' AND finished_at IS NULL"""))
    op.drop_column("ingestion_jobs", "extraction_finished_at")
