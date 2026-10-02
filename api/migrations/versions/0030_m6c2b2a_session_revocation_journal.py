"""Keep session revocations across a restore (M6c-2b2a).

No file I/O in a migration. Legacy revoked sessions keep the recovery gate
closed until recover journals them. Missing history cannot be reconstructed.
hold still reads only seq/hash, so it works before this migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

KINDS = "'genesis', 'revocation', 'recovery_begin', 'recovery_end', 'review_closure'"
REASONS = "'logout', 'replaced', 'user_revoked', 'password_reset', 'user_disabled'"
IDS_REQUIRED = ("CASE WHEN kind = 'session_revocation' THEN session_id IS NOT NULL"
                " AND session_user_id IS NOT NULL ELSE session_id IS NULL AND session_user_id IS NULL END")
MATCHES_BODY = ("CASE WHEN kind = 'session_revocation' THEN"
                " (session_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,session_id}'))"
                " AND (session_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}'))"
                " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
                f" AND COALESCE((line::jsonb #>> '{{body,reason_code}}') IN ({REASONS}), false)"
                " AND COALESCE(length(line::jsonb #>> '{body,revoked_at}') > 0, false)"
                " ELSE true END")
CONFIRM_ONLY = """
    CREATE OR REPLACE FUNCTION ras_revocation_journal_records_confirm_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND NOT OLD.journal_confirmed AND NEW.journal_confirmed
           AND (to_jsonb(NEW) - 'journal_confirmed') = (to_jsonb(OLD) - 'journal_confirmed') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'revocation_journal_records is append-only (only journal_confirmed false -> true)'
            USING ERRCODE = 'check_violation';
    END $$;
"""


def upgrade() -> None:
    for name in ("session_id", "session_user_id"):
        op.add_column("revocation_journal_records", sa.Column(name, sa.UUID(), nullable=True))
    op.create_unique_constraint("uq_revocation_journal_records_session_id", "revocation_journal_records",
                                ["session_id"])
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
                               f"kind IN ({KINDS}, 'session_revocation')")
    for name, condition in (("session_ids_required", IDS_REQUIRED), ("session_matches_body", MATCHES_BODY)):
        op.create_check_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", condition)
    op.execute(CONFIRM_ONLY)
    op.execute("""
        -- INSERT check, before append: refuse a line filed under another session or its wrong user.
        -- This does not acquire row locks: all writers already hold users -> sessions -> journal.
        CREATE FUNCTION ras_session_journal_target() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind = 'session_revocation' AND EXISTS
                (SELECT 1 FROM sessions s WHERE s.id = NEW.session_id AND s.user_id <> NEW.session_user_id) THEN
                RAISE EXCEPTION 'session journal names another user' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_session_journal_target BEFORE INSERT ON revocation_journal_records
            FOR EACH ROW EXECUTE FUNCTION ras_session_journal_target();

        CREATE FUNCTION ras_sessions_final() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.id, NEW.user_id, NEW.token_hash, NEW.created_at)
                    IS DISTINCT FROM ROW(OLD.id, OLD.user_id, OLD.token_hash, OLD.created_at)
                    OR (OLD.revoked_at IS NOT NULL AND
                        ROW(NEW.revoked_at, NEW.revoked_reason) IS DISTINCT FROM ROW(OLD.revoked_at, OLD.revoked_reason)) THEN
                    RAISE EXCEPTION 'session identity and revocation are final' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.session_id = NEW.id
                       AND (NEW.revoked_at IS NULL OR NEW.user_id <> j.session_user_id)) THEN
                RAISE EXCEPTION 'a journaled session cannot be reopened or reassigned' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_sessions_final BEFORE INSERT OR UPDATE ON sessions
            FOR EACH ROW EXECUTE FUNCTION ras_sessions_final();

        CREATE FUNCTION ras_sessions_revocation_journaled() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.revoked_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM revocation_journal_records j
                WHERE j.kind = 'session_revocation' AND j.session_id = NEW.id AND j.session_user_id = NEW.user_id
                  AND (j.line::jsonb #>> '{body,session_id}') = NEW.id::text
                  AND (j.line::jsonb #>> '{body,user_id}') = NEW.user_id::text
                  AND (j.line::jsonb #>> '{body,reason_code}') = NEW.revoked_reason
                  AND (j.line::jsonb #>> '{body,revoked_at}')::timestamptz = NEW.revoked_at) THEN
                RAISE EXCEPTION 'revoking a session needs its journal record' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_sessions_revocation_journaled
            AFTER INSERT OR UPDATE ON sessions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_sessions_revocation_journaled();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
                RAISE EXCEPTION 'the database is registered with a revocation journal; downgrade would reopen restored sessions';
            END IF;
        END $$;
        DROP TRIGGER trg_sessions_revocation_journaled ON sessions;
        DROP FUNCTION ras_sessions_revocation_journaled();
        DROP TRIGGER trg_sessions_final ON sessions;
        DROP FUNCTION ras_sessions_final();
        DROP TRIGGER trg_session_journal_target ON revocation_journal_records;
        DROP FUNCTION ras_session_journal_target();
    """)
    for name in ("session_ids_required", "session_matches_body"):
        op.drop_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", type_="check")
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
                               f"kind IN ({KINDS})")
    op.drop_constraint("uq_revocation_journal_records_session_id", "revocation_journal_records", type_="unique")
    for name in ("session_id", "session_user_id"):
        op.drop_column("revocation_journal_records", name)
    # Confirm-only compares all columns generically and remains valid after dropping the two columns.
