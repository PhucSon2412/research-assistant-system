"""Keep invitation endings across restore; no journal I/O in migration."""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

KINDS = "'genesis', 'revocation', 'recovery_begin', 'recovery_end', 'review_closure', 'session_revocation', 'reset_token_closure'"
INVITATION_IDS_REQUIRED = ("CASE WHEN kind = 'invitation_closure' THEN invitation_id IS NOT NULL"
                      " AND invitation_end_kind IS NOT NULL"
                      " AND invitation_reason_code IS NOT NULL AND invitation_ended_at IS NOT NULL"
                      " ELSE invitation_id IS NULL AND invitation_end_kind IS NULL"
                      " AND invitation_reason_code IS NULL AND invitation_ended_at IS NULL END")
INVITATION_MATCHES_BODY = ("CASE WHEN kind = 'invitation_closure' THEN"
    " (invitation_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,invitation_id}'))"
    " AND (invitation_end_kind IS NOT DISTINCT FROM (line::jsonb #>> '{body,end_kind}'))"
    " AND (invitation_reason_code IS NOT DISTINCT FROM (line::jsonb #>> '{body,reason_code}'))"
    " AND (invitation_ended_at IS NOT DISTINCT FROM (line::jsonb #>> '{body,ended_at}')::timestamptz)"
    " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
    " AND COALESCE((invitation_end_kind = 'accepted' AND invitation_reason_code IN ('invitation_accepted', 'legacy_accepted'))"
    " OR (invitation_end_kind = 'revoked' AND invitation_reason_code IN"
    " ('invitation_replaced', 'admin_revoked', 'delivery_failed', 'legacy_revoked')), false) ELSE true END")
RECOVERY_VALID = ("(closed_by_recovery_at IS NULL AND recovery_end_kind IS NULL) OR"
                  " (closed_by_recovery_at IS NOT NULL AND recovery_end_kind IS NOT NULL"
                  " AND recovery_end_kind IN ('accepted','revoked') AND closure_reason IS NOT NULL"
                  " AND accepted_at IS NULL AND accepted_user_id IS NULL AND revoked_at IS NULL)")
REASON_VALID = ("closure_reason IS NULL OR (COALESCE(recovery_end_kind,"
                " CASE WHEN accepted_at IS NOT NULL THEN 'accepted' ELSE 'revoked' END) = 'accepted'"
                " AND closure_reason IN ('invitation_accepted','legacy_accepted')"
                " AND COALESCE(accepted_at,closed_by_recovery_at) IS NOT NULL) OR"
                " (COALESCE(recovery_end_kind,CASE WHEN revoked_at IS NOT NULL THEN 'revoked' END) = 'revoked'"
                " AND closure_reason IN ('admin_revoked','invitation_replaced','delivery_failed','legacy_revoked')"
                " AND COALESCE(revoked_at,closed_by_recovery_at) IS NOT NULL)")
OPEN_INDEX = "accepted_at IS NULL AND revoked_at IS NULL AND closed_by_recovery_at IS NULL"



def upgrade() -> None:
    op.add_column("invitations", sa.Column("closed_by_recovery_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invitations", sa.Column("recovery_end_kind", sa.Text(), nullable=True))
    op.drop_index("uq_invitations_open_email", table_name="invitations")
    op.create_index("uq_invitations_open_email", "invitations", ["email"], unique=True, postgresql_where=sa.text(OPEN_INDEX))
    op.add_column("invitations", sa.Column("closure_reason", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_invitations_recovery_fields_consistent"), "invitations", RECOVERY_VALID)
    op.create_check_constraint(op.f("ck_invitations_closure_reason_valid"),
                               "invitations", "COALESCE((" + REASON_VALID + "), false)")
    for name, type_ in (("invitation_id", sa.UUID()),
                        ("invitation_end_kind", sa.Text()), ("invitation_reason_code", sa.Text()),
                        ("invitation_ended_at", sa.DateTime(timezone=True))):
        op.add_column("revocation_journal_records", sa.Column(name, type_, nullable=True))
    op.create_unique_constraint("uq_revocation_journal_records_invitation_id", "revocation_journal_records",
                                ["invitation_id"])
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
                               f"kind IN ({KINDS}, 'invitation_closure')")
    for name, expr in (("invitation_fields_required", INVITATION_IDS_REQUIRED), ("invitation_matches_body", INVITATION_MATCHES_BODY)):
        op.create_check_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", expr)
    op.execute("""
        CREATE FUNCTION ras_invitation_journal_target() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE b jsonb; stamp timestamptz;
        BEGIN
            IF NEW.kind <> 'invitation_closure' THEN RETURN NEW; END IF;
            b := NEW.line::jsonb -> 'body';
            -- Independent SQL validation; CHECK must never pass via NULL or JSON null.
            IF jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'invalid invitation journal body' USING ERRCODE = 'check_violation';
            END IF;
            IF (SELECT count(*) FROM jsonb_object_keys(b)) <> 4
               OR EXISTS (SELECT 1 FROM jsonb_object_keys(NEW.line::jsonb) k
                          WHERE k NOT IN ('body','hash','kind','prev','seq'))
               OR NOT b ?& ARRAY['invitation_id','end_kind','reason_code','ended_at']
               OR EXISTS (SELECT 1 FROM jsonb_each(b) kv WHERE jsonb_typeof(kv.value) <> 'string')
               OR COALESCE(b->>'invitation_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false) = false
               OR COALESCE(b->>'ended_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$', false) = false THEN
                RAISE EXCEPTION 'invalid invitation journal fields' USING ERRCODE = 'check_violation';
            END IF;
            stamp := (b->>'ended_at')::timestamptz;
            IF substring(b->>'ended_at',12,2)::int > 23 OR substring(b->>'ended_at',15,2)::int > 59
               OR substring(b->>'ended_at',18,2)::int > 59 OR NOT isfinite(stamp) THEN
                RAISE EXCEPTION 'invalid invitation journal time' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_invitation_journal_target BEFORE INSERT ON revocation_journal_records
            FOR EACH ROW EXECUTE FUNCTION ras_invitation_journal_target();

        CREATE FUNCTION ras_invitations_final() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF num_nonnulls(OLD.accepted_at, OLD.revoked_at, OLD.closed_by_recovery_at) > 0 THEN
                    RAISE EXCEPTION 'ended invitation cannot be deleted' USING ERRCODE = 'check_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.id, NEW.email, NEW.token_hash, NEW.created_at, NEW.expires_at, NEW.invited_by_user_id)
                    IS DISTINCT FROM ROW(OLD.id, OLD.email, OLD.token_hash, OLD.created_at, OLD.expires_at, OLD.invited_by_user_id)
                    OR (num_nonnulls(OLD.accepted_at, OLD.revoked_at, OLD.closed_by_recovery_at) > 0 AND
                        ROW(NEW.accepted_at, NEW.accepted_user_id, NEW.revoked_at, NEW.closed_by_recovery_at,
                            NEW.recovery_end_kind, NEW.closure_reason)
                        IS DISTINCT FROM ROW(OLD.accepted_at, OLD.accepted_user_id, OLD.revoked_at,
                            OLD.closed_by_recovery_at, OLD.recovery_end_kind, OLD.closure_reason)) THEN
                    RAISE EXCEPTION 'invitation identity and ending are final' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.invitation_id = NEW.id)
               AND num_nonnulls(NEW.accepted_at, NEW.revoked_at, NEW.closed_by_recovery_at) = 0 THEN
                RAISE EXCEPTION 'journaled invitation cannot reopen' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.accepted_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users u
                WHERE u.id = NEW.accepted_user_id AND u.email = NEW.email AND u.role = 'user') THEN
                RAISE EXCEPTION 'accepted invitation must name its User account' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_invitations_final BEFORE INSERT OR UPDATE OR DELETE ON invitations
            FOR EACH ROW EXECUTE FUNCTION ras_invitations_final();

        CREATE FUNCTION ras_invitations_journaled() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF num_nonnulls(NEW.accepted_at, NEW.revoked_at, NEW.closed_by_recovery_at) > 0 AND NOT EXISTS (
                SELECT 1 FROM revocation_journal_records j WHERE j.kind = 'invitation_closure'
                  AND j.invitation_id = NEW.id
                  AND j.invitation_end_kind = COALESCE(NEW.recovery_end_kind,
                        CASE WHEN NEW.accepted_at IS NOT NULL THEN 'accepted' ELSE 'revoked' END)
                  AND j.invitation_reason_code = COALESCE(NEW.closure_reason,
                        CASE WHEN NEW.accepted_at IS NOT NULL THEN 'legacy_accepted' ELSE 'legacy_revoked' END)
                  AND j.invitation_ended_at = COALESCE(NEW.accepted_at, NEW.revoked_at, NEW.closed_by_recovery_at)) THEN
                RAISE EXCEPTION 'ending invitation needs its journal record' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_invitations_journaled AFTER INSERT OR UPDATE ON invitations
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_invitations_journaled();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
                RAISE EXCEPTION 'database registered with a revocation journal; downgrade would reopen invitations';
            END IF;
        END $$;
        DROP TRIGGER trg_invitations_journaled ON invitations;
        DROP FUNCTION ras_invitations_journaled();
        DROP TRIGGER trg_invitations_final ON invitations;
        DROP FUNCTION ras_invitations_final();
        DROP TRIGGER trg_invitation_journal_target ON revocation_journal_records;
        DROP FUNCTION ras_invitation_journal_target();
    """)
    for name in ("invitation_fields_required", "invitation_matches_body"):
        op.drop_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", type_="check")
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
                               f"kind IN ({KINDS})")
    op.drop_constraint("uq_revocation_journal_records_invitation_id", "revocation_journal_records", type_="unique")
    for name in ("invitation_id", "invitation_end_kind", "invitation_reason_code", "invitation_ended_at"):
        op.drop_column("revocation_journal_records", name)
    op.drop_constraint(op.f("ck_invitations_closure_reason_valid"), "invitations", type_="check")
    op.drop_constraint(op.f("ck_invitations_recovery_fields_consistent"), "invitations", type_="check")
    op.drop_column("invitations", "closure_reason")
    op.drop_index("uq_invitations_open_email", table_name="invitations")
    op.create_index("uq_invitations_open_email", "invitations", ["email"], unique=True,
                    postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"))
    op.drop_column("invitations", "recovery_end_kind")
    op.drop_column("invitations", "closed_by_recovery_at")
