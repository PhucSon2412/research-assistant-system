"""Owner review requests for one exact private version/file (M6a).

An open request is the only grant that lets an Admin read a User's private
original, and only for the version/file it names. Closing it (withdraw or
reject) is final; the same file may be submitted again as a new row.
What the User declares about source and rights is stored as a claim, never
as verified rights. No common item or AccessRights row is created here.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

STATUSES = "'open', 'withdrawn', 'rejected'"
REJECT_REASONS = ("'rights_not_established', 'insufficient_evidence', 'wrong_file_or_version',"
                  " 'out_of_scope', 'other'")


def upgrade() -> None:
    # Composite targets: the requester must own the item, and the recorded
    # checksum must be the file's own checksum for that version.
    op.create_unique_constraint("uq_library_items_id_owner_user_id", "library_items", ["id", "owner_user_id"])
    op.create_unique_constraint("uq_source_files_id_version_id_checksum", "source_files",
                                ["id", "version_id", "checksum_sha256"])
    op.create_table("review_requests",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=False),
        sa.Column("requester_user_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_checksum_sha256", sa.Text(), nullable=False),
        sa.Column("rights_epoch_at_submit", sa.BigInteger(), nullable=False),
        sa.Column("notice_version", sa.Text(), nullable=False),
        sa.Column("declared_source", sa.Text(), nullable=False),
        sa.Column("declared_source_url", sa.Text()),
        sa.Column("declared_rights", sa.Text(), nullable=False),
        sa.Column("declared_license_url", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_by_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("close_reason_code", sa.Text()),
        sa.Column("rejection_note", sa.Text()),
        sa.ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                                name="fk_review_requests_version_item", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_file_id", "version_id", "file_checksum_sha256"],
                                ["source_files.id", "source_files.version_id", "source_files.checksum_sha256"],
                                name="fk_review_requests_file_version_checksum", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["item_id", "requester_user_id"],
                                ["library_items.id", "library_items.owner_user_id"],
                                name="fk_review_requests_item_owner", ondelete="RESTRICT"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_review_requests_status_valid"),
        sa.CheckConstraint("file_checksum_sha256 ~ '^[0-9a-f]{64}$'", name="ck_review_requests_checksum_hex"),
        sa.CheckConstraint("rights_epoch_at_submit >= 0", name="ck_review_requests_epoch_non_negative"),
        sa.CheckConstraint("length(notice_version) > 0", name="ck_review_requests_notice_not_empty"),
        sa.CheckConstraint("length(btrim(declared_source)) BETWEEN 1 AND 2000",
                           name="ck_review_requests_declared_source_length"),
        sa.CheckConstraint("length(btrim(declared_rights)) BETWEEN 1 AND 2000",
                           name="ck_review_requests_declared_rights_length"),
        sa.CheckConstraint("declared_source_url IS NULL OR (declared_source_url ~ '^https?://[^[:space:]]+$'"
                           " AND length(declared_source_url) <= 2048)", name="ck_review_requests_source_url"),
        sa.CheckConstraint("declared_license_url IS NULL OR (declared_license_url ~ '^https?://[^[:space:]]+$'"
                           " AND length(declared_license_url) <= 2048)", name="ck_review_requests_license_url"),
        sa.CheckConstraint("(status = 'open') = (closed_at IS NULL AND closed_by_user_id IS NULL"
                           " AND close_reason_code IS NULL)", name="ck_review_requests_closed_fields"),
        sa.CheckConstraint("closed_at IS NULL OR closed_at >= submitted_at", name="ck_review_requests_closed_after"),
        sa.CheckConstraint("status <> 'withdrawn' OR (close_reason_code = 'withdrawn_by_requester'"
                           " AND closed_by_user_id = requester_user_id)", name="ck_review_requests_withdrawn"),
        sa.CheckConstraint(f"status <> 'rejected' OR close_reason_code IN ({REJECT_REASONS})",
                           name="ck_review_requests_rejected_reason"),
        sa.CheckConstraint("rejection_note IS NULL OR (status = 'rejected'"
                           " AND length(btrim(rejection_note)) BETWEEN 1 AND 500)",
                           name="ck_review_requests_rejection_note"))
    # At most one open request per exact file/version, also under concurrent submits.
    op.create_index("uq_review_requests_open_file", "review_requests", ["source_file_id", "version_id"],
                    unique=True, postgresql_where=sa.text("status = 'open'"))
    op.create_index("ix_review_requests_requester_user_id", "review_requests", ["requester_user_id"])
    op.create_index("ix_review_requests_status_submitted_at", "review_requests", ["status", "submitted_at"])
    op.create_index("ix_review_requests_item_id", "review_requests", ["item_id"])
    # Closing is one-way and a request never changes which file it names.
    op.execute("""
        CREATE FUNCTION ras_review_requests_transition() RETURNS trigger
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
        CREATE TRIGGER trg_review_requests_transition
            BEFORE UPDATE ON review_requests
            FOR EACH ROW EXECUTE FUNCTION ras_review_requests_transition();
    """)
    # A request is born open; there is no path to insert an already-decided row.
    op.execute("""
        CREATE FUNCTION ras_review_requests_insert_open() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status <> 'open' THEN
                RAISE EXCEPTION 'review request must start open' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_review_requests_insert_open
            BEFORE INSERT ON review_requests
            FOR EACH ROW EXECUTE FUNCTION ras_review_requests_insert_open();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_review_requests_insert_open ON review_requests")
    op.execute("DROP FUNCTION ras_review_requests_insert_open()")
    op.execute("DROP TRIGGER trg_review_requests_transition ON review_requests")
    op.execute("DROP FUNCTION ras_review_requests_transition()")
    op.drop_index("ix_review_requests_item_id", table_name="review_requests")
    op.drop_index("ix_review_requests_status_submitted_at", table_name="review_requests")
    op.drop_index("ix_review_requests_requester_user_id", table_name="review_requests")
    op.drop_index("uq_review_requests_open_file", table_name="review_requests")
    op.drop_table("review_requests")
    op.drop_constraint("uq_source_files_id_version_id_checksum", "source_files", type_="unique")
    op.drop_constraint("uq_library_items_id_owner_user_id", "library_items", type_="unique")
