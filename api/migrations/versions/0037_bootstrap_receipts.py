"""C2c-1: runtime bootstrap with atomic initial receipts; safety stays pending."""
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade():
    # NULL is legacy evidence, never backfilled into a proven bootstrap.
    op.add_column("bootstrap_state", sa.Column("creation_transaction_id", sa.BigInteger()))
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM bootstrap_state)
       OR EXISTS (SELECT 1 FROM recovery_registrations)
       OR EXISTS (SELECT 1 FROM revocation_journal_records) OR EXISTS (SELECT 1 FROM auth_change_receipts)
       OR EXISTS (SELECT 1 FROM auth_safety_heads)
       OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq>0 OR backfill_complete) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    op.execute("DROP FUNCTION ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)")
    op.execute("DROP TRIGGER trg_auth_bootstrap_pair ON bootstrap_state")
    op.execute("DROP TRIGGER trg_auth_bootstrap_final ON bootstrap_state")
    op.execute("DROP FUNCTION ras_auth_bootstrap_pair()")
    op.execute("DROP FUNCTION ras_auth_bootstrap_final()")
    op.drop_column("bootstrap_state", "creation_transaction_id")
