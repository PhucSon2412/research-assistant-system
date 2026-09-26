"""Audit actual response model and upstream provider separately from request routing."""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("embedding_calls", "search_usage"):
        op.add_column(table, sa.Column("response_model_id", sa.Text()))
        op.add_column(table, sa.Column("response_provider", sa.Text()))


def downgrade() -> None:
    for table in ("search_usage", "embedding_calls"):
        op.drop_column(table, "response_provider")
        op.drop_column(table, "response_model_id")
