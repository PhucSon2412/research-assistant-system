"""M8b journal replica: ship complete records only, never overwrite, stop on fork/rollback, choose safely.

No database, no network: a real hash-chained journal file and an in-memory versioned
bucket (``tests.m8b_fakes``). Real MinIO semantics are in ``test_m8b_s3_minio.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app import backup, journal
from app.ops import replica
from app.ops.replica import ReplicaError
from app.ops.s3 import TargetError
from tests.m8b_fakes import T0, extend, fake_target, new_journal


@pytest.fixture
def local(tmp_path):
    path = tmp_path / "journal" / "revocations.jsonl"
    return path, new_journal(path, recoveries=2)


def segments(s3):
    return sorted(k for k in s3.objects if "/segments/" in k)


def test_ships_complete_records_as_immutable_segments_and_head(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    result = replica.ship_once(path, target, T0)
    assert result["local_seq"] == result["remote_seq"] == 5 and result["caught_up"] and result["shipped_records"] == 5
    assert segments(s3) == ["pilot/journal/%s/segments/000000000001.jsonl" % state.journal_id]
    assert s3.objects[segments(s3)[0]] == path.read_bytes()
    # Every segment and the install record are conditional creates; only the head pointer is replaced.
    for name, key, kw in s3.calls:
        if name == "put_object" and not key.endswith("head.json"):
            assert kw["if_none_match"] == "*"
    state = extend(path, state, 1)
    result = replica.ship_once(path, target, T0)
    assert result["shipped_records"] == 2 and result["remote_seq"] == 7
    assert [k.rsplit("/", 1)[1] for k in segments(s3)] == ["000000000001.jsonl", "000000000006.jsonl"]
    remote, info = replica.fetch(target)
    assert remote.head.hash == state.head.hash and info["seq"] == info["head_seq"] == 7


def test_torn_tail_is_never_shipped(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    with open(path, "ab") as handle:
        handle.write(b'{"body":{"recovery_id":"torn')
    result = replica.ship_once(path, target, T0)
    assert result["remote_seq"] == 5 and result["local_torn"]
    shipped = b"".join(s3.objects[k] for k in segments(s3))
    assert b"torn" not in shipped and shipped.endswith(b"\n")
    assert shipped == b"".join(r.line.encode() + b"\n" for r in state.records)


def test_destination_outage_grows_lag_then_catches_up(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    state_dir = tmp_path / "state"
    replica.record_status(state_dir, T0, replica.ship_once(path, target, T0), None)
    extend(path, state, 2)
    s3.down = True
    with pytest.raises(TargetError) as down:
        replica.ship_once(path, target, T0)
    assert down.value.code == "target_unavailable"
    status = replica.record_status(state_dir, T0, None, down.value.code)
    assert not status["ok"] and status["unshipped_since"] == T0.isoformat() and status["remote_seq"] == 5
    s3.down = False
    result = replica.ship_once(path, target, T0)
    status = replica.record_status(state_dir, T0, result, None)
    assert status["ok"] and status["unshipped_since"] is None and status["lag_records"] == 0
    assert result["remote_seq"] == 9


def _forked_copy(tmp_path, state, at_seq):
    """Same genesis, a different record at ``at_seq``: a fork of the shipped chain."""
    path = tmp_path / "fork" / "revocations.jsonl"
    path.parent.mkdir()
    keep = list(state.records[:at_seq - 1])
    path.write_bytes(b"".join(r.line.encode() + b"\n" for r in keep))
    forked = journal.read(path)
    return path, extend(path, forked, 2)


def test_fork_stops_shipping_without_writing(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    replica.ship_once(path, target, T0)
    fork_path, _ = _forked_copy(tmp_path, state, at_seq=4)
    before = dict(s3.objects)
    puts = len(s3.puts())
    with pytest.raises(ReplicaError) as refused:
        replica.ship_once(fork_path, target, T0)
    assert refused.value.code == "replica_fork"
    assert s3.objects == before and len(s3.puts()) == puts


def test_rolled_back_local_journal_is_detected_not_shipped(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    old = path.read_bytes()
    extend(path, state, 1)
    replica.ship_once(path, target, T0)
    path.write_bytes(old)  # host/snapshot rollback of the journal
    with pytest.raises(ReplicaError) as refused:
        replica.ship_once(path, target, T0)
    assert refused.value.code == "journal_behind_replica"
    # ...and once the rolled-back host appends again, the chains differ: a fork, still no write.
    extend(path, journal.read(path), 2)
    puts = len(s3.puts())
    with pytest.raises(ReplicaError) as again:
        replica.ship_once(path, target, T0)
    assert again.value.code == "replica_fork" and len(s3.puts()) == puts


def test_another_journal_is_refused(tmp_path, local):
    path, _ = local
    target, s3 = fake_target(tmp_path)
    replica.ship_once(path, target, T0)
    other = tmp_path / "other" / "revocations.jsonl"
    new_journal(other, recoveries=3)
    puts = len(s3.puts())
    with pytest.raises(ReplicaError) as refused:
        replica.ship_once(other, target, T0)
    assert refused.value.code == "replica_journal_mismatch" and len(s3.puts()) == puts


@pytest.mark.parametrize("tamper", ["edit_record", "drop_segment", "head_ahead"])
def test_tampered_replica_is_refused(tmp_path, local, tamper):
    path, state = local
    target, s3 = fake_target(tmp_path)
    replica.ship_once(path, target, T0)
    extend(path, state, 1)
    replica.ship_once(path, target, T0)
    first, second = segments(s3)
    if tamper == "edit_record":
        s3.objects[second] = s3.objects[second].replace(b'"replayed":0', b'"replayed":9')
    elif tamper == "drop_segment":
        del s3.objects[first]
    else:
        head = [k for k in s3.objects if k.endswith("head.json")][0]
        s3.objects[head] = s3.objects[head].replace(b'"seq": 7', b'"seq": 99')
    puts = len(s3.puts())
    with pytest.raises(ReplicaError) as refused:
        replica.ship_once(path, target, T0)
    assert refused.value.code in ("replica_corrupt", "replica_incomplete") and len(s3.puts()) == puts
    with pytest.raises(ReplicaError):
        replica.fetch(target)


def test_existing_segment_is_never_overwritten(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    replica.ship_once(path, target, T0)
    key = segments(s3)[0]
    with pytest.raises(TargetError) as conflict:
        target.create(key, b"anything\n")
    assert conflict.value.code == "target_conflict" and s3.versions[key] == [path.read_bytes()]


def test_readback_mismatch_is_an_error(tmp_path, local):
    path, state = local
    target, s3 = fake_target(tmp_path)
    s3.corrupt_reads.add(f"pilot/journal/{state.journal_id}/segments/000000000001.jsonl")
    with pytest.raises(ReplicaError) as refused:
        replica.ship_once(path, target, T0)
    assert refused.value.code == "replica_readback_mismatch"


# --- choosing the journal for a restore -------------------------------------------------------

def test_choose_longer_only_when_prefix(tmp_path, local):
    path, state = local
    short = journal.parse(b"".join(r.line.encode() + b"\n" for r in state.records[:3]))
    longer = journal.read(path)
    assert replica.choose(short, longer).source == "replica" and replica.choose(short, longer).relation == "replica_ahead"
    assert replica.choose(longer, short).source == "local" and replica.choose(longer, short).relation == "local_ahead"
    assert replica.choose(longer, longer).relation == "equal"
    assert replica.choose(None, longer).relation == "replica_only"
    assert replica.choose(longer, None).relation == "local_only"
    _, forked = _forked_copy(tmp_path, state, at_seq=4)
    with pytest.raises(ReplicaError) as diverged:
        replica.choose(longer, forked)
    assert diverged.value.code == "journal_sources_diverged"
    other = new_journal(tmp_path / "x" / "revocations.jsonl", 1)
    with pytest.raises(ReplicaError) as mismatch:
        replica.choose(longer, other)
    assert mismatch.value.code == "journal_sources_mismatch"
    with pytest.raises(ReplicaError):
        replica.choose(None, None)


def _manifest(state, seq):
    return {"journal_at_backup": {"journal_id": str(state.journal_id), "genesis_hash": state.genesis_hash,
                                  "db_records": seq, "db_head_seq": seq, "db_head_hash": state.records[seq - 1].hash,
                                  "unconfirmed": 0}}


def test_db_and_local_journal_rolled_back_but_replica_newer_is_caught(tmp_path, local):
    """Case (d): the old local copy passes the two-copy check; the replica shows it is behind."""
    path, state = local
    old_local = journal.read(path)
    manifest = _manifest(old_local, old_local.head.seq)
    newer = extend(path, state, 2)
    backup.check_journal_for_backup(old_local, manifest)  # the old two-way check cannot see it
    choice = replica.choose(old_local, newer)
    assert choice.relation == "replica_ahead" and choice.state.head.seq == newer.head.seq
    backup.check_journal_for_backup(choice.state, manifest)


def test_replica_older_than_backup_is_refused(tmp_path, local):
    """Case (c): only the replica survives and it is behind what the backup's database holds."""
    path, state = local
    newer = extend(path, state, 2)
    manifest = _manifest(newer, newer.head.seq)
    shipped = journal.parse(b"".join(r.line.encode() + b"\n" for r in newer.records[:5]))
    choice = replica.choose(None, shipped)
    with pytest.raises(backup.BackupError) as refused:
        backup.check_journal_for_backup(choice.state, manifest)
    assert refused.value.code == "journal_behind_backup"


def test_status_file_records_codes_only(tmp_path, local):
    path, _ = local
    status = replica.record_status(tmp_path / "s", T0, None, "replica_fork")
    assert set(status) == {"format", "checked_at", "ok", "code", "local_seq", "remote_seq", "lag_records",
                           "unshipped_since", "last_success_at", "local_torn"}
    assert status["code"] == "replica_fork" and not status["ok"]


def test_replicator_never_needs_database_or_lock(tmp_path, local, monkeypatch):
    """The replicator path imports nothing that opens a DB connection or takes the journal lock."""
    import app.recovery as recovery_module
    called = []
    monkeypatch.setattr(recovery_module, "_lock_journal", lambda *a: called.append("lock"))
    path, _ = local
    target, _ = fake_target(tmp_path)
    replica.ship_once(path, target, T0)
    assert called == []
    assert uuid.UUID(str(journal.read(path).journal_id))
