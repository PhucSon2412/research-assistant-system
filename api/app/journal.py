"""The revocation journal: an append-only, hash-chained JSON Lines file (M6c-1).

It lives **outside** PostgreSQL and the paper object store, so a restore of
either does not roll it back (``RAS_RECOVERY__JOURNAL_PATH``; in Compose dev a
separate volume). One canonical JSON object per line::

    {"body": {...}, "hash": "<sha256>", "kind": "...", "prev": "<sha256>", "seq": n}

``hash`` is SHA-256 of the canonical line without ``hash``; ``prev`` is the
previous record's hash (64 zeros for the genesis record), so a changed,
removed or reordered record breaks the chain. Kinds:

- ``genesis``: the journal's ID, creation time and how it was registered.
- ``revocation``: the identity of one tombstone and its per-field retention
  bases: IDs, object key, codes, epoch, times and the Admin's recorded basis.
  Never a title, text, URL, evidence, checksum, bytes or secret; the reader
  refuses a record carrying any other field.
- ``recovery_begin`` / ``recovery_end``: a recovery run; while one is open
  the recovery gate stays closed.
- ``review_closure`` (M6c-2b1): a review request's temporary Admin grant
  ended. Request, item, version and file IDs, the decision (``withdrawn``,
  ``rejected``, ``approved``), its reason code and time. Never the declared
  text, rejection note, title, email, URL, checksum or the Admin's evidence.
- ``session_revocation`` (M6c-2b2a): session ID, user ID, reason code and
  revocation time. Never a cookie, token/hash, CSRF, email, verifier or paper.
- ``reset_token_closure`` (M6c-2b2b1): token ID, User ID, consumed/revoked,
  reason code and time. No raw token/hash; replay never changes credentials.

Only this module reads or writes the file. Writers hold the journal advisory
lock in PostgreSQL (``app.recovery``), so there is one appender at a time;
the file itself is not locked. A record is durable once its line and newline
are written and ``fsync`` returned. A line without its newline is a *torn
tail*: an append that did not finish, never a durable record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.models.enums import RevocationReason, ReviewStatus, RevokedMetadataField, SessionRevokeReason, TombstoneKind
from app.models.review import closed_status_of

ZERO_HASH = "0" * 64
MAX_LINE_BYTES = 64 * 1024

GENESIS, REVOCATION, RECOVERY_BEGIN, RECOVERY_END = "genesis", "revocation", "recovery_begin", "recovery_end"
REVIEW_CLOSURE = "review_closure"
SESSION_REVOCATION = "session_revocation"
RESET_TOKEN_CLOSURE = "reset_token_closure"
RESET_END_REASONS = {"consumed": {"password_reset", "legacy_consumed"},
                     "revoked": {"reset_replaced", "reset_completed", "user_disabled", "legacy_revoked"}}
KINDS = (GENESIS, REVOCATION, RECOVERY_BEGIN, RECOVERY_END, REVIEW_CLOSURE, SESSION_REVOCATION, RESET_TOKEN_CLOSURE)
GENESIS_MODES = ("new_install", "existing_db", "rebuilt_from_db")
REVOCATION_FIELDS = ("id", "target_kind", "item_id", "version_id", "source_file_id", "storage_key",
                     "access_rights_id", "reason_code", "prior_publication_state", "rights_epoch", "recorded_at",
                     "recorded_by_user_id", "purge_due_at")
RETENTION_FIELDS = ("field", "policy_source", "scope", "reason", "confirmed_by_user_id", "recorded_at")
CLOSURE_FIELDS = ("request_id", "item_id", "version_id", "source_file_id", "status", "reason_code", "closed_at")
SESSION_FIELDS = ("session_id", "user_id", "reason_code", "revoked_at")
CLOSED_STATUSES = (ReviewStatus.WITHDRAWN.value, ReviewStatus.REJECTED.value, ReviewStatus.APPROVED.value)
BODY_FIELDS = {
    GENESIS: frozenset({"journal_id", "created_at", "mode"}),
    REVOCATION: frozenset(REVOCATION_FIELDS) | {"retentions"},
    RECOVERY_BEGIN: frozenset({"recovery_id", "started_at", "db_head_seq"}),
    RECOVERY_END: frozenset({"recovery_id", "finished_at", "replayed", "tombstones_purged", "objects_removed"}),
    REVIEW_CLOSURE: frozenset(CLOSURE_FIELDS),
    SESSION_REVOCATION: frozenset(SESSION_FIELDS),
    RESET_TOKEN_CLOSURE: frozenset({"token_id", "user_id", "end_kind", "reason_code", "ended_at"}),
}
_KEY = re.compile(r"originals/[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")


class JournalError(Exception):
    """The journal cannot be used; ``code`` says why (no path or content in it)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Record:
    seq: int
    kind: str
    body: dict
    prev: str
    hash: str
    line: str


@dataclass(frozen=True)
class JournalState:
    records: tuple[Record, ...]
    size: int  # bytes of complete records; a torn tail starts here
    torn: bytes | None

    @property
    def journal_id(self) -> uuid.UUID:
        return uuid.UUID(self.records[0].body["journal_id"])

    @property
    def genesis_hash(self) -> str:
        return self.records[0].hash

    @property
    def head(self) -> Record:
        return self.records[-1]

    @property
    def open_recovery(self) -> str | None:
        """The ``recovery_id`` of a ``recovery_begin`` with no ``recovery_end`` yet."""
        current = None
        for record in self.records:
            if record.kind == RECOVERY_BEGIN:
                current = record.body["recovery_id"]
            elif record.kind == RECOVERY_END:
                current = None
        return current


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(seq: int, kind: str, body: dict, prev: str) -> str:
    return hashlib.sha256(canonical({"body": body, "kind": kind, "prev": prev, "seq": seq}).encode()).hexdigest()


def make_record(seq: int, kind: str, body: dict, prev: str) -> Record:
    _check_body(kind, body)
    digest = _digest(seq, kind, body, prev)
    line = canonical({"body": body, "hash": digest, "kind": kind, "prev": prev, "seq": seq})
    if len(line) + 1 > MAX_LINE_BYTES:
        raise JournalError("journal_record_too_large")
    return Record(seq, kind, body, prev, digest, line)


def next_record(head: Record, kind: str, body: dict) -> Record:
    return make_record(head.seq + 1, kind, body, head.hash)


def genesis(journal_id: uuid.UUID, created_at: datetime, mode: str) -> Record:
    return make_record(1, GENESIS, {"journal_id": str(journal_id), "created_at": created_at.isoformat(),
                                    "mode": mode}, ZERO_HASH)


# --- validation ---------------------------------------------------------------

def _uuid(value, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("uuid")


def _time(value) -> None:
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("time")


def _count(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("count")


def _text(value, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("text")


def _check_body(kind: str, body) -> None:
    """Exactly the fields of the kind, each of its type: nothing else can enter the journal."""
    if kind not in KINDS or not isinstance(body, dict) or set(body) != BODY_FIELDS[kind]:
        raise JournalError("journal_corrupt")
    try:
        if kind == GENESIS:
            _uuid(body["journal_id"])
            _time(body["created_at"])
            if body["mode"] not in GENESIS_MODES:
                raise ValueError("mode")
        elif kind == REVOCATION:
            for name in ("id", "item_id", "recorded_by_user_id"):
                _uuid(body[name])
            for name in ("version_id", "source_file_id", "access_rights_id"):
                _uuid(body[name], nullable=True)
            if body["storage_key"] is not None and not (isinstance(body["storage_key"], str)
                                                        and _KEY.fullmatch(body["storage_key"])):
                raise ValueError("key")
            if body["target_kind"] not in {k.value for k in TombstoneKind}:
                raise ValueError("kind")
            if body["reason_code"] not in {r.value for r in RevocationReason}:
                raise ValueError("reason")
            if body["prior_publication_state"] not in ("pending", "listed"):
                raise ValueError("state")
            _count(body["rights_epoch"])
            _time(body["recorded_at"])
            _time(body["purge_due_at"])
            retentions = body["retentions"]
            if not isinstance(retentions, list):
                raise ValueError("retentions")
            for basis in retentions:
                if not isinstance(basis, dict) or set(basis) != set(RETENTION_FIELDS):
                    raise ValueError("retention")
                if basis["field"] not in {f.value for f in RevokedMetadataField}:
                    raise ValueError("field")
                _text(basis["policy_source"], 500)
                _text(basis["scope"], 500)
                _text(basis["reason"], 1000)
                _uuid(basis["confirmed_by_user_id"])
                _time(basis["recorded_at"])
        elif kind == RESET_TOKEN_CLOSURE:
            _uuid(body["token_id"])
            _uuid(body["user_id"])
            if (not isinstance(body["end_kind"], str) or body["end_kind"] not in RESET_END_REASONS
                    or not isinstance(body["reason_code"], str)
                    or body["reason_code"] not in RESET_END_REASONS[body["end_kind"]]):
                raise ValueError("ending")
            _time(body["ended_at"])
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", body["ended_at"]):
                raise ValueError("time")
        elif kind == SESSION_REVOCATION:
            _uuid(body["session_id"])
            _uuid(body["user_id"])
            if body["reason_code"] not in {r.value for r in SessionRevokeReason}:
                raise ValueError("reason")
            _time(body["revoked_at"])
        elif kind == REVIEW_CLOSURE:
            for name in ("request_id", "item_id", "version_id", "source_file_id"):
                _uuid(body[name])
            # The decision and its code must agree: a code stands for exactly one decision.
            if (body["status"] not in CLOSED_STATUSES or not isinstance(body["reason_code"], str)
                    or closed_status_of(body["reason_code"]) != body["status"]):
                raise ValueError("closure")
            _time(body["closed_at"])
        else:
            _uuid(body["recovery_id"])
            if kind == RECOVERY_BEGIN:
                _time(body["started_at"])
                _count(body["db_head_seq"])
            else:
                _time(body["finished_at"])
                for name in ("replayed", "tombstones_purged", "objects_removed"):
                    _count(body[name])
    except (ValueError, TypeError, AttributeError):
        raise JournalError("journal_corrupt") from None


def parse_line(raw: bytes, expected_seq: int, prev: str) -> Record:
    """One complete line (no newline) that must continue the chain at ``expected_seq``."""
    try:
        line = raw.decode("ascii")
        value = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        raise JournalError("journal_corrupt") from None
    if not isinstance(value, dict) or set(value) != {"body", "hash", "kind", "prev", "seq"}:
        raise JournalError("journal_corrupt")
    if canonical(value) != line or value["seq"] != expected_seq or value["prev"] != prev:
        raise JournalError("journal_corrupt")
    if not isinstance(value["hash"], str) or not _HASH.fullmatch(value["hash"]):
        raise JournalError("journal_corrupt")
    _check_body(value["kind"], value["body"])
    if _digest(value["seq"], value["kind"], value["body"], value["prev"]) != value["hash"]:
        raise JournalError("journal_corrupt")
    return Record(value["seq"], value["kind"], value["body"], value["prev"], value["hash"], line)


def parse(data: bytes) -> JournalState:
    """Verify a whole journal: chain, canonical form, one genesis first, paired recovery records."""
    if not data:
        raise JournalError("journal_corrupt")
    parts = data.split(b"\n")
    torn = parts.pop() or None  # bytes after the last newline: an unfinished append
    records: list[Record] = []
    prev = ZERO_HASH
    open_recovery = None
    for number, raw in enumerate(parts, start=1):
        if len(raw) + 1 > MAX_LINE_BYTES:
            raise JournalError("journal_corrupt")
        record = parse_line(raw, number, prev)
        if (record.kind == GENESIS) != (number == 1):
            raise JournalError("journal_corrupt")
        if record.kind == RECOVERY_BEGIN:
            if open_recovery is not None:
                raise JournalError("journal_corrupt")
            open_recovery = record.body["recovery_id"]
        elif record.kind == RECOVERY_END:
            if record.body["recovery_id"] != open_recovery:
                raise JournalError("journal_corrupt")
            open_recovery = None
        records.append(record)
        prev = record.hash
    if not records:
        raise JournalError("journal_corrupt")
    return JournalState(tuple(records), len(data) - (len(torn) if torn else 0), torn)


# --- file access --------------------------------------------------------------

def read(path: Path) -> JournalState:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise JournalError("journal_missing") from None
    except OSError:
        raise JournalError("journal_unreadable") from None
    return parse(data)


_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple, JournalState | JournalError]] = {}


def read_cached(path: Path) -> JournalState:
    """``read`` again only when the file's size, times or identity changed since the last full check.

    An in-place rewrite that keeps all of them is not noticed until the process
    restarts or ``recover`` runs; every gate check also compares the head with
    the database, which catches any change of the head record.
    """
    try:
        st = path.stat()
        signature = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)
    except FileNotFoundError:
        raise JournalError("journal_missing") from None
    except OSError:
        raise JournalError("journal_unreadable") from None
    key = str(path)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        if isinstance(cached[1], JournalError):
            raise JournalError(cached[1].code)
        return cached[1]
    try:
        result: JournalState | JournalError = read(path)
    except JournalError as exc:
        result = exc
    with _cache_lock:
        _cache[key] = (signature, result)
    if isinstance(result, JournalError):
        raise JournalError(result.code)
    return result


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":  # Windows cannot open a directory for fsync; NTFS journals its metadata
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: bytes, mode: str) -> None:
    with open(path, mode) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def create(path: Path, records: list[Record]) -> JournalState:
    """Write a new journal (genesis first); refuses to replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(r.line.encode("ascii") + b"\n" for r in records)
    state = parse(data)  # never write what the reader would refuse
    try:
        _write(path, data, "xb")
    except FileExistsError:
        raise JournalError("journal_exists") from None
    _fsync_dir(path.parent)
    return state


def append(path: Path, state: JournalState, records: list[Record]) -> JournalState:
    """Append records continuing ``state``, which the caller read under the journal lock.

    Refuses when the file no longer has exactly the verified bytes (a torn
    tail, or a change since the read). Raises ``OSError`` if the write or
    ``fsync`` fails: the new lines may then be absent, torn or complete.
    """
    if state.torn is not None:
        raise JournalError("journal_torn")
    prev = state.head
    for record in records:
        if record.seq != prev.seq + 1 or record.prev != prev.hash:
            raise JournalError("journal_sequence_mismatch")
        prev = record
    if path.stat().st_size != state.size:
        raise JournalError("journal_changed")
    _write(path, b"".join(r.line.encode("ascii") + b"\n" for r in records), "ab")
    return JournalState(state.records + tuple(records), state.size + sum(len(r.line) + 1 for r in records), None)


def sync(path: Path) -> None:
    """``fsync`` the journal as it is now: after it returns, every complete line in it is durable.

    Raises ``OSError`` when the file cannot be opened or synced.
    """
    with open(path, "ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def drop_torn_tail(path: Path, state: JournalState, stamp: str) -> JournalState:
    """Move an unfinished last line aside (``<file>.torn-<stamp>``) and cut the file back to its records."""
    if state.torn is None:
        return state
    aside = path.with_name(f"{path.name}.torn-{stamp}")
    _write(aside, state.torn, "xb")
    with open(path, "r+b") as handle:
        handle.truncate(state.size)
        handle.flush()
        os.fsync(handle.fileno())
    return JournalState(state.records, state.size, None)
