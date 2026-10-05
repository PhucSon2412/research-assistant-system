"""Read boundary for nine review/publication JSON GETs, never their writers.

Review locks: request UUIDs -> permitted private item UUIDs -> journal.
Common locks: permitted common item UUIDs -> journal. All production writers
of the common tuple/rights/index/retention take the item first. Cleanup takes
tombstone then item; readers deliberately do not lock tombstones. Auxiliary
status/duplicate reads are refreshed observations, not a global snapshot.
"""
from collections.abc import Callable
from datetime import datetime, timedelta
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.authz import (Principal, m2a_private_derivatives_clause, pending_publication_clause,
                      review_copy_admin_clause, review_grant_clause, review_history_admin_clause)
from app.config import get_settings
from app.identity.final_caller import final_caller
from app.models import LibraryItem, PaperVersion, ReviewRequest, SourceFile


def _scope(data: dict, review: bool) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    if review:
        rows = data.get("requests", [data["request"]] if "request" in data else [])
        requests = sorted({uuid.UUID(row["id"]) for row in rows})
        items = sorted({uuid.UUID(row["item"]["id"]) for row in rows if "item" in row})
        return requests, items
    rows = data.get("items", [data["item"]] if "item" in data else [])
    return [], sorted({uuid.UUID(row["id"]) for row in rows})


def _lock_resources(db: Session, principal: Principal, requests, items, *, review: bool,
                    admin: bool, pending: bool) -> tuple[list, list]:
    db.execute(text(f"SET LOCAL lock_timeout = {int(get_settings().recovery.gate_lock_timeout_ms)}"))
    locked_requests = []
    if requests:
        stmt = select(ReviewRequest.id).where(ReviewRequest.id.in_(requests))
        if not admin:
            stmt = stmt.join(LibraryItem, LibraryItem.id == ReviewRequest.item_id).where(
                ReviewRequest.requester_user_id == principal.user_id, m2a_private_derivatives_clause(principal))
        else:
            stmt = stmt.where(review_history_admin_clause(principal))
        locked_requests = list(db.scalars(stmt.order_by(ReviewRequest.id)
                                         .with_for_update(of=ReviewRequest, read=True)))
    locked_items = []
    if items:
        stmt = select(LibraryItem.id).where(LibraryItem.id.in_(items))
        if review and admin:
            # Only the exact grant can lock a private item. Closed history keeps
            # its request lock without acquiring any private resource lock.
            granted = (select(ReviewRequest.item_id)
                       .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
                       .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
                       .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
                       .where(ReviewRequest.id.in_(requests), review_grant_clause(principal)).correlate(None))
            stmt = stmt.where(LibraryItem.id.in_(granted))
        elif review:
            stmt = stmt.where(m2a_private_derivatives_clause(principal))
        else:
            stmt = stmt.where(pending_publication_clause(principal) if pending
                              else review_copy_admin_clause(principal))
        locked_items = list(db.scalars(stmt.order_by(LibraryItem.id)
                                      .with_for_update(of=LibraryItem, read=True)))
    return locked_requests, locked_items


def review_publication_json(db: Session, principal: Principal, *, clock: Callable[[], datetime],
                            idle: timedelta, read: Callable[[Principal], dict], review: bool = False,
                            admin: bool = False, pending: bool = False) -> dict:
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("review/publication read requires a clean session")

    def caller() -> Principal:
        proof = final_caller(db, session_id=principal.session_id, user_id=principal.user_id,
                             clock=clock, idle=idle, admin=admin)
        return Principal(proof.user_id, proof.role, proof.session_id, proof.email, proof.display_name)

    def observe(p: Principal) -> dict:
        db.expire_all()
        with db.no_autoflush:
            return read(p)

    for attempt in range(2):
        try:
            candidate = observe(principal)
        except ApiError:
            caller()
            raise
        scope = _scope(candidate, review)
        try:
            locked = _lock_resources(db, principal, *scope, review=review, admin=admin, pending=pending)
        except DBAPIError:
            raise ApiError(503, "service_recovering") from None
        current = caller()
        try:
            fresh = observe(current)
            stable = observe(current)
        except ApiError:
            caller()  # auth loss takes precedence over a late resource miss
            raise
        final = caller()  # clock AFTER every resource query/lock wait
        if _scope(fresh, review) == locked == scope and fresh == stable and current == final:
            return stable
        # Never acquire another resource lock after the journal. Drop proof/DTO
        # and all locks before the single bounded retry, as in C2d-2a.
        db.rollback()
        principal = final
    raise ApiError(409, "resource_scope_changed")
