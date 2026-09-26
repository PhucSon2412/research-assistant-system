"""Bound provider concurrency reservations without releasing uncertain cost."""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("embedding_calls", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("search_usage", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.create_index("ix_embedding_calls_reserved_lease", "embedding_calls", ["lease_until"],
                    postgresql_where=sa.text("status = 'reserved'"))
    op.create_index("ix_search_usage_reserved_lease", "search_usage", ["lease_until"],
                    postgresql_where=sa.text("status = 'reserved'"))
    # Existing in-flight reservations lack a renewable lease. Mark them uncertain
    # on upgrade while retaining both ledgers' reserved USD for reconciliation.
    op.execute("""UPDATE embedding_calls SET status='uncertain',
        cost_status='lease_expired_reserved_ceiling', finished_at=now()
        WHERE status='reserved'""")
    op.execute("""UPDATE search_usage SET status='uncertain',
        cost_status='lease_expired_reserved_ceiling', finished_at=now()
        WHERE status='reserved'""")


def downgrade() -> None:
    op.drop_index("ix_search_usage_reserved_lease", table_name="search_usage")
    op.drop_index("ix_embedding_calls_reserved_lease", table_name="embedding_calls")
    op.drop_column("search_usage", "lease_until")
    op.drop_column("embedding_calls", "lease_until")
