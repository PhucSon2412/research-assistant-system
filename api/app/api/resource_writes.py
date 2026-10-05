"""Six resource writers: User -> resources -> flush -> exclusive journal.

No auth event is appended. Commit exceptions never imply rollback; response
authority is obtained on a new transaction, and every exit releases it.
"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety
from app.api.errors import ApiError
from app.api.resource_reads import resource_json
from app.authz import Principal
from app.config import get_settings
from app.identity.predicates import lock_users, session_live
from app.identity.tokens import digest_matches
from app.models import User, UserSession


@dataclass
class Mutation:
    check: Callable[[Principal], None]
    read: Callable[[Principal], dict] | None = None
    item_ids: Callable[[dict], Iterable[str]] = lambda data: ()
    collection_ids: Callable[[dict], Iterable[str]] = lambda data: ()


def _actor(db: Session, principal: Principal, csrf_token: str,
           clock: Callable[[], datetime], idle: timedelta) -> Principal:
    # Scalar reads bypass old ORM/profile/role identities. No row lock or touch.
    row = db.execute(select(User.role, User.email, User.display_name, UserSession.csrf_token_hash)
        .join(UserSession, UserSession.user_id == User.id)
        .where(User.id == principal.user_id, UserSession.id == principal.session_id)).one_or_none()
    at = clock()  # after waits, resource SQL, and profile/CSRF SQL
    if row is None or not session_live(db, principal.session_id, at, idle, user_id=principal.user_id):
        raise ApiError(401, "session_expired", clear_session=True)
    if not digest_matches(csrf_token, row.csrf_token_hash):
        raise ApiError(403, "csrf_failed")
    return Principal(principal.user_id, row.role, principal.session_id, row.email, row.display_name)


def resource_write(db: Session, principal: Principal, *, csrf_token: str,
                   clock: Callable[[], datetime], idle: timedelta,
                   mutate: Callable[[Principal], Mutation]) -> dict | None:
    committed = False
    commit_started = False
    try:
        auth_safety.require_writer(db)  # separate integrity connection ONLY at entry
        db.execute(text(f"SET LOCAL lock_timeout = {int(get_settings().recovery.gate_lock_timeout_ms)}"))
        lock_users(db, [principal.user_id])
        current = _actor(db, principal, csrf_token, clock, idle)
        mutation = mutate(current)  # ACL row locks, DML and audit/FK before journal
        db.flush()
        auth_safety.require_final(db, writer=True)  # direct exclusive; never shared upgrade
        db.expire_all()
        with db.no_autoflush:
            current = _actor(db, principal, csrf_token, clock, idle)
            mutation.check(current)  # SQL resource gate, no new row locks or DML
            _actor(db, principal, csrf_token, clock, idle)  # fresh clock after all resource SQL
        commit_started = True
        db.commit()
        committed = True
        db.expire_all()
        # All precommit rows/proofs have expired. Reads/204 use a new transaction.
        if mutation.read is None:
            auth_safety.require_final(db)
            _actor(db, principal, csrf_token, clock, idle)
            return None
        response = resource_json(db, principal, clock=clock, idle=idle, read=mutation.read,
            item_ids=mutation.item_ids, collection_ids=mutation.collection_ids)
        _actor(db, principal, csrf_token, clock, idle)
        return response
    except ApiError as exc:
        if commit_started and not committed:
            raise ApiError(503, "resource_write_outcome_uncertain") from None
        if committed and exc.status_code == 404:
            raise ApiError(409, "resource_scope_changed") from None
        raise
    except Exception:
        code = ("resource_write_response_unavailable" if committed else
                "resource_write_outcome_uncertain" if commit_started else "resource_write_unavailable")
        raise ApiError(503, code) from None
    finally:
        # Rollback here cleans local state/locks, not a claim about remote commit.
        try:
            db.rollback()
        except Exception:
            db.close()
