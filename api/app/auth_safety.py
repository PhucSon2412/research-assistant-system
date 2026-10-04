"""C1 foundation. No public recovery/login flow and no cutover bypass."""
from dataclasses import dataclass, field
from datetime import datetime
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app import journal
from app.models import AuthChangeReceipt, AuthSafetyHead, AuthSafetyState, User, RevocationJournalRecord


def cutover(db: Session) -> str:
    state = db.get(AuthSafetyState, 1)
    if state is None or state.version != 1:
        return "auth_safety_version_missing"
    # C4 will introduce the completed contract. C1 cannot set this ready.
    return "auth_safety_pending"


def require_writer(db: Session) -> None:
    from app.api.errors import ApiError
    from app.config import get_settings
    from app import recovery
    # Independent integrity check: C2 test-only pending patches cannot conceal a bad chain/hold.
    if not recovery.evaluate_integrity(get_settings(), db.get_bind()).open:
        raise ApiError(503, "service_recovering")
    if cutover(db) != "ready":
        raise ApiError(503, "service_recovering")


def require_final(db: Session, *, writer: bool = False, pending_endings=()) -> None:
    """Final gate on this transaction, AFTER all row locks; never a second connection.

    The shared journal lock is retained until commit/rollback. No User lock may
    follow it. Read committed observes hold/journal changes that won while waiting.
    """
    from app.api.errors import ApiError
    from app.config import get_settings
    from app import recovery
    try:
        db.execute(text(f"SET LOCAL lock_timeout = {int(get_settings().recovery.gate_lock_timeout_ms)}"))
        lock = "pg_advisory_xact_lock" if writer else "pg_advisory_xact_lock_shared"
        db.execute(text(f"SELECT {lock}(:key)"), {"key": recovery.JOURNAL_LOCK})
        state = journal.read(get_settings().recovery.journal_path)
        view = list(recovery._db_view(db))
        # Only this transaction's queued closures are awaiting their mirror. The
        # binding/closure constraints are forced BEFORE append in record_auth_endings.
        # Other unjournaled rows, unconfirmed records, holds and file changes remain fatal.
        from app.models import UserSession, PasswordResetToken, Invitation
        from sqlalchemy import func
        for rows in pending_endings:
            if not rows:
                continue
            kind = type(rows[0])
            slot, column = {UserSession: (7, RevocationJournalRecord.session_id),
                            PasswordResetToken: (8, RevocationJournalRecord.reset_token_id),
                            Invitation: (9, RevocationJournalRecord.invitation_id)}[kind]
            ids = {row.id for row in rows}
            waiting = db.scalar(select(func.count()).select_from(kind).where(kind.id.in_(ids),
                ~select(RevocationJournalRecord.seq).where(column == kind.id).exists()))
            view[slot] -= waiting
        if not recovery.compare(state, tuple(view)).open or cutover(db) != "ready":
            raise ApiError(503, "service_recovering")
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, "service_recovering") from None


def account_live(db: Session, user_id: uuid.UUID) -> bool:
    return bool(db.scalar(text("SELECT ras_auth_account_live(:uid)"), {"uid": user_id}))


@dataclass(frozen=True)
class Change:
    user_id: uuid.UUID
    domain: str
    reason_code: str
    recorded_at: datetime
    decision: str = "changed"
    verifier: str | None = field(default=None, repr=False)
    policy_version: int | None = None


def fresh_ids(records, reserved=()) -> tuple[uuid.UUID, uuid.UUID]:
    """Caller holds journal lock after row locks; include all history, even absent/rollback."""
    seen = set(reserved)
    for r in records:
        if r.kind in journal.AUTH_KINDS:
            seen.update((r.body["operation_id"], r.body["generation"]))
    chosen = []
    for _ in range(64):
        value = uuid.uuid4()  # OS CSPRNG; never derive from the snapshot.
        if str(value) not in seen:
            chosen.append(value)
            seen.add(str(value))
            if len(chosen) == 2:
                return tuple(chosen)
    raise journal.JournalError("auth_uuid_collision")


def stage(db: Session, state, changes: list[Change]):
    """Internal direct foundation: ALL User/FK/audit locks already held, journal held.

    C2 will call this from converted identity writers. Does not commit or append.
    No secret is present in returned records, receipts, projection or exceptions.
    """
    records = []
    reserved = set()
    for change in changes:
        if change.domain not in ("credential", "status"):
            raise journal.JournalError("auth_domain_invalid")
        operation, generation = fresh_ids(state.records, reserved)
        reserved.update((str(operation), str(generation)))
        body = {"user_id": str(change.user_id), "operation_id": str(operation), "generation": str(generation),
                "reason_code": change.reason_code, "recorded_at": change.recorded_at.isoformat()}
        kind = journal.CREDENTIAL_TRANSITION
        if change.domain == "status":
            kind = journal.ACCOUNT_STATUS_DECISION
            body["decision"] = change.decision
        record = journal.next_record(records[-1] if records else state.head, kind, body)
        db.execute(text("SELECT ras_auth_mirror(:line,'close','auth_changed',false)"), {"line": record.line})
        db.execute(text("SELECT ras_auth_project(:uid)"), {"uid": change.user_id})
        db.execute(text("SELECT ras_auth_change(:uid,:op,:verifier,:policy)"),
                   {"uid": change.user_id, "op": operation, "verifier": change.verifier, "policy": change.policy_version})
        records.append(record)
    # Binding checks must pass before the first journal byte, not merely at commit.
    db.execute(text("SET CONSTRAINTS trg_auth_user_binding, trg_auth_receipt_binding, fk_users_credential_receipt, fk_users_status_receipt IMMEDIATE"))
    db.execute(text("SET CONSTRAINTS trg_auth_user_binding, trg_auth_receipt_binding, fk_users_credential_receipt, fk_users_status_receipt DEFERRED"))
    return records


def verify_snapshot(db: Session) -> None:
    """Validate historical receipt bindings before restrictions, without granting anything."""
    from app.recovery import RecoveryError
    for u in db.scalars(select(User)):
        for domain in ("credential", "status"):
            operation = getattr(u, f"{domain}_operation_id")
            generation = getattr(u, f"{domain}_generation")
            if operation is None:
                if domain == "credential" and generation is not None:
                    raise RecoveryError("auth_receipt_missing")
                continue
            r = db.get(AuthChangeReceipt, operation)
            j = db.scalar(select(RevocationJournalRecord).where(RevocationJournalRecord.auth_operation_id == operation))
            if r is None or j is None or (r.user_id, r.domain, r.generation, r.decision, r.reason_code, r.recorded_at) != (
                    u.id, domain, generation, j.auth_decision, j.auth_reason_code, j.auth_recorded_at) or j.auth_user_id != u.id or j.auth_domain != domain:
                raise RecoveryError("auth_receipt_binding_corrupt")
            if domain == "credential" and u.password_changed_at != r.recorded_at:
                raise RecoveryError("auth_credential_time_corrupt")
            if domain == "status" and u.status != r.decision:
                raise RecoveryError("auth_status_receipt_corrupt")


def verify(db: Session, records) -> None:
    from app.recovery import RecoveryError
    verify_snapshot(db)
    latest = {}
    for r in records:
        if r.kind in journal.AUTH_KINDS:
            domain = "credential" if r.kind == journal.CREDENTIAL_TRANSITION else "status"
            latest[(uuid.UUID(r.body["user_id"]), domain)] = r
    expected_users = {u for u, _ in latest}
    if set(db.scalars(select(AuthSafetyHead.user_id))) != expected_users:
        raise RecoveryError("auth_projection_incomplete")
    for (uid, domain), r in latest.items():
        h = db.get(AuthSafetyHead, uid)
        if (getattr(h, f"{domain}_seq"), str(getattr(h, f"{domain}_operation_id")), str(getattr(h, f"{domain}_generation")),
            getattr(h, f"{domain}_reason_code"), getattr(h, f"{domain}_recorded_at")) != (
            r.seq, r.body["operation_id"], r.body["generation"], r.body["reason_code"], datetime.fromisoformat(r.body["recorded_at"])):
            raise RecoveryError("auth_projection_corrupt")
        if domain == "status" and h.status_decision != r.body["decision"]:
            raise RecoveryError("auth_projection_corrupt")
    for u in db.scalars(select(User)):
        h = db.get(AuthSafetyHead, u.id)
        if u.credential_state == "usable" and (h is None or (u.credential_operation_id,u.credential_generation) != (h.credential_operation_id,h.credential_generation)):
            raise RecoveryError("auth_credential_not_restricted")
        if u.status_safety == "proven" and (h is None or (u.status_generation,u.status) != (h.status_generation,h.status_decision)):
            raise RecoveryError("auth_status_not_restricted")
        if h is not None and h.status_decision == "disabled" and u.status != "disabled":
            raise RecoveryError("auth_disable_not_applied")
        if u.credential_generation is None and u.credential_state != "quarantined":
            raise RecoveryError("auth_legacy_usable")
        if u.status_generation is None and u.status_safety != "held":
            raise RecoveryError("auth_legacy_proven")
