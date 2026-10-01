"""Record whether each journal line was confirmed durable (M6c-1, review of PR #26).

A revocation whose journal write failed after a *complete* line (``fsync``
raised) used to commit with a mirror row identical to the file, so the
recovery gate saw agreement and opened on a line nobody had confirmed
durable. ``revocation_journal_records.journal_confirmed`` now carries that
fact: ``true`` only once the line was written and ``fsync`` returned. Any
``false`` row keeps the gate closed until ``recover`` syncs the file itself
and confirms the row.

- Backfill, fail-closed: a ``revoke`` row written by 0026 code may be exactly
  that case and cannot be told apart afterwards, so it starts ``false`` (a
  database with revocations keeps its gate closed after this upgrade until
  ``recover`` syncs the file and confirms them; no ``--confirm-db-is-current``
  needed when the line is in the file). Rows written by ``init``,
  ``register-existing`` or a replay were mirrored only after the file write
  and its ``fsync`` returned, and start ``true``. The column then has no
  default, so every writer must state it.
- The append-only trigger now allows exactly one update: ``journal_confirmed``
  from ``false`` to ``true`` with every other column unchanged.

Not folded into 0026: that revision has already run on the dev database of
this branch. Downgrade is refused while a row is unconfirmed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("revocation_journal_records",
                  sa.Column("journal_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.alter_column("revocation_journal_records", "journal_confirmed", server_default=None)
    op.create_index("ix_revocation_journal_records_unconfirmed", "revocation_journal_records", ["seq"],
                    postgresql_where=sa.text("NOT journal_confirmed"))
    op.execute("""
        DROP TRIGGER trg_revocation_journal_records_append_only ON revocation_journal_records;
        CREATE FUNCTION ras_revocation_journal_records_confirm_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND NOT OLD.journal_confirmed AND NEW.journal_confirmed
               AND ROW(NEW.seq, NEW.kind, NEW.record_hash, NEW.line, NEW.tombstone_id, NEW.applied_via, NEW.result,
                       NEW.recorded_at)
                   IS NOT DISTINCT FROM ROW(OLD.seq, OLD.kind, OLD.record_hash, OLD.line, OLD.tombstone_id,
                                            OLD.applied_via, OLD.result, OLD.recorded_at) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'revocation_journal_records is append-only (only journal_confirmed false -> true)'
                USING ERRCODE = 'check_violation';
        END $$;
        CREATE TRIGGER trg_revocation_journal_records_append_only BEFORE UPDATE OR DELETE ON revocation_journal_records
            FOR EACH ROW EXECUTE FUNCTION ras_revocation_journal_records_confirm_only();
        -- Backfill (fail-closed): only rows mirrored after a returned fsync are confirmed.
        UPDATE revocation_journal_records SET journal_confirmed = true WHERE applied_via <> 'revoke';
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM revocation_journal_records WHERE NOT journal_confirmed) THEN
                RAISE EXCEPTION 'a revocation journal record is not confirmed durable; downgrade below 0027 would open the gate on it';
            END IF;
        END $$;
        DROP TRIGGER trg_revocation_journal_records_append_only ON revocation_journal_records;
        DROP FUNCTION ras_revocation_journal_records_confirm_only();
        CREATE TRIGGER trg_revocation_journal_records_append_only BEFORE UPDATE OR DELETE ON revocation_journal_records
            FOR EACH ROW EXECUTE FUNCTION ras_recovery_append_only();
    """)
    op.drop_index("ix_revocation_journal_records_unconfirmed", table_name="revocation_journal_records")
    op.drop_column("revocation_journal_records", "journal_confirmed")
