"""Auth safety receipts/projection/privileges foundation. No journal I/O; C1 pending."""
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


SESSION_MATCH = "CASE WHEN kind = 'session_revocation' THEN (session_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,session_id}')) AND (session_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}')) AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind) AND COALESCE((line::jsonb #>> '{body,reason_code}') IN ('logout', 'replaced', 'user_revoked', 'password_reset', 'user_disabled','auth_safety_backfill'), false) AND COALESCE(length(line::jsonb #>> '{body,revoked_at}') > 0, false) ELSE true END"
RESET_MATCH = "CASE WHEN kind = 'reset_token_closure' THEN (reset_token_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,token_id}')) AND (reset_token_user_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,user_id}')) AND (reset_end_kind IS NOT DISTINCT FROM (line::jsonb #>> '{body,end_kind}')) AND (reset_reason_code IS NOT DISTINCT FROM (line::jsonb #>> '{body,reason_code}')) AND (reset_ended_at IS NOT DISTINCT FROM (line::jsonb #>> '{body,ended_at}')::timestamptz) AND (line::jsonb ->> 'kind' IS NOT DISTINCT FROM kind) AND COALESCE((reset_end_kind = 'consumed' AND reset_reason_code IN ('password_reset', 'legacy_consumed')) OR (reset_end_kind = 'revoked' AND reset_reason_code IN ('reset_replaced', 'reset_completed', 'user_disabled','auth_safety_backfill', 'legacy_revoked')), false) ELSE true END"


def upgrade():
    op.create_table("auth_hash_policies",
        sa.Column("version",sa.Integer(),primary_key=True,autoincrement=False),
        *[sa.Column(n,sa.Integer(),nullable=False) for n in ("memory_kib","time_cost","parallelism","salt_bytes","hash_bytes")],
        sa.CheckConstraint("version > 0 AND memory_kib >= 8 * parallelism AND time_cost > 0 AND parallelism > 0 AND salt_bytes > 0 AND hash_bytes > 0",name=op.f("ck_auth_hash_policies_parameters_valid")))
    # Existing argon2-cffi RFC 9106 profile, now explicit DB-only configuration.
    op.execute("INSERT INTO auth_hash_policies VALUES(1,65536,3,4,16,32)")
    for prefix in ("credential", "status"):
        op.add_column("users", sa.Column(f"{prefix}_domain", sa.Text(), sa.Computed(f"'{prefix}'", persisted=True), nullable=False))
        for suffix in ("generation", "operation_id"):
            op.add_column("users", sa.Column(f"{prefix}_{suffix}", sa.UUID(), nullable=True))
        op.add_column("users", sa.Column(f"{prefix}_reason_code", sa.Text(), server_default="legacy_unproven", nullable=False))
    op.add_column("users", sa.Column("credential_state", sa.Text(), server_default="quarantined", nullable=False))
    op.add_column("users", sa.Column("status_safety", sa.Text(), server_default="held", nullable=False))
    op.add_column("users", sa.Column("password_hash_policy_version", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.create_foreign_key(op.f("fk_users_password_hash_policy_version_auth_hash_policies"),"users","auth_hash_policies",["password_hash_policy_version"],["version"],ondelete="RESTRICT")
    op.create_check_constraint(op.f("ck_users_auth_states"), "users", "credential_state IN ('usable','quarantined') AND status_safety IN ('proven','held') AND password_hash_policy_version > 0")
    op.create_check_constraint(op.f("ck_users_credential_evidence"), "users", "credential_state <> 'usable' OR (credential_generation IS NOT NULL AND credential_operation_id IS NOT NULL)")
    op.create_check_constraint(op.f("ck_users_status_evidence"), "users", "status_safety <> 'proven' OR (status_generation IS NOT NULL AND (status = 'disabled' OR status_operation_id IS NOT NULL))")
    op.create_table("auth_change_receipts",
        sa.Column("operation_id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False), sa.Column("generation", sa.UUID(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False), sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_id", sa.BigInteger(), server_default=sa.text("txid_current()"), nullable=False),
        sa.UniqueConstraint("operation_id", "user_id", "domain", "generation", name="uq_auth_receipt_binding"),
        sa.UniqueConstraint("generation", name="uq_auth_receipt_generation"),
        sa.CheckConstraint("domain IN ('credential','status')", name=op.f("ck_auth_change_receipts_domain_valid")))
    for prefix in ("credential", "status"):
        op.create_foreign_key(f"fk_users_{prefix}_receipt", "users", "auth_change_receipts",
            [f"{prefix}_operation_id", "id", f"{prefix}_domain", f"{prefix}_generation"],
            ["operation_id", "user_id", "domain", "generation"], deferrable=True, initially="DEFERRED")
    head = [sa.Column("user_id", sa.UUID(), primary_key=True), sa.Column("projection_head_seq", sa.BigInteger(), nullable=False)]
    for prefix in ("credential", "status"):
        head += [sa.Column(f"{prefix}_seq", sa.BigInteger()), sa.Column(f"{prefix}_operation_id", sa.UUID()),
                 sa.Column(f"{prefix}_generation", sa.UUID()), sa.Column(f"{prefix}_reason_code", sa.Text()),
                 sa.Column(f"{prefix}_recorded_at", sa.DateTime(timezone=True))]
    head += [sa.Column("status_decision", sa.Text())]
    op.create_table("auth_safety_heads", *head)
    op.create_table("auth_safety_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("version", sa.SmallInteger(), nullable=False), sa.Column("cutover_state", sa.Text(), nullable=False),
        sa.Column("projection_seq", sa.BigInteger(), nullable=False), sa.Column("backfill_complete", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id = 1 AND version = 1 AND cutover_state = 'pending'", name=op.f("ck_auth_safety_state_pending_version")),
        sa.CheckConstraint("projection_seq >= 0", name=op.f("ck_auth_safety_state_projection_seq_valid")))
    op.execute("INSERT INTO auth_safety_state VALUES (1,1,'pending',0,false)")
    op.add_column("sessions", sa.Column("credential_generation", sa.UUID()))
    op.add_column("password_reset_tokens", sa.Column("credential_fence_operation_id", sa.UUID()))
    op.add_column("password_reset_tokens", sa.Column("recovery_purpose", sa.Text(), server_default="normal", nullable=False))
    op.create_check_constraint(op.f("ck_password_reset_tokens_recovery_purpose_valid"), "password_reset_tokens", "recovery_purpose IN ('normal','quarantine')")
    for name, kind in (("auth_user_id",sa.UUID()), ("auth_operation_id",sa.UUID()), ("auth_generation",sa.UUID()),
                       ("auth_domain",sa.Text()), ("auth_decision",sa.Text()), ("auth_reason_code",sa.Text()), ("auth_recorded_at",sa.DateTime(timezone=True))):
        op.add_column("revocation_journal_records", sa.Column(name, kind))
    for name in ("auth_operation_id", "auth_generation"):
        op.create_unique_constraint(f"uq_revocation_journal_records_{name}", "revocation_journal_records", [name])
    op.drop_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records", type_="check")
    op.create_check_constraint(op.f("ck_revocation_journal_records_kind_valid"), "revocation_journal_records",
        "kind IN ('genesis','revocation','recovery_begin','recovery_end','review_closure','session_revocation','reset_token_closure','invitation_closure','credential_transition','account_status_decision')")
    # Extend the existing allowlists without editing any merged revision.
    for table, name, expression in (
        ("sessions", "revoked_reason_valid", "revoked_reason IS NULL OR revoked_reason IN ('logout','replaced','user_revoked','password_reset','user_disabled','auth_safety_backfill')"),
        ("password_reset_tokens", "closure_reason_valid", "closure_reason IS NULL OR (used_at IS NOT NULL AND closure_reason IN ('password_reset','legacy_consumed')) OR (revoked_at IS NOT NULL AND closure_reason IN ('reset_replaced','reset_completed','user_disabled','legacy_revoked','auth_safety_backfill'))")):
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, expression)
    for name, expression in (("session_matches_body", SESSION_MATCH), ("reset_matches_body", RESET_MATCH)):
        op.drop_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", type_="check")
        op.create_check_constraint(op.f(f"ck_revocation_journal_records_{name}"), "revocation_journal_records", expression)
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records)
           OR EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM auth_change_receipts)
           OR EXISTS (SELECT 1 FROM auth_safety_heads)
           OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq > 0 OR backfill_complete) THEN
            RAISE EXCEPTION 'auth safety history prevents downgrade';
        END IF;
    END $$;""")
    # Drop only objects introduced by this revision; refuse any populated installation above.
    for table, trigger in (("users","trg_auth_user_final"),("users","trg_auth_user_binding"),
                           ("auth_change_receipts","trg_auth_receipt_final"),("auth_change_receipts","trg_auth_receipt_binding"),
                           ("auth_safety_heads","trg_auth_head_final"),("auth_safety_state","trg_auth_progress_final"),
                           ("sessions","trg_auth_session_fence"),("password_reset_tokens","trg_auth_token_fence"),
                           ("revocation_journal_records","trg_auth_record_valid")):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    # Routine signatures are obtained from the catalog, restricted to this revision's prefix.
    op.execute("""DO $$ DECLARE f record; BEGIN
        FOR f IN SELECT oid::regprocedure signature FROM pg_proc
                 WHERE pronamespace='public'::regnamespace AND proname LIKE 'ras_auth_%'
        LOOP EXECUTE 'DROP FUNCTION ' || f.signature || ' CASCADE'; END LOOP;
    END $$;""")
    for prefix in ("credential","status"):
        op.drop_constraint(f"fk_users_{prefix}_receipt", "users", type_="foreignkey")
    for name in ("auth_change_receipts","auth_safety_heads","auth_safety_state"):
        op.drop_table(name)
    for name in ("auth_user_id","auth_operation_id","auth_generation","auth_domain","auth_decision","auth_reason_code","auth_recorded_at"):
        op.drop_column("revocation_journal_records",name)
    for name in ("auth_states","credential_evidence","status_evidence"):
        op.drop_constraint(op.f(f"ck_users_{name}"),"users",type_="check")
    for prefix in ("credential","status"):
        for suffix in ("domain","generation","operation_id","reason_code"):
            op.drop_column("users",f"{prefix}_{suffix}")
    for name in ("credential_state","status_safety","password_hash_policy_version"):
        op.drop_column("users",name)
    op.drop_table("auth_hash_policies")
    op.drop_column("sessions","credential_generation")
    op.drop_constraint(op.f("ck_password_reset_tokens_recovery_purpose_valid"),"password_reset_tokens",type_="check")
    op.drop_column("password_reset_tokens","credential_fence_operation_id")
    op.drop_column("password_reset_tokens","recovery_purpose")
    # Restore 0032 functions/checks including its smaller reason/kind allowlists.
    op.execute(Path(__file__).with_name("0033_auth_safety_downgrade.sql").read_text(encoding="utf-8"))
