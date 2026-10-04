"""Runtime bootstrap: hash outside transactions; bootstrap -> FK/audit -> journal.

Independent intent survives an absent/rolled-back account. No force/recreate or
success after an uncertain commit. Recovery never recreates the initial account.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from sqlalchemy import Engine, exists, select, text
from sqlalchemy.orm import Session
from app import auth_safety, journal
from app.api.errors import ApiError
from app.clock import utcnow
from app.config import PasswordPolicy, get_settings
from app.identity.emails import normalise_email
from app.identity.passwords import hasher_for_policy, validate_password
from app.identity.predicates import lock_users
from app.identity.session_commit import AUTH_PENDING, commit_auth
from app.models import BootstrapState, User, AuthChangeReceipt

AUDIT_ACTION = "admin.bootstrap"
_ADVISORY_LOCK_KEY = 0x524153424F4F5431


class BootstrapRefused(Exception):
    """Existing account or independent history: bootstrap cannot repair it."""


@dataclass(frozen=True)
class BootstrapResult:
    user_id: uuid.UUID
    email: str = field(repr=False)


def _history_is_new(state) -> bool:
    # Even a complete credential-only prefix is intent. Recovery records carry
    # no account creation; an existing/rebuilt genesis is never a new install.
    return (state.records[0].body["mode"] == "new_install"
            and all(r.kind in (journal.GENESIS, journal.RECOVERY_BEGIN, journal.RECOVERY_END)
                    for r in state.records))


def _require_new(db):
    state = journal.read(get_settings().recovery.journal_path)
    if (db.scalar(select(exists().select_from(User)))
            or db.scalar(select(exists().select_from(BootstrapState)))
            or not _history_is_new(state)):
        raise BootstrapRefused("bootstrap unavailable for this installation")


def bootstrap_admin(engine: Engine, *, email: str, password: str, policy: PasswordPolicy,
                    display_name: str | None = None, clock=utcnow) -> BootstrapResult:
    email = normalise_email(email)
    validate_password(password, policy, email=email)
    with Session(engine) as db:
        auth_safety.require_writer(db)
        _require_new(db)
        hash_policy = dict(db.execute(text("SELECT * FROM auth_hash_policies ORDER BY version DESC LIMIT 1")).mappings().one())
        db.rollback()
    verifier = hasher_for_policy(hash_policy).hash(password)
    uid = uuid.uuid4()
    with Session(engine) as db:
        auth_safety.require_writer(db)
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        # Recheck after hashing/waiting: another bootstrap, hold or journal change.
        auth_safety.require_writer(db)
        _require_new(db)
        at = clock()
        db.execute(text("SELECT ras_auth_bootstrap_prepare(:uid,:email,:name,:verifier,:policy,:at)"),
                   {"uid": uid, "email": email, "name": display_name, "verifier": verifier,
                    "policy": hash_policy["version"], "at": at})
        db.info[AUTH_PENDING] = [
            auth_safety.Change(uid, "credential", "bootstrap", at,
                               verifier=verifier, policy_version=hash_policy["version"]),
            auth_safety.Change(uid, "status", "bootstrap", at, decision="active"),
        ]
        db.info["auth_bootstrap_guard"] = _guard_history
        batch = commit_auth(db)
        operations = {line.record.kind: uuid.UUID(line.record.body["operation_id"])
                      for line in batch if line.record.kind in journal.AUTH_KINDS
                      and line.record.body["user_id"] == str(uid)}
        db.rollback()
        # Postconditions in a fresh transaction, locks before final journal read.
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        lock_users(db, [uid])
        auth_safety.require_final(db)
        # SQL DEBUG result logging must never receive verifier/email columns.
        user = db.execute(select(User.id, User.role, User.credential_operation_id,
                                 User.status_operation_id).where(User.id == uid)).first()
        singleton = db.get(BootstrapState, True, populate_existing=True)
        if (not user or user.role != "admin" or not auth_safety.account_live(db, uid)
                or not singleton or singleton.admin_user_id != uid
                or singleton.completed_at != at or singleton.creation_transaction_id is None
                or operations != {journal.CREDENTIAL_TRANSITION: user.credential_operation_id,
                                  journal.ACCOUNT_STATUS_DECISION: user.status_operation_id}):
            raise ApiError(503, "auth_outcome_uncertain")
        for kind, operation in operations.items():
            receipt = db.get(AuthChangeReceipt, operation)
            domain = "credential" if kind == journal.CREDENTIAL_TRANSITION else "status"
            if (not receipt or (receipt.user_id, receipt.domain, receipt.reason_code, receipt.recorded_at,
                                receipt.transaction_id) != (uid, domain, "bootstrap", at,
                                                            singleton.creation_transaction_id)):
                raise ApiError(503, "auth_outcome_uncertain")
        return BootstrapResult(uid, email)


def _guard_history(db):
    # User/singleton now exist in this transaction: check the independent file.
    if not _history_is_new(journal.read(get_settings().recovery.journal_path)):
        raise BootstrapRefused("bootstrap unavailable for this installation")
