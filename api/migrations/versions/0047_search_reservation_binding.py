"""Nonce-bound query/rerank attempts; historical generation stays separate."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('reservation_token', 'operation_id', 'session_id'):
        op.add_column('search_usage', sa.Column(name, UUID(as_uuid=True)))
    op.create_check_constraint('ck_search_usage_reservation_binding', 'search_usage',
        '(reservation_token IS NULL AND operation_id IS NULL AND session_id IS NULL) OR '
        "(reservation_token IS NOT NULL AND operation_id IS NOT NULL AND session_id IS NOT NULL "
        "AND task_type IN ('embed_query','rerank'))")
    op.create_unique_constraint('uq_search_usage_reservation_token', 'search_usage', ['reservation_token'])


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM search_usage WHERE reservation_token IS NOT NULL) THEN
            RAISE EXCEPTION 'search reservation bindings exist';
        END IF;
    END $$""")
    op.drop_constraint('uq_search_usage_reservation_token', 'search_usage', type_='unique')
    op.drop_constraint('ck_search_usage_reservation_binding', 'search_usage', type_='check')
    for name in ('session_id', 'operation_id', 'reservation_token'):
        op.drop_column('search_usage', name)
