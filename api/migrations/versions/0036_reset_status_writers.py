"""C2b reset/status predicates and restricted token writers; cutover stays pending."""
from pathlib import Path
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM recovery_registrations)
       OR EXISTS (SELECT 1 FROM revocation_journal_records) OR EXISTS (SELECT 1 FROM auth_change_receipts)
       OR EXISTS (SELECT 1 FROM auth_safety_heads)
       OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq>0 OR backfill_complete) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    op.execute(Path(__file__).with_name("0034_reset_token_status_fence.sql").read_text(encoding="utf-8"))
    for signature in ("ras_auth_token_close(uuid,uuid,text,text,timestamptz)",
                      "ras_auth_token_lock(uuid,uuid[])", "ras_auth_reset_token_live(uuid,uuid,timestamptz)",
                      "ras_auth_reset_eligible(uuid,uuid,text)", "ras_auth_status_live(uuid)"):
        op.execute(f"DROP FUNCTION {signature}")
