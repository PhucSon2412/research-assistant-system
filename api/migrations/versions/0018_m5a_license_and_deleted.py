"""Version-bound license provenance and persistent OAI deletion markers."""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("discovery_paper_licenses",
        sa.Column("paper_id", sa.UUID(), sa.ForeignKey("discovery_papers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version_label", sa.Text(), primary_key=True),
        sa.Column("license_url", sa.Text(), nullable=False),
        sa.Column("oai_datestamp", sa.Date(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("discovery_deleted_records",
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("remote_id", sa.Text(), primary_key=True),
        sa.Column("oai_datestamp", sa.Date(), nullable=False))
    # Preserve already captured version-bound URLs from 0017. URLs filtered by
    # the old host restriction require a bounded reharvest of the same window.
    op.execute("""INSERT INTO discovery_paper_licenses
        (paper_id, version_label, license_url, oai_datestamp, first_observed_at, last_observed_at)
        SELECT id, latest_version, license_url, oai_datestamp, first_discovered_at, last_seen_at
        FROM discovery_papers WHERE latest_version IS NOT NULL AND license_url IS NOT NULL""")


def downgrade() -> None:
    op.drop_table("discovery_deleted_records")
    op.drop_table("discovery_paper_licenses")
