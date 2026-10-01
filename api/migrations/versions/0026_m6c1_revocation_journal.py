"""Revocation journal registration and mirror (M6c-1).

- ``recovery_registrations`` (one row) names the journal file the database
  belongs to: its ID and genesis hash. It is written by ``app.cli.recovery``
  (``init`` for a new install, ``register-existing`` for a live database,
  ``recover`` when it adopts an older snapshot), never by this migration: the
  migration cannot see the journal, and a database that has just been
  restored must not be taken for a new install.
- ``revocation_journal_records`` holds the database's copy of every journal
  record (seq, hash, exact line) and how it arrived. The recovery gate opens
  only while this copy and the file agree. Append-only.
- A tombstone cannot be committed without the journal record naming it
  (deferred constraint trigger), so no revocation reaches the database
  without its journal line. Tombstones that already exist are imported by
  ``register-existing``.

Downgrade is refused once the database is registered.
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("recovery_registrations",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("journal_id", sa.UUID(), nullable=False),
        sa.Column("genesis_hash", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_recovery_registrations_singleton")),
        sa.CheckConstraint("genesis_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_recovery_registrations_genesis_hash_hex")),
        sa.CheckConstraint("mode IN ('new_install', 'existing_db', 'adopted', 'rebuilt_from_db')",
                           name=op.f("ck_recovery_registrations_mode_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recovery_registrations")))
    op.create_table("revocation_journal_records",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("tombstone_id", sa.UUID(), nullable=True),
        sa.Column("applied_via", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("seq > 0", name=op.f("ck_revocation_journal_records_seq_positive")),
        sa.CheckConstraint("kind IN ('genesis', 'revocation', 'recovery_begin', 'recovery_end')",
                           name=op.f("ck_revocation_journal_records_kind_valid")),
        sa.CheckConstraint("record_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_revocation_journal_records_record_hash_hex")),
        sa.CheckConstraint("length(line) BETWEEN 2 AND 65535", name=op.f("ck_revocation_journal_records_line_length")),
        sa.CheckConstraint("applied_via IN ('genesis', 'revoke', 'replay', 'import')",
                           name=op.f("ck_revocation_journal_records_applied_via_valid")),
        sa.CheckConstraint("result ~ '^[a-z_]{1,64}$'", name=op.f("ck_revocation_journal_records_result_is_code")),
        sa.CheckConstraint("kind = 'revocation' OR tombstone_id IS NULL",
                           name=op.f("ck_revocation_journal_records_tombstone_only_for_revocation")),
        sa.CheckConstraint("kind <> 'revocation' OR tombstone_id IS NOT NULL OR result = 'superseded'",
                           name=op.f("ck_revocation_journal_records_revocation_names_tombstone")),
        sa.CheckConstraint("(kind = 'genesis') = (seq = 1)", name=op.f("ck_revocation_journal_records_genesis_first")),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_revocation_journal_records")),
        sa.UniqueConstraint("record_hash", name="uq_revocation_journal_records_record_hash"),
        sa.UniqueConstraint("tombstone_id", name="uq_revocation_journal_records_tombstone_id"))
    op.execute("""
        CREATE FUNCTION ras_recovery_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'check_violation';
        END $$;
        CREATE TRIGGER trg_recovery_registrations_append_only BEFORE UPDATE OR DELETE ON recovery_registrations
            FOR EACH ROW EXECUTE FUNCTION ras_recovery_append_only();
        CREATE TRIGGER trg_recovery_registrations_no_truncate BEFORE TRUNCATE ON recovery_registrations
            FOR EACH STATEMENT EXECUTE FUNCTION ras_recovery_append_only();
        CREATE TRIGGER trg_revocation_journal_records_append_only BEFORE UPDATE OR DELETE ON revocation_journal_records
            FOR EACH ROW EXECUTE FUNCTION ras_recovery_append_only();
        CREATE TRIGGER trg_revocation_journal_records_no_truncate BEFORE TRUNCATE ON revocation_journal_records
            FOR EACH STATEMENT EXECUTE FUNCTION ras_recovery_append_only();

        -- Named to fire after trg_deletion_tombstones_target_revoked on the same row.
        CREATE FUNCTION ras_deletion_tombstones_tracked_in_journal() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM revocation_journal_records WHERE tombstone_id = NEW.id) THEN
                RAISE EXCEPTION 'a tombstone needs its revocation journal record' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_deletion_tombstones_tracked_in_journal
            AFTER INSERT ON deletion_tombstones DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_deletion_tombstones_tracked_in_journal();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
                RAISE EXCEPTION 'the database is registered with a revocation journal; downgrade below 0026 would drop its recovery gate';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_deletion_tombstones_tracked_in_journal ON deletion_tombstones")
    op.execute("DROP FUNCTION ras_deletion_tombstones_tracked_in_journal()")
    op.drop_table("revocation_journal_records")
    op.drop_table("recovery_registrations")
    op.execute("DROP FUNCTION ras_recovery_append_only()")
