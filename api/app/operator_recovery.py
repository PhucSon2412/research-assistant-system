"""C3 operator recovery of an existing account's credential or initial status (CLI only, no HTTP).

Trust, stated plainly: the recovery operator is the project owner or a person
they designate directly, and the same person may approve and execute (MVP).
They, and whoever provisions the operator login and registers cases with the
migrator connection, are trusted to have checked the person behind the User
UUID outside the app. Software only enforces what follows from that: the real
DB login (``session_user``) maps to the case's operator UUID; the case binds
action, installation, User, role and expected latest heads; it is used once
(journal-wide, not only in a restorable DB row) inside its time window; the
journal/DB are reconciled and held for maintenance; credential and status are
separate decisions. Host/DB access, email, an old cookie or the snapshot
password never prove ownership of a User UUID. A malicious owner/DBA is out of
scope.

Lock order: User NO KEY UPDATE -> open tokens (UUID) -> open sessions (UUID) ->
journal exclusive; no row lock after the journal. No DB connection or lock is
held across the TTY prompt, Argon2 hashing or case-file reads. Success is
reported only after a known commit, a confirmed (fsynced) batch and a fresh
proof transaction; an operation ID alone never proves a commit.
"""
from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine, text

from app import journal, recovery
from app.auth_safety import fresh_ids
from app.config import Settings
from app.journal import JournalError
from app.operator_cases import RecoveryCase

SET_CREDENTIAL, RESOLVE_STATUS = journal.CASE_ACTIONS


class OperatorRefused(Exception):
    """Nothing was written by this attempt; ``code`` is safe to print."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OperatorOutcomeError(Exception):
    """Not a success: the journal and/or DB may hold this attempt. Inspect/recover; never retry blindly."""

    def __init__(self, code: str, *, committed: bool | None):
        super().__init__(code)
        self.code = code
        self.committed = committed  # True known, False known not, None uncertain


@dataclass(frozen=True)
class Hold:
    state: journal.JournalState = field(repr=False)
    recovery_id: str
    begin_seq: int


@dataclass(frozen=True)
class Snapshot:
    user_id: uuid.UUID
    role: str
    status: str
    status_safety: str
    credential_state: str
    credential_operation_id: uuid.UUID | None
    credential_generation: uuid.UUID | None
    status_operation_id: uuid.UUID | None
    status_generation: uuid.UUID | None
    password_verifier: str | None = field(repr=False)
    password_hash_policy_version: int
    email_digest: str = field(repr=False)
    head_credential_operation_id: uuid.UUID | None
    head_status_operation_id: uuid.UUID | None
    head_status_decision: str | None
    status_live: bool
    policy: dict = field(repr=False)
    token_ids: tuple = ()
    session_ids: tuple = ()

    def key(self):
        """Everything the decision depends on; any change aborts (no automatic retry)."""
        return (self.user_id, self.role, self.status, self.status_safety, self.credential_state,
                self.credential_operation_id, self.credential_generation, self.status_operation_id,
                self.status_generation, self.password_verifier, self.password_hash_policy_version,
                self.head_credential_operation_id, self.head_status_operation_id, self.head_status_decision,
                self.status_live, self.policy["version"])


@dataclass(frozen=True)
class OperatorResult:
    action: str
    case_id: uuid.UUID
    user_id: uuid.UUID
    operation_id: uuid.UUID
    generation: uuid.UUID
    credential_state: str
    status: str
    status_safety: str
    account_live: bool


class ConnectionReleaseFailed(OperatorRefused):
    """Rollback/close of a finished phase failed. The connection was invalidated and returned, but
    the phase is not clean evidence: callers stop and classify by their own Attempt facts."""

    def __init__(self):
        super().__init__("connection_release_failed")


def _release(conn) -> bool:
    """Always hand the connection back; a failed rollback/close invalidates it (never pooled dirty).
    Returns False when the release was not clean, even if the invalidate/close fallback succeeded."""
    try:
        conn.rollback()
        conn.close()
        return True
    except Exception:  # noqa: BLE001 - reported to the caller, never swallowed as success
        try:
            conn.invalidate()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return False


@contextmanager
def connection(engine: Engine):
    """A phase's connection. The body's own error wins (a cleanup error never masks it); a clean body
    followed by an unclean release raises ConnectionReleaseFailed instead of looking successful."""
    conn = engine.connect()
    try:
        yield conn
    except BaseException:
        _release(conn)
        raise
    if not _release(conn):
        raise ConnectionReleaseFailed()


def _timeout(conn, settings: Settings) -> None:
    conn.execute(text(f"SET LOCAL lock_timeout = {int(settings.auth_recovery.lock_timeout_ms)}"))


def whoami(conn) -> uuid.UUID:
    try:
        return conn.scalar(text("SELECT ras_auth_operator_whoami()"))
    except Exception:  # noqa: BLE001 - a failed probe is never authority
        raise OperatorRefused("operator_authority_required") from None


def read_journal(settings: Settings) -> journal.JournalState:
    if settings.recovery.journal_path is None:
        raise OperatorRefused("gate_journal_not_configured")
    try:
        return journal.read(settings.recovery.journal_path)
    except JournalError as exc:
        raise OperatorRefused(f"gate_{exc.code}") from None


def gate_view(conn):
    return conn.execute(text("SELECT * FROM ras_auth_operator_gate()")).one()


def check_hold(conn, settings: Settings) -> Hold:
    """Maintenance hold: everything the recovery gate checks passed except the open recovery itself,
    and that recovery was reconciled by ``recover --hold-for-operator`` (progress past its begin)."""
    view = gate_view(conn)
    state = read_journal(settings)
    status = recovery.compare(state, tuple(view[:10]))
    # compare() checks recovery_in_progress last: reaching it means no torn/missing/diverged/behind/
    # unconfirmed/unjournaled condition was found. Hold or pending never excuse any of those.
    if status.reason != "recovery_in_progress":
        raise OperatorRefused(f"gate_{status.reason}")
    begin = next(r for r in reversed(state.records)
                 if r.kind == journal.RECOVERY_BEGIN and r.body["recovery_id"] == state.open_recovery)
    if not view.backfill_complete or view.projection_seq is None or view.projection_seq < begin.seq:
        raise OperatorRefused("hold_not_reconciled")
    return Hold(state, state.open_recovery, begin.seq)


def shared_hold(conn, settings: Settings) -> Hold:
    _timeout(conn, settings)
    conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": recovery.JOURNAL_LOCK})
    return check_hold(conn, settings)


def journal_heads(state: journal.JournalState, user_id: uuid.UUID):
    credential = status = None
    for r in state.records:
        if r.kind in journal.AUTH_KINDS and r.body["user_id"] == str(user_id):
            if r.kind == journal.CREDENTIAL_TRANSITION:
                credential = r
            else:
                status = r
    return credential, status


def case_used(state: journal.JournalState, case_id: uuid.UUID) -> bool:
    return any(r.kind == journal.OPERATOR_RECOVERY_CASE and r.body["case_id"] == str(case_id) for r in state.records)


def check_window(case: RecoveryCase, at: datetime, settings: Settings) -> None:
    if case.expires_at - case.approved_at > settings.auth_recovery.case_max_lifetime:
        raise OperatorRefused("case_lifetime_exceeds_policy")
    if not case.approved_at <= at < case.expires_at:
        raise OperatorRefused("case_expired" if at >= case.expires_at else "case_not_yet_valid")


def registered_case(conn, case: RecoveryCase) -> None:
    row = conn.execute(text("SELECT * FROM ras_auth_operator_case(:c)"), {"c": case.case_id}).first()
    if row is None:
        raise OperatorRefused("case_not_registered")
    if RecoveryCase.from_row(row) != case:
        raise OperatorRefused("case_mismatch")


def snapshot(conn, case: RecoveryCase, at: datetime, *, lock: bool) -> Snapshot | None:
    try:
        row = conn.execute(text("SELECT * FROM ras_auth_operator_snapshot(:c,:at,:lock)"),
                           {"c": case.case_id, "at": at, "lock": lock}).first()
    except Exception as exc:  # noqa: BLE001
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "55P03":
            raise OperatorRefused("target_busy") from None
        raise OperatorRefused("case_unavailable") from None
    if row is None:
        return None
    m = row._mapping
    policy = {k: m[k] for k in ("memory_kib", "time_cost", "parallelism", "salt_bytes", "hash_bytes")}
    policy["version"] = m["policy_version"]
    return Snapshot(m["user_id"], m["role"], m["status"], m["status_safety"], m["credential_state"],
                    m["credential_operation_id"], m["credential_generation"], m["status_operation_id"],
                    m["status_generation"], m["password_verifier"], m["password_hash_policy_version"],
                    m["email_digest"], m["head_credential_operation_id"], m["head_status_operation_id"],
                    m["head_status_decision"], m["status_live"], policy,
                    tuple(m["token_ids"] or ()), tuple(m["session_ids"] or ()))


def _op(record):
    return uuid.UUID(record.body["operation_id"]) if record is not None else None


def check_target(case: RecoveryCase, snap: Snapshot | None, state: journal.JournalState) -> None:
    """Same User UUID only (never email); role/heads exactly as approved, NULL-safe (NULL is declared, not a wildcard)."""
    if snap is None:
        raise OperatorRefused("user_absent")
    if snap.role != case.expected_role:
        raise OperatorRefused("role_mismatch")
    credential, status = journal_heads(state, case.user_id)
    if (_op(credential), _op(status)) != (snap.head_credential_operation_id, snap.head_status_operation_id):
        raise OperatorRefused("projection_mismatch")
    if _op(credential) != case.expected_credential_operation_id:
        raise OperatorRefused("case_stale_credential_head")
    if _op(status) != case.expected_status_operation_id:
        raise OperatorRefused("case_stale_status_head")
    if case.action == RESOLVE_STATUS:
        if snap.status != "active":
            raise OperatorRefused("status_disabled")
        if status is not None and status.body["decision"] == "disabled":
            raise OperatorRefused("latest_status_disabled")
        if snap.status_safety != "held" or snap.status_live:
            raise OperatorRefused("status_already_proven")


@dataclass(frozen=True)
class Prepared:
    case: RecoveryCase
    hold: Hold = field(repr=False)
    snap: Snapshot = field(repr=False)


def prepare(engine: Engine, settings: Settings, case: RecoveryCase, *, user_id: uuid.UUID,
            installation: uuid.UUID, action: str, clock) -> Prepared:
    """Short read-only proof; the connection is released before any prompt/hash."""
    if (case.user_id, case.journal_id, case.action) != (user_id, installation, action):
        raise OperatorRefused("case_target_mismatch")
    with connection(engine) as conn:
        conn.begin()
        me = whoami(conn)
        if me != case.operator_id:
            raise OperatorRefused("case_operator_mismatch")
        hold = shared_hold(conn, settings)
        if hold.state.journal_id != case.journal_id:
            raise OperatorRefused("case_installation_mismatch")
        # The CURRENT independent journal decides reuse, even when a restored DB lacks/forgot the case.
        if case_used(hold.state, case.case_id):
            raise OperatorRefused("case_already_used")
        registered_case(conn, case)
        at = clock()
        check_window(case, at, settings)
        snap = snapshot(conn, case, at, lock=False)
        check_target(case, snap, hold.state)
        return Prepared(case, hold, snap)


def check_password(prepared: Prepared, password: str, settings: Settings) -> None:
    """Outside any transaction. Refuse the snapshot verifier; lost historical passwords cannot be checked."""
    from app.identity.passwords import PasswordPolicyError, validate_password, verify_password
    validate_password(password, settings.password)
    if hashlib.sha256(password.strip().lower().encode("utf-8")).hexdigest() == prepared.snap.email_digest:
        raise PasswordPolicyError("password must not equal the email address")
    if prepared.snap.password_verifier and verify_password(prepared.snap.password_verifier, password):
        raise PasswordPolicyError("new password must differ from the current (quarantined) password")


def hash_password(prepared: Prepared, password: str) -> str:
    from app.identity.passwords import hasher_for_policy
    return hasher_for_policy(prepared.snap.policy).hash(password)


def _records(case: RecoveryCase, state, snap: Snapshot, at: datetime):
    operation, generation = fresh_ids(state.records)
    stamp = at.isoformat()
    head = journal.next_record(state.head, journal.OPERATOR_RECOVERY_CASE, {
        "case_id": str(case.case_id), "operator_id": str(case.operator_id), "user_id": str(case.user_id),
        "action": case.action, "operation_id": str(operation), "recorded_at": stamp})
    body = {"user_id": str(case.user_id), "operation_id": str(operation), "generation": str(generation),
            "reason_code": "verified_recovery", "recorded_at": stamp}
    kind = journal.CREDENTIAL_TRANSITION
    if case.action == RESOLVE_STATUS:
        kind = journal.ACCOUNT_STATUS_DECISION
        body.update(decision="active", reason_code="verified_initial_status")
    records = [head, journal.next_record(head, kind, body)]
    for token in snap.token_ids:
        records.append(journal.next_record(records[-1], journal.RESET_TOKEN_CLOSURE, {
            "token_id": str(token), "user_id": str(case.user_id), "end_kind": "revoked",
            "reason_code": "operator_recovery", "ended_at": stamp}))
    for session in snap.session_ids:
        records.append(journal.next_record(records[-1], journal.SESSION_REVOCATION, {
            "session_id": str(session), "user_id": str(case.user_id), "reason_code": "operator_recovery",
            "revoked_at": stamp}))
    return operation, generation, records


@dataclass
class Attempt:
    """Outcome facts independent of teardown: rollback/close never erase commit_started/committed."""
    append_started: bool = False
    written: bool = False
    commit_started: bool = False
    committed: bool = False
    rolled_back: bool = False


def _recheck(conn, case: RecoveryCase, settings: Settings, clock) -> None:
    if whoami(conn) != case.operator_id:
        raise OperatorRefused("case_operator_mismatch")
    check_window(case, clock(), settings)


def mutate(engine: Engine, settings: Settings, prepared: Prepared, *, verifier: str | None, clock,
           attempt: Attempt | None = None, append=journal.append):
    """User -> tokens -> sessions -> journal exclusive -> stage -> append/fsync -> confirm -> commit."""
    case, snap0 = prepared.case, prepared.snap
    attempt = attempt or Attempt()
    try:
        return _mutate(engine, settings, prepared, case, snap0, verifier, clock, attempt, append)
    except ConnectionReleaseFailed:
        # Classified by the attempt's own facts; teardown never turns a commit into "not committed".
        if attempt.committed:
            if not attempt.written:
                raise OperatorOutcomeError("auth_journal_unavailable", committed=True) from None
            raise OperatorOutcomeError("connection_release_failed", committed=True) from None
        if attempt.commit_started:
            raise OperatorOutcomeError("auth_outcome_uncertain", committed=None) from None
        if attempt.append_started:
            raise OperatorOutcomeError("rolled_back_after_journal", committed=False) from None
        raise


def _mutate(engine, settings, prepared, case, snap0, verifier, clock, attempt, append):
    path = settings.recovery.journal_path
    with connection(engine) as conn:
        trans = conn.begin()
        try:
            _timeout(conn, settings)
            _recheck(conn, case, settings, clock)
            snap = snapshot(conn, case, clock(), lock=True)
            if snap is None or snap.key() != snap0.key():
                raise OperatorRefused("target_changed")
            try:
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": recovery.JOURNAL_LOCK})
            except Exception as exc:  # noqa: BLE001
                if getattr(getattr(exc, "orig", None), "sqlstate", None) == "55P03":
                    raise OperatorRefused("journal_lock_busy") from None
                raise
            hold = check_hold(conn, settings)
            if hold.recovery_id != prepared.hold.recovery_id:
                raise OperatorRefused("hold_changed")
            if case_used(hold.state, case.case_id):
                raise OperatorRefused("case_already_used")
            check_target(case, snap, hold.state)
            registered_case(conn, case)
            at = clock()
            check_window(case, at, settings)
            operation, generation, records = _records(case, hold.state, snap, at)
            lines = [r.line for r in records]
            policy = snap.policy["version"] if case.action == SET_CREDENTIAL else None
            try:
                conn.execute(text("SELECT ras_auth_operator_stage(:c,:at,CAST(:lines AS text[]),CAST(:v AS text),CAST(:p AS integer))"),
                             {"c": case.case_id, "at": at, "lines": lines, "v": verifier, "p": policy})
            except Exception:  # noqa: BLE001 - SQL guard refused before any journal byte
                raise OperatorRefused("stage_refused") from None
            attempt.append_started = True  # from here the file may hold any prefix of the batch
            try:
                append(path, hold.state, records)
                attempt.written = True
            except (OSError, JournalError):
                attempt.written = False  # absent/torn/prefix/unsynced: the whole batch stays false
            # Final authority/case window with a fresh clock after EVERY append result (written, absent,
            # torn, prefix or fsync error) and before any COMMIT: a lost authority rolls the decision back,
            # so the journal can only restrict. Valid authority + failed append still commits batch false.
            _recheck(conn, case, settings, clock)
            if attempt.written:
                conn.execute(text("SELECT ras_auth_operator_confirm(:c,CAST(:lines AS text[]))"), {"c": case.case_id, "lines": lines})
                _recheck(conn, case, settings, clock)
            attempt.commit_started = True
            trans.commit()
            attempt.committed = True
        except BaseException as exc:
            if not attempt.commit_started:
                try:
                    trans.rollback()
                    attempt.rolled_back = True
                except Exception:  # noqa: BLE001 - COMMIT was never sent; teardown invalidates
                    pass
                if attempt.append_started:
                    # The file may hold the decision (or a prefix) while the DB rolled back: the gate stays
                    # closed and replay only restricts (credential quarantined, case consumed).
                    raise OperatorOutcomeError("rolled_back_after_journal", committed=False) from None
                if isinstance(exc, OperatorRefused):
                    raise
                if isinstance(exc, Exception):
                    raise OperatorRefused("operator_unavailable") from None
                raise
            if not attempt.committed:
                raise OperatorOutcomeError("auth_outcome_uncertain", committed=None) from None
            raise
    if not attempt.written:
        raise OperatorOutcomeError("auth_journal_unavailable", committed=True)
    return operation, generation, at


def prove(engine: Engine, settings: Settings, case: RecoveryCase, operation: uuid.UUID,
          generation: uuid.UUID, at: datetime, clock) -> OperatorResult:
    """A NEW transaction after a known commit: authority, hold, case record, receipt, pointer and heads."""
    with connection(engine) as conn:
        conn.begin()
        if whoami(conn) != case.operator_id:
            raise OperatorRefused("case_operator_mismatch")
        hold = shared_hold(conn, settings)
        rows = conn.execute(text("SELECT * FROM ras_auth_operator_outcome(:c)"), {"c": case.case_id}).all()
        credential, status = journal_heads(hold.state, case.user_id)
        head = credential if case.action == SET_CREDENTIAL else status
        if len(rows) != 1:
            raise OperatorRefused("outcome_missing")
        m = rows[0]._mapping
        expected = (case.operator_id, True, operation, case.action, case.user_id, at, True, generation, True, True, True)
        actual = (m["case_operator_id"], m["case_confirmed"], m["operation_id"], m["action"], m["user_id"],
                  m["recorded_at"], m["event_confirmed"], m["generation"], m["receipt"], m["current_pointer"],
                  m["head_current"])
        if actual != expected or _op(head) != operation or clock() < at:
            raise OperatorRefused("outcome_mismatch")
        if case.action == SET_CREDENTIAL and m["credential_state"] != "usable":
            raise OperatorRefused("outcome_mismatch")
        if case.action == RESOLVE_STATUS and (m["status"], m["status_safety"]) != ("active", "proven"):
            raise OperatorRefused("outcome_mismatch")
        return OperatorResult(case.action, case.case_id, case.user_id, operation, generation,
                              m["credential_state"], m["status"], m["status_safety"], bool(m["account_live"]))


def run(engine: Engine, settings: Settings, case: RecoveryCase, *, user_id: uuid.UUID, installation: uuid.UUID,
        action: str, clock, read_password=None, attempt: Attempt | None = None, append=journal.append) -> OperatorResult:
    """One decision. ``read_password`` (credential only) runs with no DB connection checked out."""
    prepared = prepare(engine, settings, case, user_id=user_id, installation=installation, action=action, clock=clock)
    verifier = None
    if action == SET_CREDENTIAL:
        password = read_password()
        check_password(prepared, password, settings)
        verifier = hash_password(prepared, password)
        del password
    operation, generation, at = mutate(engine, settings, prepared, verifier=verifier, clock=clock,
                                       attempt=attempt, append=append)
    try:
        return prove(engine, settings, case, operation, generation, at, clock)
    except BaseException:  # noqa: BLE001 - a known commit without fresh proof (even Ctrl-C) is not a success
        raise OperatorOutcomeError("postcommit_proof_failed", committed=True) from None


def inspect(engine: Engine, settings: Settings, user_id: uuid.UUID, case_id: uuid.UUID | None = None) -> dict:
    """Read-only codes/IDs for the operator; never email, verifier, token or evidence."""
    with connection(engine) as conn:
        conn.begin()
        out = {"operator_id": whoami(conn)}
        _timeout(conn, settings)
        conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": recovery.JOURNAL_LOCK})
        view = gate_view(conn)
        state = None
        try:
            state = read_journal(settings)
            gate = recovery.compare(state, tuple(view[:10])).reason
        except OperatorRefused as exc:
            gate = exc.code.removeprefix("gate_")
        out["gate"] = gate
        try:
            check_hold(conn, settings)
            out["maintenance_hold"] = "ready"
        except OperatorRefused as exc:
            out["maintenance_hold"] = exc.code
        out["installation_journal_id"] = view.journal_id
        row = conn.execute(text("SELECT * FROM ras_auth_operator_inspect(:u)"), {"u": user_id}).one()._mapping
        out["user_id"] = user_id
        out["present"] = row["present"]
        for key in ("role", "status", "status_safety", "credential_state", "credential_operation_id",
                    "credential_generation", "status_operation_id", "status_generation", "head_credential_seq",
                    "head_credential_operation_id", "head_status_seq", "head_status_operation_id",
                    "head_status_decision"):
            out[key] = row[key]
        if state is not None:
            credential, status = journal_heads(state, user_id)
            out["journal_credential_head"] = _op(credential)
            out["journal_status_head"] = _op(status)
        out["credential"] = "usable_with_receipt" if row["credential_receipt"] else (
            "quarantined" if row["credential_state"] == "quarantined" else "unproven")
        out["status_proven"] = row["status_live"]
        out["account_live"] = row["account_live"]
        out["initial_status_verified"] = row["initial_status_verified"]
        out["open_sessions"] = row["open_sessions"]
        out["open_tokens"] = row["open_tokens"]
        out["bootstrap_singleton"] = row["bootstrap_admin"]
        if not row["present"]:
            out["eligible_set_credential"] = out["eligible_initial_status"] = "user_absent"
        else:
            out["eligible_set_credential"] = "case_required"
            if row["status"] != "active":
                out["eligible_initial_status"] = "status_disabled"
            elif row["head_status_decision"] == "disabled":
                out["eligible_initial_status"] = "latest_status_disabled"
            elif row["status_safety"] != "held" or row["status_live"]:
                out["eligible_initial_status"] = "status_already_proven"
            else:
                out["eligible_initial_status"] = "case_required"
        if case_id is not None:
            out["case_id"] = case_id
            out["case_used_in_journal"] = bool(state is not None and case_used(state, case_id))
            rows = conn.execute(text("SELECT * FROM ras_auth_operator_outcome(:c)"), {"c": case_id}).all()
            if not rows:
                out["case_outcome"] = "no_db_record"
            else:
                m = rows[0]._mapping
                out["case_operation_id"] = m["operation_id"]
                out["case_action"] = m["action"]
                out["case_record_confirmed"] = m["case_confirmed"]
                out["case_receipt"] = m["receipt"]
                out["case_current"] = m["current_pointer"] and m["head_current"]
                out["case_outcome"] = ("committed" if m["receipt"] and m["case_confirmed"] and m["event_confirmed"]
                                       else "unconfirmed" if m["receipt"] else "no_receipt")
        return out
