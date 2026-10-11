"""Shared provider reservation leases; uncertain calls retain their cost ceiling."""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update, text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import EmbeddingCall, SearchUsage

log = logging.getLogger(__name__)
LOCK = "SELECT pg_advisory_xact_lock(hashtext('ras_embedding_capacity'))"


def expire_reservations(db: Session) -> None:
    """Call inside the capacity transaction, before counting any active slots."""
    now = datetime.now(timezone.utc)
    for model in (EmbeddingCall, SearchUsage):
        predicate = [model.status == "reserved", (model.lease_until <= now) | (model.lease_until.is_(None))]
        if model is EmbeddingCall:
            # C2d-5d2b owns nonce-bound maintenance with runtime + journal proof.
            predicate.append(model.reservation_token.is_(None))
        else:
            predicate.append(model.reservation_token.is_(None))
        db.execute(update(model).where(*predicate).values(
                status="uncertain", cost_status="lease_expired_reserved_ceiling",
                finished_at=now, lease_until=None))
    db.flush()


def reap_expired(engine: Engine) -> None:
    """Persist expiry even when the following admission is rejected/rolled back."""
    with Session(engine) as db:
        db.execute(text(LOCK))
        expire_reservations(db)
        documents = db.scalar(select(EmbeddingCall.id).where(EmbeddingCall.status == 'reserved',
            EmbeddingCall.reservation_token.is_not(None), EmbeddingCall.lease_until <= datetime.now(timezone.utc)).limit(1))
        db.commit()
    if documents is not None:
        # Compatibility bridge for shared query admission; nonce-bound document
        # expiry always owns a NEW runtime/gate transaction, after capacity release.
        from app.config import get_settings
        from app.ingest.document_accounting import reap
        reap(Session(engine, expire_on_commit=False), get_settings())
    # Legacy RAG admissions cannot expire/adopt nonce-bound query attempts.
    # Their expiry belongs to a separate short runtime financial boundary.
    from app.config import get_settings
    from app.search_accounting import reap as reap_search
    reap_search(engine, get_settings())


def lease_deadline(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def renew_lease(engine: Engine, model: type[EmbeddingCall] | type[SearchUsage],
                reservation_id: uuid.UUID, seconds: int) -> bool:
    with Session(engine) as db:
        db.execute(text(LOCK))
        now = datetime.now(timezone.utc)
        result = db.execute(update(model).where(model.id == reservation_id,
            model.reservation_token.is_(None),
            model.status == "reserved", model.lease_until > now).values(
                lease_until=func.greatest(model.lease_until, now + timedelta(seconds=seconds))))
        db.commit()
        return result.rowcount == 1


@contextmanager
def reservation_heartbeat(engine: Engine, model: type[EmbeddingCall] | type[SearchUsage],
                          reservation_id: uuid.UUID, seconds: int):
    """Keep a live provider call leased; a dead process stops renewing."""
    stopped = threading.Event()

    def run() -> None:
        while not stopped.wait(max(1, seconds // 3)):
            try:
                if not renew_lease(engine, model, reservation_id, seconds):
                    log.warning("provider reservation lease could not be renewed")
                    return
            except Exception:
                log.warning("provider reservation lease renewal failed")
                return

    worker = threading.Thread(target=run, name="provider-reservation-heartbeat", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
