"""C3 operator recovery: login->operator map, registered cases, journal case kind, routines.

No journal I/O. Existing revisions stay untouched; downgrade only on pristine DBs.
"""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

V4 = "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'"
KINDS = ("'genesis','revocation','recovery_begin','recovery_end','review_closure','session_revocation',"
         "'reset_token_closure','invitation_closure','credential_transition','account_status_decision'")
SESSION_REASONS = "'logout','replaced','user_revoked','password_reset','user_disabled','auth_safety_backfill'"
TOKEN_REVOKED = "'reset_replaced','reset_completed','user_disabled','legacy_revoked','auth_safety_backfill'"
CASE_COLUMNS = (("case_id", sa.UUID()), ("case_operator_id", sa.UUID()), ("case_user_id", sa.UUID()),
                ("case_action", sa.Text()), ("case_operation_id", sa.UUID()),
                ("case_recorded_at", sa.DateTime(timezone=True)))


def _session_match(reasons):
    return ("CASE WHEN kind = 'session_revocation' THEN (session_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,session_id}'))"
            " AND (session_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}'))"
            " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
            f" AND COALESCE((line::jsonb #>> '{{body,reason_code}}') IN ({reasons}), false)"
            " AND COALESCE(length(line::jsonb #>> '{body,revoked_at}') > 0, false) ELSE true END")


def _reset_match(revoked):
    return ("CASE WHEN kind = 'reset_token_closure' THEN (reset_token_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,token_id}'))"
            " AND (reset_token_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}'))"
            " AND (reset_end_kind IS NOT DISTINCT FROM (line::jsonb #>> '{body,end_kind}'))"
            " AND (reset_reason_code IS NOT DISTINCT FROM (line::jsonb #>> '{body,reason_code}'))"
            " AND (reset_ended_at IS NOT DISTINCT FROM (line::jsonb #>> '{body,ended_at}')::timestamptz)"
            " AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind)"
            " AND COALESCE((reset_end_kind = 'consumed' AND reset_reason_code IN ('password_reset', 'legacy_consumed'))"
            f" OR (reset_end_kind = 'revoked' AND reset_reason_code IN ({revoked})), false) ELSE true END")


def _allowlists(kinds, sessions, tokens):
    for table, name, expression in (
            ("revocation_journal_records", "kind_valid", f"kind IN ({kinds})"),
            ("sessions", "revoked_reason_valid", f"revoked_reason IS NULL OR revoked_reason IN ({sessions})"),
            ("password_reset_tokens", "closure_reason_valid",
             "closure_reason IS NULL OR (used_at IS NOT NULL AND closure_reason IN ('password_reset','legacy_consumed'))"
             f" OR (revoked_at IS NOT NULL AND closure_reason IN ({tokens}))"),
            ("revocation_journal_records", "session_matches_body", _session_match(sessions)),
            ("revocation_journal_records", "reset_matches_body", _reset_match(tokens))):
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, expression)


def upgrade():
    op.create_table("auth_recovery_operators",
        sa.Column("operator_id", sa.UUID(), primary_key=True),
        sa.Column("login_role", sa.Text(), nullable=False),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("login_role", name="uq_auth_recovery_operators_login_role"),
        sa.CheckConstraint("login_role ~ '^[a-z][a-z0-9_]{0,62}$'", name=op.f("ck_auth_recovery_operators_login_role_valid")),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= provisioned_at",
                           name=op.f("ck_auth_recovery_operators_revoked_after_provisioned")))
    op.create_table("auth_recovery_cases",
        sa.Column("case_id", sa.UUID(), primary_key=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.UUID(), sa.ForeignKey("auth_recovery_operators.operator_id", ondelete="RESTRICT",
                  name="fk_auth_recovery_cases_operator_id_auth_recovery_operators"), nullable=False),
        sa.Column("approver_id", sa.UUID(), nullable=False),
        sa.Column("journal_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("expected_role", sa.Text(), nullable=False),
        sa.Column("expected_credential_operation_id", sa.UUID()),
        sa.Column("expected_status_operation_id", sa.UUID()),
        sa.Column("verification_method", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"case_id::text ~ {V4}", name=op.f("ck_auth_recovery_cases_case_id_v4")),
        sa.CheckConstraint("action IN ('set_credential','resolve_initial_status')", name=op.f("ck_auth_recovery_cases_action_valid")),
        sa.CheckConstraint("expected_role IN ('admin','user')", name=op.f("ck_auth_recovery_cases_expected_role_valid")),
        sa.CheckConstraint("verification_method ~ '^[a-z][a-z0-9_]{2,40}$'",
                           name=op.f("ck_auth_recovery_cases_verification_method_code")),
        sa.CheckConstraint("isfinite(approved_at) AND isfinite(expires_at) AND expires_at > approved_at",
                           name=op.f("ck_auth_recovery_cases_window_valid")))
    for name, kind in CASE_COLUMNS:
        op.add_column("revocation_journal_records", sa.Column(name, kind))
    for name in ("case_id", "case_operation_id"):
        op.create_unique_constraint(f"uq_revocation_journal_records_{name}", "revocation_journal_records", [name])
    columns = ", ".join(name for name, _ in CASE_COLUMNS)
    op.create_check_constraint(op.f("ck_revocation_journal_records_case_fields_required"), "revocation_journal_records",
        f"CASE WHEN kind = 'operator_recovery_case' THEN num_nonnulls({columns}) = 6 ELSE num_nonnulls({columns}) = 0 END")
    _allowlists(KINDS + ",'operator_recovery_case'", SESSION_REASONS + ",'operator_recovery'",
                TOKEN_REVOKED + ",'operator_recovery'")
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    # Never drop operator/case protection once an installation has auth or recovery history.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM recovery_registrations)
       OR EXISTS (SELECT 1 FROM revocation_journal_records) OR EXISTS (SELECT 1 FROM auth_change_receipts)
       OR EXISTS (SELECT 1 FROM auth_safety_heads) OR EXISTS (SELECT 1 FROM auth_recovery_operators)
       OR EXISTS (SELECT 1 FROM auth_recovery_cases)
       OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq>0 OR backfill_complete) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    op.execute("DROP TRIGGER trg_auth_receipt_operator_case ON auth_change_receipts")
    op.execute("DROP TRIGGER trg_auth_case_record_valid ON revocation_journal_records")
    for signature in ("ras_auth_operator_outcome(uuid)", "ras_auth_operator_confirm(uuid,text[])",
                      "ras_auth_operator_stage(uuid,timestamptz,text[],text,integer)",
                      "ras_auth_operator_snapshot(uuid,timestamptz,boolean)", "ras_auth_operator_case(uuid)",
                      "ras_auth_operator_inspect(uuid)", "ras_auth_operator_gate()", "ras_auth_operator_whoami()",
                      "ras_auth_operator_assert()", "ras_auth_receipt_operator_case()", "ras_auth_case_record_valid()"):
        op.execute(f"DROP FUNCTION {signature}")
    _allowlists(KINDS, SESSION_REASONS, TOKEN_REVOKED)
    op.drop_constraint(op.f("ck_revocation_journal_records_case_fields_required"), "revocation_journal_records", type_="check")
    for name in ("case_id", "case_operation_id"):
        op.drop_constraint(f"uq_revocation_journal_records_{name}", "revocation_journal_records", type_="unique")
    for name, _ in CASE_COLUMNS:
        op.drop_column("revocation_journal_records", name)
    op.drop_table("auth_recovery_cases")
    op.drop_table("auth_recovery_operators")
    op.execute("DROP FUNCTION ras_auth_recovery_case_final()")
    op.execute("DROP FUNCTION ras_auth_recovery_operator_final()")
