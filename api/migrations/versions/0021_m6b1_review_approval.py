"""Admin approval of one review request into a new, unpublished common copy (M6b-1).

Approving closes the request as ``approved`` and, in the same transaction,
creates a separate common item/version/file and an ``access_rights`` row the
Admin filled in for exactly that file/version/checksum. The common copy is
``pending``: no normal route lists, counts or opens it, and nothing here can
publish it (``publication_state`` is immutable until the publication gate
lands). Common items that already existed stay ``listed``, as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

OLD_STATUSES = "'open', 'withdrawn', 'rejected'"
OLD_STATUS_CHECK = "ck_review_requests_ck_review_requests_status_valid"
STATUSES = "'open', 'withdrawn', 'rejected', 'approved'"
URL_CHECK = "{col} IS NULL OR ({col} ~ '^https?://[^[:space:]]+$' AND length({col}) <= 2048)"

OLD_TRANSITION = """
        CREATE OR REPLACE FUNCTION ras_review_requests_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'open' THEN
                RAISE EXCEPTION 'closed review request is final' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.item_id IS DISTINCT FROM OLD.item_id
               OR NEW.version_id IS DISTINCT FROM OLD.version_id
               OR NEW.source_file_id IS DISTINCT FROM OLD.source_file_id
               OR NEW.requester_user_id IS DISTINCT FROM OLD.requester_user_id
               OR NEW.file_checksum_sha256 IS DISTINCT FROM OLD.file_checksum_sha256
               OR NEW.rights_epoch_at_submit IS DISTINCT FROM OLD.rights_epoch_at_submit
               OR NEW.notice_version IS DISTINCT FROM OLD.notice_version
               OR NEW.declared_source IS DISTINCT FROM OLD.declared_source
               OR NEW.declared_source_url IS DISTINCT FROM OLD.declared_source_url
               OR NEW.declared_rights IS DISTINCT FROM OLD.declared_rights
               OR NEW.declared_license_url IS DISTINCT FROM OLD.declared_license_url
               OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
                RAISE EXCEPTION 'review request target and declaration are immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'rejected' AND NOT EXISTS (
                SELECT 1 FROM users WHERE id = NEW.closed_by_user_id AND role = 'admin') THEN
                RAISE EXCEPTION 'only an admin may reject a review request' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
"""


def upgrade() -> None:
    # --- review_requests: a fourth, final state ----------------------------
    # 0020 created its checks through the naming convention, doubling the prefix.
    op.drop_constraint(op.f(OLD_STATUS_CHECK), "review_requests", type_="check")
    op.create_check_constraint(op.f("ck_review_requests_status_valid"), "review_requests", f"status IN ({STATUSES})")
    op.create_check_constraint(op.f("ck_review_requests_approved"), "review_requests",
                               "status <> 'approved' OR close_reason_code = 'approved_by_admin'")
    # Target for access_rights: the rights' checksum is the request's checksum.
    op.create_unique_constraint("uq_review_requests_id_file_checksum_sha256", "review_requests",
                                ["id", "file_checksum_sha256"])
    op.execute(OLD_TRANSITION.replace(
        "IF NEW.status = 'rejected' AND", "IF NEW.status IN ('rejected', 'approved') AND").replace(
        "'only an admin may reject a review request'", "'only an admin may reject or approve a review request'"))

    # --- library_items: pending common copies ------------------------------
    op.add_column("library_items", sa.Column("publication_state", sa.Text()))
    # Existing common items keep their current metadata visibility.
    op.execute("UPDATE library_items SET publication_state = 'listed' WHERE visibility = 'common'")
    # No default: every new common row must say explicitly which state it is in.
    op.create_check_constraint(
        op.f("ck_library_items_publication_state_valid"), "library_items",
        "(visibility = 'private' AND publication_state IS NULL)"
        # IS NOT NULL: a bare NULL IN (...) is NULL, which a CHECK lets through.
        " OR (visibility = 'common' AND publication_state IS NOT NULL"
        " AND publication_state IN ('listed', 'pending'))")
    op.create_unique_constraint("uq_library_items_id_visibility", "library_items", ["id", "visibility"])
    op.execute("""
        CREATE FUNCTION ras_library_items_publication_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.publication_state IS DISTINCT FROM OLD.publication_state THEN
                RAISE EXCEPTION 'publication state has no transition yet' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_library_items_publication_immutable
            BEFORE UPDATE ON library_items
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_publication_immutable();
    """)

    # --- access_rights ------------------------------------------------------
    op.create_table("access_rights",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("item_visibility", sa.Text(), nullable=False, server_default=sa.text("'common'")),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=False),
        sa.Column("file_checksum_sha256", sa.Text(), nullable=False),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_review_request_id", sa.UUID()),
        sa.Column("verified_source", sa.Text(), nullable=False),
        sa.Column("verified_source_url", sa.Text()),
        sa.Column("license_url", sa.Text()),
        sa.Column("evidence_note", sa.Text()),
        sa.Column("checked_on", sa.Date(), nullable=False),
        sa.Column("reviewer_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_audience", sa.Text(), nullable=False),
        sa.Column("permits_original_download", sa.Boolean(), nullable=False),
        sa.Column("permits_fulltext_search", sa.Boolean(), nullable=False),
        sa.Column("permits_external_processing", sa.Boolean(), nullable=False),
        sa.Column("scope_conditions", sa.Text()),
        sa.Column("rights_epoch_at_grant", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["item_id", "item_visibility"], ["library_items.id", "library_items.visibility"],
                                name="fk_access_rights_common_item", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                                name="fk_access_rights_version_item", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_file_id", "version_id", "file_checksum_sha256"],
                                ["source_files.id", "source_files.version_id", "source_files.checksum_sha256"],
                                name="fk_access_rights_file_version_checksum", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["origin_review_request_id", "file_checksum_sha256"],
                                ["review_requests.id", "review_requests.file_checksum_sha256"],
                                name="fk_access_rights_review_request_checksum", ondelete="RESTRICT"),
        sa.UniqueConstraint("origin_review_request_id", name="uq_access_rights_origin_review_request_id"),
        sa.CheckConstraint("item_visibility = 'common'", name="common_only"),
        sa.CheckConstraint("file_checksum_sha256 ~ '^[0-9a-f]{64}$'", name="checksum_hex"),
        sa.CheckConstraint("origin_kind IN ('review_request')", name="origin_kind_valid"),
        sa.CheckConstraint("(origin_kind = 'review_request') = (origin_review_request_id IS NOT NULL)",
                           name="origin_matches_kind"),
        sa.CheckConstraint("length(btrim(verified_source)) BETWEEN 1 AND 2000",
                           name="verified_source_length"),
        sa.CheckConstraint(URL_CHECK.format(col="verified_source_url"), name="source_url"),
        sa.CheckConstraint(URL_CHECK.format(col="license_url"), name="license_url"),
        sa.CheckConstraint("evidence_note IS NULL OR length(btrim(evidence_note)) BETWEEN 1 AND 2000",
                           name="evidence_note_length"),
        sa.CheckConstraint("license_url IS NOT NULL OR evidence_note IS NOT NULL",
                           name="evidence_required"),
        # One day of slack for an Admin whose local date is ahead of UTC.
        sa.CheckConstraint("checked_on <= (recorded_at AT TIME ZONE 'UTC')::date + 1",
                           name="checked_not_future"),
        sa.CheckConstraint("scope_audience IN ('instance_users')", name="scope_audience_valid"),
        # RAG/embedding answers cite chunks, so provider processing needs full-text use too.
        sa.CheckConstraint("NOT permits_external_processing OR permits_fulltext_search",
                           name="processing_needs_fulltext"),
        sa.CheckConstraint("scope_conditions IS NULL OR length(btrim(scope_conditions)) BETWEEN 1 AND 1000",
                           name="scope_conditions_length"),
        sa.CheckConstraint("rights_epoch_at_grant >= 0", name="epoch_non_negative"))
    op.create_index("ix_access_rights_item_id", "access_rights", ["item_id"])
    op.create_index("ix_access_rights_source_file_id", "access_rights", ["source_file_id"])

    # Row-level rules that need other tables. The deferred ones run at commit,
    # so approval, rights and the pending item must all land together.
    op.execute("""
        CREATE FUNCTION ras_access_rights_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE private_key text; common_key text; common_state text;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.reviewer_user_id AND role = 'admin') THEN
                RAISE EXCEPTION 'access rights need an admin reviewer' USING ERRCODE = 'check_violation';
            END IF;
            SELECT publication_state INTO common_state FROM library_items WHERE id = NEW.item_id;
            IF common_state IS DISTINCT FROM 'pending' THEN
                RAISE EXCEPTION 'review rights attach only to a pending common copy'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT f.storage_key INTO private_key FROM review_requests r
                JOIN source_files f ON f.id = r.source_file_id WHERE r.id = NEW.origin_review_request_id;
            SELECT storage_key INTO common_key FROM source_files WHERE id = NEW.source_file_id;
            IF private_key IS NULL OR common_key IS NULL OR private_key = common_key THEN
                RAISE EXCEPTION 'common copy must use its own object key' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_access_rights_insert
            BEFORE INSERT ON access_rights
            FOR EACH ROW EXECUTE FUNCTION ras_access_rights_insert();

        CREATE FUNCTION ras_access_rights_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'access rights have no update/delete path yet' USING ERRCODE = 'check_violation';
        END $$;
        CREATE TRIGGER trg_access_rights_append_only
            BEFORE UPDATE OR DELETE ON access_rights
            FOR EACH ROW EXECUTE FUNCTION ras_access_rights_append_only();

        CREATE FUNCTION ras_access_rights_needs_approval() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM review_requests
                           WHERE id = NEW.origin_review_request_id AND status = 'approved') THEN
                RAISE EXCEPTION 'access rights need their review request approved'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_access_rights_needs_approval
            AFTER INSERT ON access_rights DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_access_rights_needs_approval();

        CREATE FUNCTION ras_review_requests_approval_has_rights() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'approved' AND NOT EXISTS (
                SELECT 1 FROM access_rights WHERE origin_review_request_id = NEW.id) THEN
                RAISE EXCEPTION 'approved review request needs its access rights'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_review_requests_approval_has_rights
            AFTER UPDATE ON review_requests DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_review_requests_approval_has_rights();

        CREATE FUNCTION ras_library_items_pending_has_rights() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.publication_state = 'pending' AND NOT EXISTS (
                SELECT 1 FROM access_rights WHERE item_id = NEW.id) THEN
                RAISE EXCEPTION 'pending common copy needs its access rights'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_library_items_pending_has_rights
            AFTER INSERT ON library_items DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_pending_has_rights();
    """)


def downgrade() -> None:
    # An approved request and its pending copy have no 0020 representation.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM review_requests WHERE status = 'approved') THEN
                RAISE EXCEPTION 'approved review requests exist; downgrade below 0021 would lose them';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_library_items_pending_has_rights ON library_items")
    op.execute("DROP FUNCTION ras_library_items_pending_has_rights()")
    op.execute("DROP TRIGGER trg_review_requests_approval_has_rights ON review_requests")
    op.execute("DROP FUNCTION ras_review_requests_approval_has_rights()")
    op.drop_table("access_rights")  # drops its triggers
    for fn in ("ras_access_rights_needs_approval", "ras_access_rights_append_only", "ras_access_rights_insert"):
        op.execute(f"DROP FUNCTION {fn}()")
    op.execute("DROP TRIGGER trg_library_items_publication_immutable ON library_items")
    op.execute("DROP FUNCTION ras_library_items_publication_immutable()")
    op.drop_constraint("uq_library_items_id_visibility", "library_items", type_="unique")
    op.drop_constraint(op.f("ck_library_items_publication_state_valid"), "library_items", type_="check")
    op.drop_column("library_items", "publication_state")
    op.execute(OLD_TRANSITION)
    op.drop_constraint("uq_review_requests_id_file_checksum_sha256", "review_requests", type_="unique")
    op.drop_constraint(op.f("ck_review_requests_approved"), "review_requests", type_="check")
    op.drop_constraint(op.f("ck_review_requests_status_valid"), "review_requests", type_="check")
    op.create_check_constraint(op.f(OLD_STATUS_CHECK), "review_requests", f"status IN ({OLD_STATUSES})")
