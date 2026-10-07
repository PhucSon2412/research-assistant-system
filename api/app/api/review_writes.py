"""Three review JSON writers; runtime only, no storage/provider or replay.

Operation-owned outcome survives rollback/close faults. Journal intent can end
a grant during recovery even when this HTTP request returns an error.
"""
from dataclasses import dataclass
from datetime import datetime
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery
from app.api.errors import ApiError
from app.api.resource_writes import _actor
from app.api.review_publication_reads import review_publication_json
from app.authz import m2a_private_derivatives_clause, review_history_admin_clause
from app.identity.predicates import lock_users
from app.models import LibraryItem, PaperVersion, ReviewRequest, SourceFile
from app.models.enums import AuditOutcome


@dataclass(frozen=True)
class Closure:
    request_id: uuid.UUID
    item_id: uuid.UUID
    version_id: uuid.UUID
    source_file_id: uuid.UUID
    requester_user_id: uuid.UUID
    actor_user_id: uuid.UUID
    status: str
    reason_code: str
    closed_at: datetime

    def waiting(self, db):
        # Marker is minted by a BEFORE trigger only on open -> closed. An
        # arbitrary existing UUID/closure or another transaction cannot qualify.
        return db.scalar(select(ReviewRequest.id).where(
            ReviewRequest.id == self.request_id, ReviewRequest.item_id == self.item_id,
            ReviewRequest.version_id == self.version_id, ReviewRequest.source_file_id == self.source_file_id,
            ReviewRequest.requester_user_id == self.requester_user_id,
            ReviewRequest.closed_by_user_id == self.actor_user_id, ReviewRequest.status == self.status,
            ReviewRequest.close_reason_code == self.reason_code, ReviewRequest.closed_at == self.closed_at,
            ReviewRequest.closure_transaction_id == text('txid_current()'),
            ~select(recovery.RevocationJournalRecord.seq).where(
                recovery.RevocationJournalRecord.review_request_id == self.request_id).exists()))

    def body(self):
        return dict(request_id=str(self.request_id), item_id=str(self.item_id), version_id=str(self.version_id),
                    source_file_id=str(self.source_file_id), status=self.status,
                    reason_code=self.reason_code, closed_at=self.closed_at.isoformat())


@dataclass
class Outcome:
    closure: Closure | None = None
    record: journal.Record | None = None
    append_started: bool = False
    written: bool = False
    commit_started: bool = False
    committed: bool = False
    teardown_failed: bool = False


def _gate(db, settings, pending=None):
    try:
        return _integrity(db, settings, pending)
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, 'service_recovering') from None


def _integrity(db, settings, pending=None):
    # Direct exclusive acquisition AFTER all rows/FK/audit. No shared upgrade.
    db.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': recovery.JOURNAL_LOCK})
    state = journal.read(settings.recovery.journal_path)
    view = list(recovery._db_view(db))
    if pending is not None:
        if pending.waiting(db) != pending.request_id:
            raise ApiError(503, 'service_recovering')
        view[6] -= 1  # exactly this staged, transaction-bound closure only
    if not recovery.compare(state, tuple(view)).open or auth_safety.cutover(db) != 'ready':
        raise ApiError(503, 'service_recovering')
    return state


def _current(db, principal, csrf, clock, settings, action):
    p = _actor(db, principal, csrf, clock, settings.auth.session_idle)
    if (action == 'submit' and p.role != 'user') or (action == 'reject' and not p.is_admin):
        raise ApiError(403, 'forbidden')
    return p


def _parse(raw):
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _target(db, principal, body, *, lock=False):
    stmt = (select(LibraryItem.id, PaperVersion.id, SourceFile.id, SourceFile.checksum_sha256,
                   LibraryItem.rights_epoch)
        .join(PaperVersion, PaperVersion.item_id == LibraryItem.id)
        .join(SourceFile, SourceFile.version_id == PaperVersion.id)
        .where(LibraryItem.id == body.item_id, PaperVersion.id == body.version_id,
               SourceFile.id == body.source_file_id, SourceFile.item_id == LibraryItem.id,
               m2a_private_derivatives_clause(principal)))
    return db.execute(stmt.with_for_update(of=LibraryItem, read=True) if lock else stmt).one_or_none()


def _release(db, outcome):
    failed = False
    for method in ('rollback', 'close'):
        try:
            getattr(db, method)()
        except Exception:
            failed = True
    if failed:
        outcome.teardown_failed = True
        try:
            db.invalidate()
        except Exception:
            pass


def _response(db, principal, rid, csrf, clock, settings, action):
    from app.api.routes import reviews
    db.expire_all()
    read = (lambda p: reviews._read_admin_get(str(rid), p, db)) if action == 'reject' else (
        lambda p: reviews._read_get_own(str(rid), p, db))
    result = review_publication_json(db, principal, clock=clock, idle=settings.auth.session_idle,
                                    review=True, admin=action == 'reject', read=read)
    _current(db, principal, csrf, clock, settings, action)  # POST CSRF/role + clock after DTO SQL
    return result


def write(db: Session, principal, *, action, body=None, request_id=None, csrf_token,
          clock, settings, outcome: Outcome | None = None):
    from app.api.routes import reviews
    outcome = outcome if outcome is not None else Outcome()
    error = result = None
    try:
        auth_safety.require_writer(db)
        db.execute(text(f'SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}'))
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf_token, clock, settings, action)

        def deny(status, code):
            _gate(db, settings)
            _current(db, principal, csrf_token, clock, settings, action)
            raise ApiError(status, code)

        if action == 'submit':
            if body.notice_version != settings.review.notice_version:
                deny(409, 'notice_changed')
            target = _target(db, current, body, lock=True)
            if target is None:
                deny(404, 'not_found')
            rr = ReviewRequest(item_id=target[0], version_id=target[1], source_file_id=target[2],
                requester_user_id=current.user_id, status='open', file_checksum_sha256=target[3],
                rights_epoch_at_submit=target[4], notice_version=body.notice_version,
                declared_source=body.declared_source, declared_source_url=body.declared_source_url,
                declared_rights=body.declared_rights, declared_license_url=body.declared_license_url,
                submitted_at=clock())
            # A failed duplicate INSERT must not discard caller/item locks or
            # use an aborted transaction for the late caller denial.
            try:
                with db.begin_nested():
                    db.add(rr)
                    db.flush()
            except IntegrityError as exc:
                if getattr(getattr(exc.orig, 'diag', None), 'constraint_name', None) == 'uq_review_requests_open_file':
                    deny(409, 'review_request_open')
                raise
            rid = rr.id
            reviews._audit(db, current, 'review.request.submit', AuditOutcome.SUCCESS, rid,
                item_id=str(target[0]), version_id=str(target[1]), source_file_id=str(target[2]))

            def check():
                if _target(db, current, body) != target:
                    raise ApiError(404, 'not_found')
                if body.notice_version != settings.review.notice_version or body.acknowledged is not True:
                    raise ApiError(409, 'notice_changed')
        else:
            rid = _parse(request_id)
            stmt = select(ReviewRequest).where(ReviewRequest.id == rid)
            if action == 'withdraw':
                stmt = stmt.join(LibraryItem, LibraryItem.id == ReviewRequest.item_id).where(
                    ReviewRequest.requester_user_id == current.user_id, m2a_private_derivatives_clause(current))
            else:
                stmt = stmt.where(review_history_admin_clause(current))
            rr = db.scalar(stmt.with_for_update(of=ReviewRequest))
            if rr is None:
                deny(404, 'not_found')
            if rr.status != 'open':
                deny(409, 'review_request_closed')
            if action == 'withdraw':
                db.execute(select(LibraryItem.id).where(LibraryItem.id == rr.item_id,
                    m2a_private_derivatives_clause(current)).with_for_update(read=True)).all()
            rr.status = 'withdrawn' if action == 'withdraw' else 'rejected'
            rr.closed_at = clock()
            rr.closed_by_user_id = current.user_id
            rr.close_reason_code = 'withdrawn_by_requester' if action == 'withdraw' else body.reason_code.value
            rr.rejection_note = None if action == 'withdraw' else body.note
            outcome.closure = Closure(rr.id, rr.item_id, rr.version_id, rr.source_file_id,
                rr.requester_user_id, current.user_id, rr.status, rr.close_reason_code, rr.closed_at)
            reviews._audit(db, current, 'review.request.' + action, AuditOutcome.SUCCESS, rid,
                           reason_code=rr.close_reason_code)

            def check():
                c = outcome.closure
                row = db.execute(select(ReviewRequest.item_id, ReviewRequest.version_id, ReviewRequest.source_file_id,
                    ReviewRequest.requester_user_id, ReviewRequest.closed_by_user_id, ReviewRequest.status,
                    ReviewRequest.close_reason_code, ReviewRequest.closed_at, ReviewRequest.closure_transaction_id)
                    .where(ReviewRequest.id == rid)).one_or_none()
                if row is None or tuple(row[:8]) != (c.item_id, c.version_id, c.source_file_id,
                        c.requester_user_id, c.actor_user_id, c.status, c.reason_code, c.closed_at) or row[8] != db.scalar(text('SELECT txid_current()')):
                    raise ApiError(409, 'review_request_changed')
                if action == 'withdraw' and not db.scalar(select(LibraryItem.id).where(
                        LibraryItem.id == c.item_id, m2a_private_derivatives_clause(current))):
                    raise ApiError(404, 'not_found')

        db.flush()  # includes actor FK/audit before journal
        state = _gate(db, settings, outcome.closure)
        with db.no_autoflush:
            check()
            _current(db, principal, csrf_token, clock, settings, action)
            if outcome.closure is not None:
                outcome.record = journal.next_record(state.head, journal.REVIEW_CLOSURE, outcome.closure.body())
                db.execute(text('SELECT ras_auth_review_closure_mirror(:line)'), {'line': outcome.record.line})
                outcome.append_started = True
                try:
                    journal.append(settings.recovery.journal_path, state, [outcome.record])
                except (OSError, journal.JournalError):
                    # Only append/fsync errors enter the fail-closed commit branch.
                    # All actor/resource checks have already passed. Do not call
                    # a normal gate that would roll back this false mirror.
                    pass
                else:
                    outcome.written = True
                    db.execute(text('SELECT ras_auth_review_closure_confirm(:line)'), {'line': outcome.record.line})
                    _gate(db, settings)
                    check()
                    _current(db, principal, csrf_token, clock, settings, action)
        outcome.commit_started = True
        db.commit()
        outcome.committed = True
        if outcome.closure is not None and not outcome.written:
            raise ApiError(503, 'review_journal_unavailable')
        result = _response(db, principal, rid, csrf_token, clock, settings, action)
    except ApiError as exc:
        error = ApiError(503, 'review_write_outcome_uncertain') if outcome.commit_started and not outcome.committed else exc
    except Exception:
        code = ('review_write_outcome_uncertain' if outcome.commit_started and not outcome.committed else
                'review_write_response_unavailable' if outcome.committed else
                'review_closure_pending_recovery' if outcome.append_started else 'review_write_unavailable')
        error = ApiError(503, code)
    finally:
        _release(db, outcome)
    if error is not None:
        raise error from None
    if outcome.teardown_failed:
        raise ApiError(503, 'review_write_response_unavailable')
    return result
