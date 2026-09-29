"""Durable, isolated arXiv metadata discovery."""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("discovery_sources",
        sa.Column("topic", sa.Text(), primary_key=True),
        sa.Column("window_from", sa.Date(), nullable=False),
        sa.Column("window_until", sa.Date()),
        sa.Column("resumption_token", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("pages_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("state IN ('idle', 'running', 'error')", name="ck_discovery_sources_state_valid"),
        sa.CheckConstraint("consecutive_errors >= 0", name="ck_discovery_sources_errors_non_negative"))
    op.create_table("discovery_papers",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", sa.Text(), nullable=False, server_default="arxiv"),
        sa.Column("remote_id", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authors", sa.Text()), sa.Column("abstract", sa.Text()),
        sa.Column("categories", sa.Text()), sa.Column("license_url", sa.Text()),
        sa.Column("latest_version", sa.Text()),
        sa.Column("source_published_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("oai_datestamp", sa.Date(), nullable=False),
        sa.Column("first_discovered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("content_status", sa.Text(), nullable=False, server_default="metadata_only"),
        sa.UniqueConstraint("source", "remote_id", name="uq_discovery_papers_source_remote_id"),
        sa.CheckConstraint("content_status = 'metadata_only'", name="ck_discovery_papers_metadata_only"))
    op.create_index("ix_discovery_papers_first_discovered_at", "discovery_papers", ["first_discovered_at"])
    op.create_table("discovery_paper_topics",
        sa.Column("paper_id", sa.UUID(), sa.ForeignKey("discovery_papers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("topic", sa.Text(), sa.ForeignKey("discovery_sources.topic", ondelete="RESTRICT"), primary_key=True))
    op.create_table("discovery_request_clock",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("next_request_at", sa.DateTime(timezone=True)))
    op.execute("INSERT INTO discovery_request_clock (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("discovery_request_clock")
    op.drop_table("discovery_paper_topics")
    op.drop_index("ix_discovery_papers_first_discovered_at", "discovery_papers")
    op.drop_table("discovery_papers")
    op.drop_table("discovery_sources")
