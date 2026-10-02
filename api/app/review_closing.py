"""Commit the close of a review grant with its journal record (M6c-2b1).

Withdraw and reject lock the request ``FOR UPDATE``, set it closed and add
their audit row; then ``commit_close`` journals the close and commits. The
record (IDs, decision, code, time) is appended and ``fsync``-ed **before** the
commit, under the journal lock taken after the row lock, as a revocation
does (``app.recovery``). Approve (``app.promotion``) follows the same steps
inside its own transaction.

Failure windows (the same as a revocation's):

- journal and commit succeed: the grant ends at the commit;
- the journal write fails in any way (nothing, a torn line, or a complete line
  whose ``fsync`` raised) but the commit succeeds: the grant still ends (fail
  closed), the record is mirrored ``journal_confirmed = false`` and the gate
  stays closed until ``recover``; the caller gets ``503 review_journal_unavailable``;
- the line is durable but the commit fails or its outcome is unknown: the
  line is replayed at once (idempotent; a replay into a database that still
  has the request open writes ``closed_by_recovery``); if that fails too the
  gate stays closed (database behind journal) until ``recover``:
  ``503 review_closure_pending_recovery``;
- neither takes it: nothing changed, the grant is still open, ``503
  review_closure_unavailable``, and the caller may retry.

Bytes of a review download that passed its last check before the commit are
still sent (as before); every new request after the commit is refused.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app import recovery
from app.api.errors import ApiError
from app.config import get_settings
from app.models import ReviewRequest
from app.models.enums import ReviewStatus


def commit_close(db: Session, rr: ReviewRequest, *, journal_path: Path | None = None) -> ReviewRequest:
    """Journal and commit a close already applied to the locked ``rr``; returns the request as committed."""
    if journal_path is None:
        journal_path = get_settings().recovery.journal_path
    try:
        journaled = recovery.record_review_closure(db, journal_path, rr)
    except recovery.RecoveryError:
        db.rollback()
        raise ApiError(503, "service_recovering") from None
    except Exception:
        db.rollback()
        raise ApiError(503, "review_closure_unavailable") from None
    request_id, closed_at = rr.id, rr.closed_at
    try:
        db.commit()  # the grant ends here
    except Exception:
        db.rollback()
        if not journaled.written:  # neither the journal nor the database took it
            raise ApiError(503, "review_closure_unavailable") from None
        # The line is durable, so the grant must end: replay it now (a no-op if the commit did land).
        try:
            recovery.replay_record(db.get_bind(), journaled.record, closed_at)
        except Exception:
            raise ApiError(503, "review_closure_pending_recovery") from None
        rr = db.get(ReviewRequest, request_id, populate_existing=True)
        if rr is None or rr.status == ReviewStatus.OPEN.value:
            raise ApiError(503, "review_closure_pending_recovery")
    if not journaled.written:
        # Committed, so the grant is over, but the journal lacks it: the gate stays closed until repaired.
        raise ApiError(503, "review_journal_unavailable")
    return rr
