"""Shared auth reads and ordered runtime locks. Paper ACL remains separate."""
from sqlalchemy import select, text, func
from app.models import UserSession


def lock_users(db, user_ids):
    tx = db.get_transaction()
    if db.info.get("auth_lock_transaction") is not tx or tx is None:
        db.info["auth_locked_users"] = set()
    known = db.info.setdefault("auth_locked_users", set())
    wanted = set(user_ids)
    if not wanted <= known:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": sorted(wanted | known)})
        db.info["auth_lock_transaction"] = db.get_transaction()
        known.update(wanted)


def live_clause(now, idle):
    return func.ras_auth_session_live(UserSession.id, UserSession.user_id, now, idle)


def session_live(db, session_id, now, idle, *, user_id=None):
    stmt = select(UserSession.id).where(UserSession.id == session_id, live_clause(now, idle))
    if user_id is not None:
        stmt = stmt.where(UserSession.user_id == user_id)
    return db.scalar(stmt) is not None
