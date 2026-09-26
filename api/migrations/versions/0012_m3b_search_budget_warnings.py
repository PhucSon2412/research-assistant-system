"""Persist warnings for search reservations without rewriting applied 0011."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("search_usage", sa.Column("warnings", JSONB(), nullable=False,
                                            server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("search_usage", "warnings")
