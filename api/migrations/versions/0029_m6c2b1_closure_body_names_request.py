"""A review-closure mirror row must name, in its journal line, the request it is filed under (PR #28 review).

0028 checked the closing ``UPDATE`` against the mirror's ``review_request_id``
and the line's item/version/file/code, but not the line's own
``body.request_id``: a row filed under request R whose line named another
(or no existing) request was accepted, and a restore replaying that line
would act on the request the line names, not on R.

- CHECK ``ck_revocation_journal_records_closure_request_matches_body``: a
  ``review_closure`` row with a ``review_request_id`` holds exactly that ID
  as ``body.request_id`` of its line. ``IS NOT DISTINCT FROM``, not ``=``: a
  line whose ``body.request_id`` is missing or JSON ``null`` gives ``NULL``
  with ``=``, and a CHECK lets ``NULL`` through (PR #28 review, second round).
  Only a ``TRUE`` result is accepted. It is checked when the row is inserted,
  which every writer does *before* appending the line to the journal file
  (``app.recovery._journal_decision``), so a wrong row stops the write with
  nothing appended. A ``superseded`` row (no ``review_request_id``) is
  unchanged. A request the database does not hold is still allowed: the
  journal may name one a restored snapshot lacks (``recorded_request_absent``).
- ``ras_review_requests_closure_journaled`` (0028) also compares the line's
  ``body.request_id`` with ``NEW.id``.

0028 is not edited: it has already been applied (the dev database was at
0028 when this revision was written). Upgrade refuses a database that holds
such a mismatched row instead of hiding it. Downgrade is refused on any
database registered with a journal, as for 0026-0028.
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

MATCHES_BODY = ("CASE WHEN kind = 'review_closure' AND review_request_id IS NOT NULL"
                " THEN review_request_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,request_id}')"
                " ELSE true END")

TRIGGER = """
    CREATE OR REPLACE FUNCTION ras_review_requests_closure_journaled() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        -- The line is canonical JSON written by app.journal; its body names the exact target and code.
        IF NOT EXISTS (
            SELECT 1 FROM revocation_journal_records j
            WHERE j.kind = 'review_closure' AND j.review_request_id = NEW.id{body_id}
              AND (j.line::jsonb #>> '{{body,item_id}}') = NEW.item_id::text
              AND (j.line::jsonb #>> '{{body,version_id}}') = NEW.version_id::text
              AND (j.line::jsonb #>> '{{body,source_file_id}}') = NEW.source_file_id::text
              AND (j.line::jsonb #>> '{{body,reason_code}}') = NEW.close_reason_code
              AND (NEW.status = 'closed_by_recovery' OR (j.line::jsonb #>> '{{body,status}}') = NEW.status)) THEN
            RAISE EXCEPTION 'closing a review request needs its revocation journal record'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END $$;
"""


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM revocation_journal_records WHERE kind = 'review_closure'
                       AND review_request_id IS NOT NULL
                       AND review_request_id::text IS DISTINCT FROM (line::jsonb #>> '{body,request_id}')) THEN
                RAISE EXCEPTION 'a review_closure journal record is filed under another request than its line names; investigate before upgrading';
            END IF;
        END $$;
    """)
    op.create_check_constraint(op.f("ck_revocation_journal_records_closure_request_matches_body"),
                               "revocation_journal_records", MATCHES_BODY)
    op.execute(TRIGGER.format(body_id="\n              AND (j.line::jsonb #>> '{body,request_id}') = NEW.id::text"))


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
                RAISE EXCEPTION 'the database is registered with a revocation journal; downgrade below 0029 would accept a review closure record naming another request';
            END IF;
        END $$;
    """)
    op.execute(TRIGGER.format(body_id=""))
    op.drop_constraint(op.f("ck_revocation_journal_records_closure_request_matches_body"),
                       "revocation_journal_records", type_="check")
