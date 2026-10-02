"""Journal the end of every review grant (M6c-2b1).

A backup taken while a review request was open, restored after the owner
withdrew it or an Admin rejected or approved it, used to bring the request
back ``open``: the Admin could again list it, read its review metadata and
stream the owner's private original. The revocation journal (0026/0027) now
also holds a ``review_closure`` record for each such close, and recovery
replays it.

- ``revocation_journal_records``: kind ``review_closure``, column
  ``review_request_id`` (unique, no FK: the restored snapshot may lack the
  request) and ``applied_via='close'`` for the closing transaction itself.
  The confirm-only trigger of 0027 now also keeps ``review_request_id``
  unchanged.
- ``review_requests``: final status ``closed_by_recovery``, written only by a
  replay into a snapshot that still had the request open. It keeps the
  original decision's reason code and time, has no actor and no note, and is
  never an approval (no rights, no common copy, nothing to publish).
- Deferred constraint trigger: a request cannot leave ``open`` unless the same
  transaction holds the journal record naming that request, its
  item/version/file and its reason code (and the same decision, unless it is
  a replay). Every closing path therefore journals, as every tombstone does.

No backfill here: the migration cannot see the journal file. Requests closed
before this revision have no record; the recovery gate stays closed
(``review_closures_not_journaled``) until ``recover`` appends them to the
journal (no ``--confirm-db-is-current``: a close is final, so importing one
from any database can only end a grant that really ended). A backup taken
before that import is protected only for closes the live database had
imported. Downgrade is refused on any database registered with a journal.
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

REJECT_REASONS = "'rights_not_established', 'insufficient_evidence', 'wrong_file_or_version', 'out_of_scope', 'other'"
CLOSE_REASONS = f"'withdrawn_by_requester', 'approved_by_admin', {REJECT_REASONS}"
STATUSES_0021 = "'open', 'withdrawn', 'rejected', 'approved'"
STATUSES = "'open', 'withdrawn', 'rejected', 'approved', 'closed_by_recovery'"
KINDS_0026 = "'genesis', 'revocation', 'recovery_begin', 'recovery_end'"
KINDS = "'genesis', 'revocation', 'recovery_begin', 'recovery_end', 'review_closure'"
VIA_0026 = "'genesis', 'revoke', 'replay', 'import'"
VIA = "'genesis', 'revoke', 'close', 'replay', 'import'"

CONFIRM_ONLY = """
    CREATE OR REPLACE FUNCTION ras_revocation_journal_records_confirm_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND NOT OLD.journal_confirmed AND NEW.journal_confirmed
           AND ROW(NEW.seq, NEW.kind, NEW.record_hash, NEW.line, NEW.tombstone_id, NEW.applied_via, NEW.result,
                   NEW.recorded_at{new_extra})
               IS NOT DISTINCT FROM ROW(OLD.seq, OLD.kind, OLD.record_hash, OLD.line, OLD.tombstone_id,
                                        OLD.applied_via, OLD.result, OLD.recorded_at{old_extra}) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'revocation_journal_records is append-only (only journal_confirmed false -> true)'
            USING ERRCODE = 'check_violation';
    END $$;
"""


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    # --- the mirror: a fifth kind, naming its request ----------------------
    op.add_column("revocation_journal_records", sa.Column("review_request_id", sa.UUID(), nullable=True))
    op.create_unique_constraint("uq_revocation_journal_records_review_request_id", "revocation_journal_records",
                                ["review_request_id"])
    _replace_check("revocation_journal_records", "ck_revocation_journal_records_kind_valid", f"kind IN ({KINDS})")
    _replace_check("revocation_journal_records", "ck_revocation_journal_records_applied_via_valid",
                   f"applied_via IN ({VIA})")
    op.create_check_constraint(op.f("ck_revocation_journal_records_request_only_for_closure"),
                               "revocation_journal_records", "kind = 'review_closure' OR review_request_id IS NULL")
    op.create_check_constraint(op.f("ck_revocation_journal_records_closure_names_request"),
                               "revocation_journal_records",
                               "kind <> 'review_closure' OR review_request_id IS NOT NULL OR result = 'superseded'")
    op.execute(CONFIRM_ONLY.format(new_extra=", NEW.review_request_id", old_extra=", OLD.review_request_id"))

    # --- review_requests: the replayed close --------------------------------
    _replace_check("review_requests", "ck_review_requests_status_valid", f"status IN ({STATUSES})")
    op.create_check_constraint(
        op.f("ck_review_requests_closed_by_recovery"), "review_requests",
        "status <> 'closed_by_recovery' OR (closed_at IS NOT NULL AND closed_by_user_id IS NULL"
        f" AND rejection_note IS NULL AND close_reason_code IN ({CLOSE_REASONS}))")
    op.execute("""
        CREATE FUNCTION ras_review_requests_closure_journaled() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- The line is canonical JSON written by app.journal; its body names the exact target and code.
            IF NOT EXISTS (
                SELECT 1 FROM revocation_journal_records j
                WHERE j.kind = 'review_closure' AND j.review_request_id = NEW.id
                  AND (j.line::jsonb #>> '{body,item_id}') = NEW.item_id::text
                  AND (j.line::jsonb #>> '{body,version_id}') = NEW.version_id::text
                  AND (j.line::jsonb #>> '{body,source_file_id}') = NEW.source_file_id::text
                  AND (j.line::jsonb #>> '{body,reason_code}') = NEW.close_reason_code
                  AND (NEW.status = 'closed_by_recovery' OR (j.line::jsonb #>> '{body,status}') = NEW.status)) THEN
                RAISE EXCEPTION 'closing a review request needs its revocation journal record'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_review_requests_closure_journaled
            AFTER UPDATE ON review_requests DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW WHEN (OLD.status = 'open' AND NEW.status <> 'open')
            EXECUTE FUNCTION ras_review_requests_closure_journaled();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            -- 0027 code cannot read a review_closure record: its gate would stay shut on journal_corrupt.
            IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
                RAISE EXCEPTION 'the database is registered with a revocation journal; downgrade below 0028 would let a restore reopen closed review grants';
            END IF;
        END $$;
        DROP TRIGGER trg_review_requests_closure_journaled ON review_requests;
        DROP FUNCTION ras_review_requests_closure_journaled();
    """)
    op.drop_constraint(op.f("ck_review_requests_closed_by_recovery"), "review_requests", type_="check")
    _replace_check("review_requests", "ck_review_requests_status_valid", f"status IN ({STATUSES_0021})")
    op.execute(CONFIRM_ONLY.format(new_extra="", old_extra=""))
    op.drop_constraint(op.f("ck_revocation_journal_records_closure_names_request"), "revocation_journal_records",
                       type_="check")
    op.drop_constraint(op.f("ck_revocation_journal_records_request_only_for_closure"), "revocation_journal_records",
                       type_="check")
    _replace_check("revocation_journal_records", "ck_revocation_journal_records_applied_via_valid",
                   f"applied_via IN ({VIA_0026})")
    _replace_check("revocation_journal_records", "ck_revocation_journal_records_kind_valid", f"kind IN ({KINDS_0026})")
    op.drop_constraint("uq_revocation_journal_records_review_request_id", "revocation_journal_records", type_="unique")
    op.drop_column("revocation_journal_records", "review_request_id")
