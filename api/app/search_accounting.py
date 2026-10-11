"""Financial authority only: capacity -> usage -> flush -> journal -> binding/clock.

No content, caller renewal, provider dispatch or generation adoption. An expired
lease loses its slot and keeps its ceiling; late settlement never restores access.
"""
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, text

from app import auth_safety, search as s
from app.clock import utcnow
from app.ingest.checkpoints import frozen
from app.models import (EmbeddingCall, EmbeddingBudgetDay, EmbeddingUserBudgetDay, SearchUsage)
from app.provider_capacity import LOCK
from app import search_boundary as boundary

FIELDS = ('id', 'user_id', 'session_id', 'operation_id', 'reservation_token', 'day',
          'task_type', 'model_id', 'provider', 'reserved_usd')


def matches(row, proof):
    return (row is not None and proof.reservation_token is not None
        and proof.task_type in ('embed_query', 'rerank')
        and all(getattr(row, k) == getattr(proof, k) for k in FIELDS))


def locked(db, settings):
    db.execute(text(f'SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}'))
    db.execute(text(LOCK))


def final(db, row, proof, clock, *, deadline=None):
    db.flush()
    db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
    snapshot = frozen(row)
    auth_safety.require_final(db, writer=True)
    db.refresh(row)
    if not matches(row, proof) or frozen(row) != snapshot or (deadline and clock() >= deadline):
        raise s.ApiError(503, 'search_reservation_lost')


def costs(row):
    return {'status': row.cost_status, 'estimated_usd': str(row.cost_usd) if row.cost_usd is not None else None,
        'reserved_usd': str(row.reserved_usd), 'input_tokens': row.input_tokens,
        'search_units': row.search_units, 'warnings': list(row.warnings)}


def settle(engine, proof, settings, *, tokens=None, units=None, rejected=False,
           response_model_id=None, response_provider=None, clock=utcnow):
    boundary.entry(engine)
    def write(db, outcome):
        locked(db, settings)
        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update())
        if not matches(row, proof):
            raise s.ApiError(503, 'search_reservation_lost')
        if row.status not in ('completed', 'rejected'):
            if row.status not in ('reserved', 'uncertain'):
                raise s.ApiError(503, 'search_reservation_lost')
            row.response_model_id, row.response_provider = response_model_id, response_provider
            if rejected:
                row.status, row.cost_usd, row.cost_status = 'rejected', Decimal(0), 'rejected_no_charge'
            elif tokens is not None:
                price = settings.embedding.max_usd_per_million_tokens if row.task_type == 'embed_query' else settings.rerank.max_usd_per_million_tokens
                if type(tokens) is not int or tokens < 0 or (units is not None and (type(units) is not int or units < 0)):
                    raise s.ApiError(503, 'search_usage_invalid')
                amount = s.money(tokens, price)
                if amount > row.reserved_usd:
                    raise s.ApiError(503, 'search_usage_exceeds_reservation')
                row.status, row.cost_usd, row.cost_status = 'completed', amount, 'usage_price_estimate'
                row.input_tokens, row.search_units = tokens, units
            else:
                row.status, row.cost_status = 'uncertain', 'unknown_reserved_ceiling'
            row.finished_at, row.lease_until = clock(), None
        final(db, row, proof, clock)
        result = costs(row)
        boundary.commit(db, outcome)
        return result
    return boundary.checkpoint(engine, 'settlement', write)


def renew(engine, proof, settings, *, clock=utcnow):
    boundary.entry(engine)
    def write(db, outcome):
        locked(db, settings)
        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update())
        if not matches(row, proof) or row.status != 'reserved' or not row.lease_until:
            return False
        old, at = row.lease_until, clock()
        if old <= at:
            return False
        policy = settings.embedding if row.task_type == 'embed_query' else settings.rerank
        row.lease_until = max(old, at + timedelta(seconds=policy.reservation_lease_seconds))
        final(db, row, proof, clock, deadline=old)
        boundary.commit(db, outcome)
        return True
    return boundary.checkpoint(engine, 'heartbeat', write)


def reap(engine, settings, *, clock=utcnow):
    boundary.entry(engine)
    def write(db, outcome):
        locked(db, settings)
        rows = list(db.scalars(select(SearchUsage).where(SearchUsage.status == 'reserved',
            (SearchUsage.lease_until <= clock()) | SearchUsage.lease_until.is_(None))
            .order_by(SearchUsage.id).limit(settings.ingest.dispatch_batch).with_for_update(skip_locked=True)))
        expected = [(r, frozen(r), r.lease_until) for r in rows]
        for row in rows:
            row.status, row.cost_status = 'uncertain', 'lease_expired_reserved_ceiling'
            row.finished_at, row.lease_until = clock(), None
        db.flush()
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        auth_safety.require_final(db, writer=True)
        for row, proof, _ in expected:
            db.refresh(row)
            if any(getattr(row, k) != getattr(proof, k) for k in FIELDS) or row.status != 'uncertain' or row.lease_until is not None:
                raise s.ApiError(503, 'search_reservation_lost')
        at = clock()
        if any(deadline and deadline > at for _, _, deadline in expected):
            raise s.ApiError(503, 'search_reservation_lost')
        boundary.commit(db, outcome)
        return len(rows)
    return boundary.checkpoint(engine, 'reaper', write)


@contextmanager
def heartbeat(engine, proof, settings, *, clock=utcnow):
    stopped, failed = threading.Event(), threading.Event()
    policy = settings.embedding if proof.task_type == 'embed_query' else settings.rerank
    def run():
        while not stopped.wait(max(1, policy.reservation_lease_seconds // 3)):
            try:
                if not renew(engine, proof, settings, clock=clock):
                    failed.set()
                    return
            except BaseException:
                failed.set()
                return
    thread = threading.Thread(target=run, name='search-reservation-heartbeat', daemon=True)
    thread.start()
    try:
        yield failed
    finally:
        stopped.set()
        thread.join()


def admit(db, principal, settings, task, amount, operation_id, clock):
    if task not in ('embed_query', 'rerank'):
        raise s.ApiError(503, 'search_task_invalid')
    day = clock().date()
    locked(db, settings)
    if task == "embed_query":
        active_document = db.scalar(select(func.count()).select_from(EmbeddingCall).where(
            EmbeddingCall.status == "reserved", EmbeddingCall.lease_until > clock()))
        active_query = db.scalar(select(func.count()).select_from(SearchUsage).where(
            SearchUsage.task_type == "embed_query", SearchUsage.status == "reserved", SearchUsage.lease_until > clock()))
        if active_document + active_query >= settings.embedding.max_concurrent_requests:
            raise s.ApiError(429, "search_embedding_concurrency_limit")
    elif task == "rerank":
        active_rerank = db.scalar(select(func.count()).select_from(SearchUsage).where(
            SearchUsage.task_type == "rerank", SearchUsage.status == "reserved", SearchUsage.lease_until > clock()))
        if active_rerank >= settings.rerank.max_concurrent_requests:
            raise s.ApiError(429, "search_rerank_concurrency_limit")
    used = s._total(db, day)
    doc = db.get(EmbeddingBudgetDay, day)
    doc_used = (doc.committed_usd + doc.reserved_usd) if doc else Decimal(0)
    app_cap = Decimal(str(settings.budget.app_daily_usd))
    if used + doc_used + amount > app_cap:
        raise s.ApiError(429, "search_app_budget_exhausted")
    warnings = []
    if task == "embed_query":
        cap = Decimal(str(settings.budget.embedding_daily_usd))
        user_cap = Decimal(str(settings.budget.user_embedding_daily_usd))
        doc_user = db.get(EmbeddingUserBudgetDay, (day, principal.user_id))
        doc_user_used = (doc_user.committed_usd + doc_user.reserved_usd) if doc_user else Decimal(0)
        query_spend = s._total(db, day, "embed_query")
        if query_spend + doc_used + amount > cap:
            raise s.ApiError(429, "search_embedding_budget_exhausted")
        if s._total(db, day, "embed_query", principal.user_id) + doc_user_used + amount > user_cap:
            raise s.ApiError(429, "search_user_budget_exhausted")
        if query_spend + doc_used + amount >= cap * Decimal(str(settings.budget.embedding_warning_fraction)):
            warnings.append("embedding_bucket_near_limit")
        if (s._total(db, day, "embed_query", principal.user_id) + doc_user_used + amount >=
                user_cap * Decimal(str(settings.budget.user_embedding_warning_fraction))):
            warnings.append("embedding_user_quota_near_limit")
        model = settings.embedding.model_id
        lease_seconds = settings.embedding.reservation_lease_seconds
    elif task == "rerank":
        rerank_spend = s._total(db, day, "rerank")
        rerank_cap = Decimal(str(settings.budget.rerank_daily_usd))
        if rerank_spend + amount > rerank_cap:
            raise s.ApiError(429, "search_rerank_budget_exhausted")
        if s._total(db, day, "rerank", principal.user_id) + amount > Decimal(str(settings.budget.user_rerank_daily_usd)):
            raise s.ApiError(429, "search_user_rerank_budget_exhausted")
        if rerank_spend + amount >= rerank_cap * Decimal(str(settings.budget.rerank_warning_fraction)):
            warnings.append("rerank_bucket_near_limit")
        model = settings.rerank.model_id
        lease_seconds = settings.rerank.reservation_lease_seconds
    usage = SearchUsage(reservation_token=uuid.uuid4(), operation_id=operation_id, session_id=principal.session_id, user_id=principal.user_id, day=day, task_type=task, model_id=model,
        provider="openrouter", status="reserved", reserved_usd=amount, cost_status="unknown",
        warnings=warnings, lease_until=clock() + timedelta(seconds=lease_seconds))
    db.add(usage)
    db.flush()
    return usage
