"""Keep reset token endings across restore; no journal I/O in migration."""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

KINDS = "'genesis', 'revocation', 'recovery_begin', 'recovery_end', 'review_closure', 'session_revocation'"
TOKEN_IDS_REQUIRED = ("CASE WHEN kind = 'reset_token_closure' THEN reset_token_id IS NOT NULL"
                      " AND reset_token_user_id IS NOT NULL AND reset_end_kind IS NOT NULL"
                      " AND reset_reason_code IS NOT NULL AND reset_ended_at IS NOT NULL"
                      " ELSE reset_token_id IS NULL AND reset_token_user_id IS NULL AND reset_end_kind IS NULL"
                      " AND reset_reason_code IS NULL AND reset_ended_at IS NULL END")
TOKEN_MATCHES_BODY = ("CASE WHEN kind = 'reset_token_closure' THEN"
    " (reset_token_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,token_id}'))"
    " AND (reset_token_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}'))"
    " AND (reset_end_kind IS NOT DISTINCT FROM (line::jsonb #>> '{body,end_kind}'))"
    " AND (reset_reason_code IS NOT DISTINCT FROM (line::jsonb #>> '{body,reason_code}'))"
    " AND (reset_ended_at IS NOT DISTINCT FROM (line::jsonb #>> '{body,ended_at}')::timestamptz)"
    " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
    " AND COALESCE((reset_end_kind = 'consumed' AND reset_reason_code IN ('password_reset', 'legacy_consumed'))"
    " OR (reset_end_kind = 'revoked' AND reset_reason_code IN"
    " ('reset_replaced', 'reset_completed', 'user_disabled', 'legacy_revoked')), false) ELSE true END")
REASON_VALID = ("closure_reason IS NULL OR (used_at IS NOT NULL AND closure_reason IN"
               " ('password_reset', 'legacy_consumed')) OR (revoked_at IS NOT NULL AND closure_reason IN"
               " ('reset_replaced', 'reset_completed', 'user_disabled', 'legacy_revoked'))")


def upgrade() -> None:
    op.add_column("password_reset_tokens", sa.Column("closure_reason", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_password_reset_tokens_closure_reason_valid"),
                               "password_reset_tokens", REASON_VALID)
    for name, type_ in (("reset_token_id", sa.UUID()), ("reset_token_user_id", sa.UUID()),
                        ("reset_end_kind", sa.Text()), ("reset_reason_code", sa.Text()),
                        ("reset_ended_at", sa.DateTime(timezone=True))):
        op.add_column("revocation_journal_records", sa.Column(name, type_, nullable=True))
    op.create_unique_constraint("uq_revocation_journal_records_reset_token_id", "revocation_journal_records",
                                ["reset_token_id"])
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
                               f"kind IN ({KINDS}, 'reset_token_closure')")
    for name, expr in (("reset_fields_required", TOKEN_IDS_REQUIRED), ("reset_matches_body", TOKEN_MATCHES_BODY)):
        op.create_check_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", expr)
    op.execute("""
        CREATE FUNCTION ras_reset_journal_target() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE b jsonb; stamp timestamptz;
        BEGIN
            IF NEW.kind <> 'reset_token_closure' THEN RETURN NEW; END IF;
            b := NEW.line::jsonb -> 'body';
            -- Independent SQL validation; CHECK must never pass via NULL or JSON null.
            IF jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'invalid reset token journal body' USING ERRCODE = 'check_violation';
            END IF;
            IF (SELECT count(*) FROM jsonb_object_keys(b)) <> 5
               OR EXISTS (SELECT 1 FROM jsonb_object_keys(NEW.line::jsonb) k
                          WHERE k NOT IN ('body','hash','kind','prev','seq'))
               OR NOT b ?& ARRAY['token_id','user_id','end_kind','reason_code','ended_at']
               OR EXISTS (SELECT 1 FROM jsonb_each(b) kv WHERE jsonb_typeof(kv.value) <> 'string')
               OR COALESCE(b->>'token_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false) = false
               OR COALESCE(b->>'user_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false) = false
               OR COALESCE(b->>'ended_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$', false) = false THEN
                RAISE EXCEPTION 'invalid reset token journal fields' USING ERRCODE = 'check_violation';
            END IF;
            stamp := (b->>'ended_at')::timestamptz;
            IF substring(b->>'ended_at',12,2)::int > 23 OR substring(b->>'ended_at',15,2)::int > 59
               OR substring(b->>'ended_at',18,2)::int > 59 OR NOT isfinite(stamp)
               OR EXISTS (SELECT 1 FROM password_reset_tokens t
                  WHERE t.id = NEW.reset_token_id AND t.user_id <> NEW.reset_token_user_id) THEN
                RAISE EXCEPTION 'reset journal names another user or invalid time' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_reset_journal_target BEFORE INSERT ON revocation_journal_records
            FOR EACH ROW EXECUTE FUNCTION ras_reset_journal_target();

        CREATE FUNCTION ras_reset_tokens_final() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.id, NEW.user_id, NEW.token_hash, NEW.created_at, NEW.expires_at)
                    IS DISTINCT FROM ROW(OLD.id, OLD.user_id, OLD.token_hash, OLD.created_at, OLD.expires_at)
                    OR ((OLD.used_at IS NOT NULL OR OLD.revoked_at IS NOT NULL) AND
                        ROW(NEW.used_at, NEW.revoked_at, NEW.closure_reason)
                        IS DISTINCT FROM ROW(OLD.used_at, OLD.revoked_at, OLD.closure_reason)) THEN
                    RAISE EXCEPTION 'reset token identity and ending are final' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.reset_token_id = NEW.id AND
                  ((NEW.used_at IS NULL AND NEW.revoked_at IS NULL) OR NEW.user_id <> j.reset_token_user_id)) THEN
                RAISE EXCEPTION 'journaled reset token cannot reopen or change owner' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_reset_tokens_final BEFORE INSERT OR UPDATE ON password_reset_tokens
            FOR EACH ROW EXECUTE FUNCTION ras_reset_tokens_final();

        CREATE FUNCTION ras_reset_tokens_journaled() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.used_at IS NOT NULL OR NEW.revoked_at IS NOT NULL) AND NOT EXISTS (
                SELECT 1 FROM revocation_journal_records j WHERE j.kind = 'reset_token_closure'
                  AND j.reset_token_id = NEW.id AND j.reset_token_user_id = NEW.user_id
                  AND j.reset_end_kind = CASE WHEN NEW.used_at IS NOT NULL THEN 'consumed' ELSE 'revoked' END
                  AND j.reset_reason_code = COALESCE(NEW.closure_reason,
                      CASE WHEN NEW.used_at IS NOT NULL THEN 'legacy_consumed' ELSE 'legacy_revoked' END)
                  AND j.reset_ended_at = COALESCE(NEW.used_at, NEW.revoked_at)) THEN
                RAISE EXCEPTION 'ending reset token needs its journal record' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_reset_tokens_journaled AFTER INSERT OR UPDATE ON password_reset_tokens
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_reset_tokens_journaled();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
                RAISE EXCEPTION 'database registered with a revocation journal; downgrade would reopen reset tokens';
            END IF;
        END $$;
        DROP TRIGGER trg_reset_tokens_journaled ON password_reset_tokens;
        DROP FUNCTION ras_reset_tokens_journaled();
        DROP TRIGGER trg_reset_tokens_final ON password_reset_tokens;
        DROP FUNCTION ras_reset_tokens_final();
        DROP TRIGGER trg_reset_journal_target ON revocation_journal_records;
        DROP FUNCTION ras_reset_journal_target();
    """)
    for name in ("reset_fields_required", "reset_matches_body"):
        op.drop_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", type_="check")
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
                               f"kind IN ({KINDS})")
    op.drop_constraint("uq_revocation_journal_records_reset_token_id", "revocation_journal_records", type_="unique")
    for name in ("reset_token_id", "reset_token_user_id", "reset_end_kind", "reset_reason_code", "reset_ended_at"):
        op.drop_column("revocation_journal_records", name)
    op.drop_constraint(op.f("ck_password_reset_tokens_closure_reason_valid"), "password_reset_tokens", type_="check")
    op.drop_column("password_reset_tokens", "closure_reason")
