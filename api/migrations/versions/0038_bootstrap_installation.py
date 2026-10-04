"""Require a bound new-install registration/genesis at bootstrap SQL creation."""
from pathlib import Path

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    # Never restore the weaker creation boundary on an installation with history.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM bootstrap_state)
       OR EXISTS (SELECT 1 FROM recovery_registrations)
       OR EXISTS (SELECT 1 FROM revocation_journal_records) OR EXISTS (SELECT 1 FROM auth_change_receipts)
       OR EXISTS (SELECT 1 FROM auth_safety_heads)
       OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq>0 OR backfill_complete) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    # Exact predecessor definition on pristine DBs; keep its owner and ACL.
    previous = Path(__file__).with_name("0037_bootstrap_receipts.sql").read_text(encoding="utf-8")
    start = previous.index("CREATE FUNCTION ras_auth_bootstrap_prepare(")
    end = previous.index("-- Carry the provisioned runtime ACL", start)
    op.execute(previous[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    # The minimal SELECT grant is harmless and is kept; no creation grants change.
