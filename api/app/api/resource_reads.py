"""Final boundary for the thirteen read-only resource JSON handlers.

Only permitted page items/owned collections are locked, in that order and
UUID order, before citation and journal advisory locks. Count scopes are never
row-locked. Reads are rebuilt under these locks; two equal observations detect
page/count churn at READ COMMITTED without claiming snapshot isolation.
"""
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.authz import Principal, visible_items_clause
from app.citations import _lock as lock_citations
from app.config import get_settings
from app.identity.final_caller import final_caller
from app.models import Collection, LibraryItem


def resource_json(db: Session, principal: Principal, *, clock: Callable[[], datetime],
                  idle: timedelta, read: Callable[[Principal], dict],
                  item_ids: Callable[[dict], Iterable[str]] = lambda data: (),
                  collection_ids: Callable[[dict], Iterable[str]] = lambda data: (),
                  citations: bool = False, admin: bool = False) -> dict:
    # A formatter shared with a writer must never enter this boundary.
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("resource read requires a clean session")

    def caller() -> Principal:
        proof = final_caller(db, session_id=principal.session_id, user_id=principal.user_id,
                             clock=clock, idle=idle, admin=admin)
        return Principal(proof.user_id, proof.role, proof.session_id, proof.email, proof.display_name)

    def observe(p: Principal) -> dict:
        # Scalar final_caller bypasses identities; resource ORM rows also need
        # fresh columns (including references/rights flags) on every observation.
        db.expire_all()
        with db.no_autoflush:
            return read(p)

    def ids(select_ids, data):
        return sorted({uuid.UUID(str(value)) for value in select_ids(data)})

    for attempt in range(2):
        try:
            candidate = observe(principal)
        except ApiError:
            caller()  # never conceal expired auth behind an initial resource miss
            raise
        items, collections = ids(item_ids, candidate), ids(collection_ids, candidate)
        # Each lock SELECT retains the SQL ACL, including when a candidate lost
        # permission while waiting. Never lock a foreign private item.
        try:
            db.execute(text(f"SET LOCAL lock_timeout = {int(get_settings().recovery.gate_lock_timeout_ms)}"))
            if items:
                db.execute(select(LibraryItem.id).where(LibraryItem.id.in_(items),
                    visible_items_clause(principal)).order_by(LibraryItem.id).with_for_update(read=True)).all()
            if collections:
                db.execute(select(Collection.id).where(Collection.id.in_(collections),
                    Collection.owner_user_id == principal.user_id).order_by(Collection.id)
                    .with_for_update(read=True)).all()
            if citations:
                lock_citations(db, exclusive=False)
        except DBAPIError:
            raise ApiError(503, "service_recovering") from None
        current = caller()
        try:
            fresh = observe(current)
            stable = observe(current)
        except ApiError:
            caller()
            raise
        final = caller()  # fresh clock after ALL potentially slow resource SQL
        if (ids(item_ids, fresh) == items and ids(collection_ids, fresh) == collections
                and fresh == stable and current == final):
            return stable
        # No new resource lock after journal. Start the bounded retry in a new
        # transaction, discard every DTO/proof from the previous one.
        db.rollback()
        principal = final
    raise ApiError(409, "resource_scope_changed")
