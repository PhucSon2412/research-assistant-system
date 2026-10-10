"""Financial-only document maintenance. No content predicate, item lock or vector write.

Capacity -> call UUID -> original-day budgets -> flush -> journal exclusive ->
exact durable nonce/tuple/status -> fresh clock -> known commit. Cleanup can
scrub content while a response is in flight; the financial tuple survives it.
"""
import math
import threading
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app import auth_safety
from app.ingest import checkpoints as cp, runtime
from app.ingest import worker as w, indexer as ix
from app.models import EmbeddingCall, EmbeddingBudgetDay
from app.provider_capacity import LOCK

FIELDS = ('id', 'source_file_id', 'owner_user_id', 'model_id', 'provider', 'pipeline_version',
          'reservation_token', 'reservation_job_id', 'reservation_job_token', 'reserved_day', 'reserved_usd')


def matches(call, proof):
    return (call is not None and proof.reservation_token is not None
        and all(getattr(call, k) == getattr(proof, k) for k in FIELDS))


def expiry_matches(call, proof):
    # Historic rows have no attempt nonce. They may ONLY lose an expired slot,
    # retaining the ceiling; never adopt them for renewal, settlement or resend.
    return call is not None and all(getattr(call, k) == getattr(proof, k) for k in FIELDS)


def locked(db):
    db.execute(text(f'SET LOCAL lock_timeout = {int(w.get_settings().recovery.gate_lock_timeout_ms)}'))
    db.execute(text(LOCK))


def final(db, call, proof, *, old_deadline=None, expired=False):
    db.flush()
    db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
    snapshot = cp.frozen(call)
    try:
        auth_safety.require_final(db, writer=True)
    except Exception:
        raise runtime.Halt('recovery_gate_closed') from None
    db.refresh(call)
    if not matches(call, proof) or cp.frozen(call) != snapshot:
        raise runtime.Halt('reservation_lost')
    at = w.utcnow()
    if old_deadline is not None and (at < old_deadline if expired else at >= old_deadline):
        raise runtime.Halt('reservation_lost')


def invalid_result(result, settings):
    if result is None:
        return None
    try:
        valid = (result.model_id == settings.embedding.model_id and bool(result.vector)
            and all(isinstance(v, (float, int)) and math.isfinite(v) for v in result.vector)
            and (result.input_tokens is None or (type(result.input_tokens) is int
                and 0 <= result.input_tokens <= settings.embedding.max_request_bytes)))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return ix.EmbeddingError('provider_response_invalid', response_model_id=result.response_model_id,
            response_provider=result.response_provider)


def settle(db, proof, settings, *, result=None, error=None):
    """One settlement per nonce, even after expiry/revoke/cleanup; original day only."""
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'settlement'
        locked(db)
        call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == proof.id).with_for_update())
        if not matches(call, proof):
            raise runtime.Halt('reservation_lost')
        if call.status in ('completed', 'rejected'):
            final(db, call, proof)
            return call.status
        if call.status not in ('reserved', 'uncertain'):
            raise runtime.Halt('reservation_lost')
        budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == call.reserved_day).with_for_update())
        user = ix._user_budget(db, call.reserved_day, call.owner_user_id)
        if budget is None or budget.reserved_usd < call.reserved_usd or (user and user.reserved_usd < call.reserved_usd):
            raise runtime.Halt('embedding_accounting_unavailable')
        call.response_model_id = result.response_model_id if result else error.response_model_id if error else None
        call.response_provider = result.response_provider if result else error.response_provider if error else None
        rejected = error is not None and error.definitely_rejected
        if result is not None or rejected:
            from decimal import Decimal
            amount = (ix._amount(result.input_tokens, settings) if result and result.input_tokens is not None
                else call.reserved_usd if result else Decimal(0))
            if amount > call.reserved_usd:
                raise runtime.Halt('embedding_usage_exceeds_reservation')
            budget.reserved_usd -= call.reserved_usd
            budget.committed_usd += amount
            if user:
                user.reserved_usd -= call.reserved_usd
                user.committed_usd += amount
            call.status = 'completed' if result else 'rejected'
            call.input_tokens = result.input_tokens if result else None
            call.cost_usd = amount if rejected or result.input_tokens is not None else None
            call.cost_status = ('rejected_no_charge' if rejected else 'usage_price_estimate'
                if result.input_tokens is not None else 'unknown_reserved_ceiling')
        else:
            call.status, call.cost_status = 'uncertain', 'unknown_reserved_ceiling'
        call.lease_until, call.finished_at = None, w.utcnow()
        final(db, call, proof)
        status = call.status
        runtime.commit(db, outcome)
        return status
    return runtime.short(db, write)


def renew(engine, proof, settings):
    db = Session(engine, expire_on_commit=False)
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'heartbeat'
        locked(db)
        call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == proof.id).with_for_update())
        if not matches(call, proof) or call.status != 'reserved' or call.lease_until is None:
            return False
        old, at = call.lease_until, w.utcnow()
        if old <= at:
            return False
        call.lease_until = max(old, at + timedelta(seconds=settings.embedding.reservation_lease_seconds))
        final(db, call, proof, old_deadline=old)
        runtime.commit(db, outcome)
        return True
    return runtime.short(db, write)


def reap(db, settings):
    """One bounded batch, financial-only; no nested transaction/connection."""
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'reaper'
        locked(db)
        calls = list(db.scalars(select(EmbeddingCall).where(EmbeddingCall.status == 'reserved',
            or_(EmbeddingCall.lease_until <= w.utcnow(), EmbeddingCall.lease_until.is_(None)))
            .order_by(EmbeddingCall.id).limit(settings.ingest.dispatch_batch).with_for_update(skip_locked=True)))
        snapshots = [(c, cp.frozen(c), c.lease_until) for c in calls]
        for call, _, _ in snapshots:
            call.status, call.cost_status = 'uncertain', 'lease_expired_reserved_ceiling'
            call.lease_until, call.finished_at = None, w.utcnow()
        db.flush()
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        try:
            auth_safety.require_final(db, writer=True)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
        for call, proof, _ in snapshots:
            db.refresh(call)
            if not expiry_matches(call, proof) or call.status != 'uncertain' or call.lease_until is not None:
                raise runtime.Halt('reservation_lost')
        at = w.utcnow()
        if any(deadline is not None and deadline > at for _, _, deadline in snapshots):
            raise runtime.Halt('reservation_lost')
        runtime.commit(db, outcome)
        return len(calls)
    return runtime.short(db, write)


@contextmanager
def heartbeat(engine, proof, settings):
    stopped = threading.Event()
    def run():
        while not stopped.wait(max(1, settings.embedding.reservation_lease_seconds // 3)):
            try:
                if not renew(engine, proof, settings):
                    return
            except BaseException:
                # Stop renewing, retain ceiling. No exception text/DSN/content log.
                return
    thread = threading.Thread(target=run, name='document-reservation-heartbeat', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()  # the attempt owns the thread until its DB work has settled
