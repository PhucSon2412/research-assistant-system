"""Bind document reservations to a durable attempt, including after content cleanup.

Nullable for historical calls; workers never adopt a historical reserved call.
No new content table or additional runtime privilege is introduced.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('embedding_calls', sa.Column('reservation_token', UUID(as_uuid=True)))
    op.add_column('embedding_calls', sa.Column('reservation_job_id', UUID(as_uuid=True),
        sa.ForeignKey('ingestion_jobs.id', ondelete='RESTRICT')))
    op.add_column('embedding_calls', sa.Column('reservation_job_token', UUID(as_uuid=True)))
    op.create_check_constraint('ck_embedding_calls_reservation_binding', 'embedding_calls',
        '(reservation_token IS NULL AND reservation_job_id IS NULL AND reservation_job_token IS NULL) OR '
        '(reservation_token IS NOT NULL AND reservation_job_id IS NOT NULL AND reservation_job_token IS NOT NULL)')


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM embedding_calls WHERE reservation_token IS NOT NULL) THEN
            RAISE EXCEPTION 'document reservation bindings exist; downgrade would lose attempt fencing';
        END IF;
    END $$""")
    op.drop_constraint('ck_embedding_calls_reservation_binding', 'embedding_calls', type_='check')
    for name in ('reservation_job_token', 'reservation_job_id', 'reservation_token'):
        op.drop_column('embedding_calls', name)
