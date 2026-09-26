"""Shared provider reservation leases; uncertain calls retain their cost ceiling."""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import EmbeddingCall, SearchUsage

log = logging.getLogger(__name__)
LOCK = "SELECT pg_advisory_xact_lock(hashtext('ras_embedding_capacity'))"


def expire_reservations(db: Session) -> None:
    """Call inside the capacity transaction, before counting any active slots."""
    now = datetime.now(timezone.utc)
    for model in (EmbeddingCall, SearchUsage):
        db.execute(update(model).where(model.status == "reserved",
            (model.lease_until <= now) | (model.lease_until.is_(None))).values(
                status="uncertain", cost_status="lease_expired_reserved_ceiling",
                finished_at=now, lease_until=None))
    db.flush()


def reap_expired(engine: Engine) -> None:
    """Persist expiry even when the following admission is rejected/rolled back."""
    with Session(engine) as db:
        db.execute(text(LOCK))
        expire_reservations(db)
        db.commit()


def lease_deadline(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def renew_lease(engine: Engine, model: type[EmbeddingCall] | type[SearchUsage],
                reservation_id: uuid.UUID, seconds: int) -> bool:
    with Session(engine) as db:
        db.execute(text(LOCK))
        now = datetime.now(timezone.utc)
        result = db.execute(update(model).where(model.id == reservation_id,
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
                log.exception("provider reservation lease renewal failed")

    worker = threading.Thread(target=run, name="provider-reservation-heartbeat", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
