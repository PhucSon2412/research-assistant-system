"""Off-host journal replica: asynchronous, append-only, chain-checked (M8b).

The journal write path is unchanged: requests append and ``fsync`` the local
file under the journal lock (``app.recovery``) and the gate compares it with
the database, locally. This module never takes a DB or journal lock, never
writes the local file and is never called from a request, gate or readiness
check. ``ship_once`` reads the local file (read-only), keeps only complete
records (a torn tail is never shipped), checks the destination's chain and
uploads the missing records as an immutable segment:

    <prefix>/journal/install.json                        journal_id + genesis hash, created once
    <prefix>/journal/<journal_id>/segments/<first>.jsonl  records first..last, created once (If-None-Match)
    <prefix>/journal/<journal_id>/head.json              last shipped seq/hash/time (versioned pointer)

Shipping stops (nothing is written) when the destination is not a prefix of
the local chain: another ``journal_id``/genesis (``replica_journal_mismatch``),
a different record at the same seq (``replica_fork``), a local journal shorter
than what was shipped (``journal_behind_replica``: the local file or the whole
host was rolled back), or a destination chain that does not verify
(``replica_corrupt``). The replica is never pruned and never rewritten.

Limit: records appended after the last successful ship are lost with the host.
That gap is measured (``lag_records``/``unshipped_since``) and alerted, it is
not zero.

``choose`` picks the journal for a restore from a local copy and the replica:
the longer chain only when the shorter one is its prefix, never a splice.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import journal
from app.journal import JournalError, JournalState
from app.ops import state as state_file
from app.ops.s3 import Target, TargetError

FORMAT = "ras-journal-replica/1"
STATUS_FILE = "replica-status.json"
_SEGMENT = re.compile(r"(\d{12})\.jsonl")


class ReplicaError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _base(target: Target, journal_id: str) -> str:
    return target.key("journal", journal_id)


def install_key(target: Target) -> str:
    return target.key("journal", "install.json")


def segment_key(target: Target, journal_id: str, first: int) -> str:
    return f"{_base(target, journal_id)}/segments/{first:012d}.jsonl"


def head_key(target: Target, journal_id: str) -> str:
    return f"{_base(target, journal_id)}/head.json"


def chain_bytes(records) -> bytes:
    return b"".join(r.line.encode("ascii") + b"\n" for r in records)


def read_local(path: Path) -> JournalState:
    """The local journal as it is now: complete records verified, a torn tail kept apart (never shipped)."""
    try:
        return journal.read(path)
    except JournalError as exc:
        raise ReplicaError(exc.code) from None


@dataclass(frozen=True)
class Remote:
    install: dict | None
    state: JournalState | None  # None: nothing shipped yet for this journal
    listing: tuple
    head: dict | None

    @property
    def seq(self) -> int:
        return self.state.head.seq if self.state else 0


def read_install(target: Target) -> dict | None:
    raw = target.get(install_key(target))
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("ascii"))
        if value.get("format") != FORMAT or set(value) != {"format", "journal_id", "genesis_hash", "created_at"}:
            raise ValueError
        uuid.UUID(value["journal_id"])
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError):
        raise ReplicaError("replica_corrupt") from None
    return value


def read_remote(target: Target, journal_id: str, *, cache: dict | None = None) -> Remote:
    """Download and verify the whole shipped chain of ``journal_id`` (segments must be contiguous from 1)."""
    install = read_install(target)
    prefix = f"{_base(target, journal_id)}/segments/"
    listing = tuple(sorted(target.list(prefix).items()))
    if cache is not None and cache.get("listing") == listing and cache.get("journal_id") == journal_id:
        return Remote(install, cache["state"], listing, cache.get("head"))
    parts, expected = [], 1
    for key, _ in listing:
        match = _SEGMENT.fullmatch(key[len(prefix):])
        if match is None or int(match.group(1)) != expected:
            raise ReplicaError("replica_corrupt")
        data = target.get(key)
        if data is None or not data.endswith(b"\n"):
            raise ReplicaError("replica_corrupt")
        count = data.count(b"\n")
        parts.append(data)
        expected += count
    state = None
    if parts:
        try:
            state = journal.parse(b"".join(parts))
        except JournalError:
            raise ReplicaError("replica_corrupt") from None
        if state.torn is not None or str(state.journal_id) != journal_id:
            raise ReplicaError("replica_corrupt")
    head = None
    raw_head = target.get(head_key(target, journal_id))
    if raw_head is not None:
        try:
            head = json.loads(raw_head.decode("ascii"))
            int(head["seq"]), str(head["hash"])
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise ReplicaError("replica_corrupt") from None
    if head is not None and state is not None and head["seq"] > state.head.seq:
        raise ReplicaError("replica_incomplete")  # the pointer names records no segment holds
    if head is not None and state is None:
        raise ReplicaError("replica_incomplete")
    if cache is not None:
        cache.update(listing=listing, journal_id=journal_id, state=state, head=head)
    return Remote(install, state, listing, head)


def relation(local: JournalState, remote: JournalState | None) -> str:
    """How the shipped chain relates to the local one: ``equal``, ``local_ahead`` or an error code."""
    if remote is None:
        return "local_ahead"
    if remote.journal_id != local.journal_id or remote.genesis_hash != local.genesis_hash:
        return "replica_journal_mismatch"
    r_seq, l_seq = remote.head.seq, local.head.seq
    common = min(r_seq, l_seq)
    if local.records[common - 1].hash != remote.records[common - 1].hash:
        return "replica_fork"
    if r_seq > l_seq:
        return "journal_behind_replica"
    return "equal" if r_seq == l_seq else "local_ahead"


def ship_once(local_path: Path, target: Target, now: datetime, *, max_records: int = 1000,
              max_segments: int = 20, cache: dict | None = None) -> dict:
    """One replication cycle. Returns codes/counts; raises ``ReplicaError``/``TargetError`` with a code."""
    local = read_local(local_path)
    journal_id = str(local.journal_id)
    install = read_install(target)
    if install is None:
        record = {"format": FORMAT, "journal_id": journal_id, "genesis_hash": local.genesis_hash,
                  "created_at": now.isoformat()}
        try:
            target.create(install_key(target), json.dumps(record, sort_keys=True).encode("ascii"),
                          content_type="application/json")
        except TargetError as exc:
            if exc.code != "target_conflict":
                raise
        install = read_install(target)
    if install is None or install["journal_id"] != journal_id or install["genesis_hash"] != local.genesis_hash:
        raise ReplicaError("replica_journal_mismatch")
    remote = read_remote(target, journal_id, cache=cache)
    shipped = 0
    for _ in range(max_segments):
        rel = relation(local, remote.state)
        if rel not in ("equal", "local_ahead"):
            raise ReplicaError(rel)
        if rel == "equal":
            break
        start = remote.seq
        batch = local.records[start:start + max_records]
        data = chain_bytes(batch)
        key = segment_key(target, journal_id, start + 1)
        try:
            target.create(key, data, content_type="application/x-ndjson",
                          metadata={"last-seq": str(batch[-1].seq), "last-hash": batch[-1].hash})
        except TargetError as exc:
            if exc.code == "target_conflict":
                raise ReplicaError("replica_conflict") from None  # someone else wrote it: verify next cycle
            raise
        size, digest = target.digest(key)
        if (size, digest) != (len(data), hashlib.sha256(data).hexdigest()):
            raise ReplicaError("replica_readback_mismatch")
        shipped += len(batch)
        # The destination now holds exactly the local prefix (read back above); re-list only.
        listing = tuple(sorted(target.list(f"{_base(target, journal_id)}/segments/").items()))
        prefix_state = journal.parse(chain_bytes(local.records[:start + len(batch)]))
        if not listing or listing[-1][0] != key:
            raise ReplicaError("replica_conflict")  # an unexpected later segment: verify everything next cycle
        remote = Remote(remote.install, prefix_state, listing, remote.head)
        if cache is not None:
            cache.update(listing=listing, journal_id=journal_id, state=prefix_state, head=remote.head)
    rel = relation(local, remote.state)
    if rel not in ("equal", "local_ahead"):
        raise ReplicaError(rel)
    head = remote.state.head if remote.state else None
    if head is not None and (remote.head is None or remote.head.get("seq") != head.seq):
        pointer = {"format": FORMAT, "journal_id": journal_id, "seq": head.seq, "hash": head.hash,
                   "shipped_at": now.isoformat()}
        target.replace(head_key(target, journal_id), json.dumps(pointer, sort_keys=True).encode("ascii"))
        if cache is not None:
            cache["head"] = pointer
    return {"journal_id": journal_id, "local_seq": local.head.seq, "remote_seq": remote.seq,
            "shipped_records": shipped, "local_torn": local.torn is not None,
            "caught_up": remote.seq == local.head.seq}


# --- status file ------------------------------------------------------------------

def record_status(state_dir: Path, now: datetime, result: dict | None, error: str | None) -> dict:
    """Persist the cycle's outcome; ``unshipped_since`` is when records were first seen not shipped."""
    path = state_dir / STATUS_FILE
    try:
        previous = state_file.read(path) or {}
    except (ValueError, OSError):
        previous = {}
    local_seq = result["local_seq"] if result else previous.get("local_seq")
    remote_seq = result["remote_seq"] if result else previous.get("remote_seq")
    if result is not None and result["caught_up"]:
        unshipped_since = None
    else:
        unshipped_since = previous.get("unshipped_since") or now.isoformat()
    status = {"format": FORMAT, "checked_at": now.isoformat(), "ok": error is None, "code": error or "ok",
              "local_seq": local_seq, "remote_seq": remote_seq,
              "lag_records": (local_seq - remote_seq) if isinstance(local_seq, int) and isinstance(remote_seq, int)
              else None, "unshipped_since": unshipped_since,
              "last_success_at": now.isoformat() if error is None else previous.get("last_success_at"),
              "local_torn": bool(result and result.get("local_torn"))}
    state_file.write(path, status)
    return status


# --- restore: fetch and choose -------------------------------------------------------

def fetch(target: Target) -> tuple[JournalState | None, dict]:
    """The replica's verified chain (``None`` when nothing was ever shipped) and its pointer facts."""
    install = read_install(target)
    if install is None:
        return None, {"replica": "empty"}
    remote = read_remote(target, install["journal_id"])
    if remote.state is None:
        return None, {"replica": "empty", "journal_id": install["journal_id"]}
    if remote.state.genesis_hash != install["genesis_hash"]:
        raise ReplicaError("replica_corrupt")
    return remote.state, {"replica": "ok", "journal_id": install["journal_id"], "seq": remote.state.head.seq,
                          "head_seq": remote.head["seq"] if remote.head else None,
                          "shipped_at": remote.head.get("shipped_at") if remote.head else None}


@dataclass(frozen=True)
class Choice:
    state: JournalState
    source: str  # "local", "replica" or "both_equal"
    local_seq: int | None
    replica_seq: int | None
    relation: str  # equal, local_ahead, replica_ahead, local_only, replica_only

    def data(self) -> bytes:
        return chain_bytes(self.state.records)


def choose(local: JournalState | None, replica: JournalState | None) -> Choice:
    """The chain for a restore: the longer of the two only when the other is its prefix.

    A torn local tail is not part of the chain. Different journals or chains that
    differ at any common seq are refused (``journal_sources_*``); they are never
    spliced. With one source only, that source is used (the caller reports it).
    """
    if local is None and replica is None:
        raise ReplicaError("journal_sources_missing")
    if replica is None:
        return Choice(local, "local", local.head.seq, None, "local_only")
    if local is None:
        return Choice(replica, "replica", None, replica.head.seq, "replica_only")
    if local.journal_id != replica.journal_id or local.genesis_hash != replica.genesis_hash:
        raise ReplicaError("journal_sources_mismatch")
    l_seq, r_seq = local.head.seq, replica.head.seq
    common = min(l_seq, r_seq)
    if local.records[common - 1].hash != replica.records[common - 1].hash:
        raise ReplicaError("journal_sources_diverged")
    if l_seq == r_seq:
        return Choice(local, "both_equal", l_seq, r_seq, "equal")
    if l_seq > r_seq:
        return Choice(local, "local", l_seq, r_seq, "local_ahead")
    return Choice(replica, "replica", l_seq, r_seq, "replica_ahead")
