"""Discovery installation lease; preserve cursor and request pacing on upgrade."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("discovery_request_clock", sa.Column("lease_token", postgresql.UUID(as_uuid=True)))
    op.add_column("discovery_request_clock", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_discovery_request_clock_lease_pair", "discovery_request_clock",
                               "(lease_token IS NULL) = (lease_until IS NULL)")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM discovery_request_clock
        WHERE lease_token IS NOT NULL) THEN RAISE EXCEPTION 'discovery lease exists'; END IF; END $$""")
    op.drop_constraint("ck_discovery_request_clock_lease_pair", "discovery_request_clock", type_="check")
    op.drop_column("discovery_request_clock", "lease_until")
    op.drop_column("discovery_request_clock", "lease_token")
