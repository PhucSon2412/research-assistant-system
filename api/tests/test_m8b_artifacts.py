"""M8b encrypted off-host backups: age encryption, no plaintext left, read-back, retention, fetch.

Real ``app.backup`` artifacts and real age encryption (pyrage); in-memory versioned bucket.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import timedelta

import pytest

from app import backup
from app.ops import artifacts
from app.ops.artifacts import OffsiteError
from app.ops.s3 import TargetError
from tests.m8b_fakes import T0, fake_target

pyrage = pytest.importorskip("pyrage")


@pytest.fixture
def keys(tmp_path):
    identity = pyrage.x25519.Identity.generate()
    recipients = tmp_path / "config" / "age-recipients.txt"
    recipients.parent.mkdir()
    recipients.write_text(f"# operator public key\n{identity.to_public()}\n", encoding="ascii")
    secret = tmp_path / "operator-only" / "identity.txt"
    secret.parent.mkdir()
    secret.write_text(f"{identity}\n", encoding="ascii")
    return recipients, secret


def make_artifact(root, backup_id="20261020T090000Z-0badc0de", objects=2, db_head_seq=5):
    writer = backup.ArtifactWriter(root, backup_id)
    database = writer.write_stream(backup.DUMP, [b"PGDMP synthetic dump " + backup_id.encode()])
    entries = []
    for n in range(1, objects + 1):
        entry = writer.write_stream(f"objects/{n:06d}", [f"object {n} of {backup_id}".encode()])
        entries.append({"key": f"originals/{n:032x}", "content_type": "application/pdf", **entry})
    return writer.finish({
        "started_at": T0.isoformat(), "finished_at": T0.isoformat(),
        "source": {"compose_project": "rasm8b-test", "server_version": "16", "system_identifier": "1",
                   "alembic_revision": "0052"},
        "snapshot": {"id": "00000003-00000002-1", "taken_at": T0.isoformat(), "wal_lsn": "0/1", "txid_snapshot": "1:1:"},
        "writers_stopped": ["api", "web", "worker", "beat"],
        "journal_at_backup": {"journal_id": "00000000-0000-4000-8000-000000000000", "genesis_hash": "0" * 64,
                              "db_records": db_head_seq, "db_head_seq": db_head_seq, "db_head_hash": "1" * 64,
                              "unconfirmed": 0},
        "database": database,
        "objects": {"bucket": "ras-originals", "count": len(entries), "bytes": sum(e["bytes"] for e in entries),
                    "entries": entries},
        "db_object_check": {"file_rows_checked": len(entries)}})


def staging_files(staging):
    return sorted(str(p.relative_to(staging)) for p in staging.rglob("*") if p.is_file())


def test_ship_encrypts_uploads_reads_back_and_leaves_no_plaintext(tmp_path, keys):
    recipients, secret = keys
    staging = tmp_path / "staging"
    artifact = make_artifact(staging / "artifacts")
    plain_dump = (artifact / backup.DUMP).read_bytes()
    target, s3 = fake_target(tmp_path)
    shipped = artifacts.ship(artifact, staging, target, recipients, T0)
    assert staging_files(staging) == []  # no artifact, tar or ciphertext stays behind
    cipher_key, meta_key = artifacts.keys(target, shipped["backup_id"])
    cipher = s3.objects[cipher_key]
    assert cipher.startswith(b"age-encryption.org/v1\n") and plain_dump not in cipher
    assert hashlib.sha256(cipher).hexdigest() == shipped["ciphertext_sha256"]
    meta = json.loads(s3.objects[meta_key])
    assert meta["verified"] is True and meta["ciphertext_sha256"] == shipped["ciphertext_sha256"]
    assert "AGE-SECRET-KEY" not in json.dumps(meta) and "originals/" not in json.dumps(meta)
    # The ciphertext write never replaces anything; the metadata is written after the read-back.
    order = [key for key in s3.puts()]
    assert order.index(cipher_key) < order.index(meta_key)
    [listed] = artifacts.list_backups(target)
    assert listed.good
    # Restore side: decrypt in an isolated staging with the operator's test identity.
    restored = artifacts.fetch(target, shipped["backup_id"], tmp_path / "restore-staging", secret)
    assert backup.verify_artifact(restored)["backup_id"] == shipped["backup_id"]
    assert (restored / backup.DUMP).read_bytes() == plain_dump


def test_private_key_on_server_is_refused(tmp_path, keys):
    recipients, secret = keys
    recipients.write_text(recipients.read_text() + secret.read_text(), encoding="ascii")
    staging = tmp_path / "staging"
    artifact = make_artifact(staging / "artifacts")
    target, s3 = fake_target(tmp_path)
    with pytest.raises(OffsiteError) as refused:
        artifacts.ship(artifact, staging, target, recipients, T0)
    assert refused.value.code == "private_key_on_server"
    assert staging_files(staging) == [] and s3.objects == {}


@pytest.mark.parametrize("fault", ["unavailable", "versioning_off", "readback", "conflict"])
def test_upload_faults_leave_nothing_good_and_no_plaintext(tmp_path, keys, fault):
    recipients, _ = keys
    staging = tmp_path / "staging"
    artifact = make_artifact(staging / "artifacts")
    target, s3 = fake_target(tmp_path)
    cipher_key, meta_key = artifacts.keys(target, "20261020T090000Z-0badc0de")
    if fault == "unavailable":
        s3.down = True
    elif fault == "versioning_off":
        s3.versioning = "Suspended"
    elif fault == "readback":
        s3.corrupt_reads.add(cipher_key)
    else:
        s3.objects[cipher_key] = b"an older upload"
    with pytest.raises((OffsiteError, TargetError)) as failed:
        artifacts.ship(artifact, staging, target, recipients, T0)
    assert failed.value.code in {"unavailable": ("target_unavailable",), "versioning_off": ("target_versioning_disabled",),
                                 "readback": ("upload_readback_mismatch",), "conflict": ("upload_conflict",)}[fault]
    assert staging_files(staging) == []
    assert meta_key not in s3.objects
    if fault == "conflict":
        assert s3.objects[cipher_key] == b"an older upload"  # never overwritten
    s3.down = False
    assert not any(s.good for s in artifacts.list_backups(target))


def _stored(target, s3, recipients, tmp_path, ids):
    for backup_id in ids:
        staging = tmp_path / ("s-" + backup_id)
        artifacts.ship(make_artifact(staging / "artifacts", backup_id), staging, target, recipients, T0)
    return artifacts.list_backups(target)


def test_retention_keeps_good_backups_and_needs_a_successful_last_run(tmp_path, keys):
    recipients, _ = keys
    target, s3 = fake_target(tmp_path)
    ids = [f"202609{day:02d}T020000Z-0000{day:04x}" for day in range(1, 11)]
    stored = _stored(target, s3, recipients, tmp_path, ids)
    now = T0  # 2026-10-20: every synthetic backup (2026-09-01..10) is 40-49 days old
    retention = timedelta(days=30)
    assert artifacts.prune_plan(stored, now, retention, 3, last_run_ok=False) == []
    plan = artifacts.prune_plan(stored, now, retention, 3, last_run_ok=True)
    kept = {s.backup_id for s in stored} - {s.backup_id for s in plan}
    assert kept == set(ids[-3:])  # all are 40-49 days old: only the newest 3 good ones stay
    assert all(now.replace(tzinfo=None) - s.taken > retention for s in plan)
    # Even with every backup expired and min_keep 1 the newest good one stays.
    far = T0 + timedelta(days=400)
    plan = artifacts.prune_plan(stored, far, retention, 1, last_run_ok=True)
    assert {s.backup_id for s in stored} - {s.backup_id for s in plan} == {ids[-1]}
    assert artifacts.prune(target, plan) == 9
    remaining = artifacts.list_backups(target)
    assert [s.backup_id for s in remaining] == [ids[-1]] and remaining[0].good


def test_retention_never_deletes_when_no_good_backup_exists(tmp_path, keys):
    recipients, _ = keys
    target, s3 = fake_target(tmp_path)
    s3.objects["pilot/backups/20260101T000000Z-00000001.tar.age"] = b"incomplete upload"
    stored = artifacts.list_backups(target)
    assert [s.good for s in stored] == [False]
    assert artifacts.prune_plan(stored, T0, timedelta(days=30), 1, last_run_ok=True) == []


def test_fetch_refuses_changed_ciphertext_and_wrong_key(tmp_path, keys):
    recipients, secret = keys
    staging = tmp_path / "staging"
    target, s3 = fake_target(tmp_path)
    shipped = artifacts.ship(make_artifact(staging / "artifacts"), staging, target, recipients, T0)
    cipher_key, _ = artifacts.keys(target, shipped["backup_id"])
    good = s3.objects[cipher_key]
    s3.objects[cipher_key] = good[:-1] + bytes([good[-1] ^ 1])
    with pytest.raises(OffsiteError) as changed:
        artifacts.fetch(target, shipped["backup_id"], tmp_path / "r1", secret)
    assert changed.value.code == "ciphertext_mismatch" and staging_files(tmp_path / "r1") == []
    s3.objects[cipher_key] = good
    other = tmp_path / "other-identity.txt"
    other.write_text(str(pyrage.x25519.Identity.generate()) + "\n", encoding="ascii")
    with pytest.raises(OffsiteError) as wrong:
        artifacts.fetch(target, shipped["backup_id"], tmp_path / "r2", other)
    assert wrong.value.code == "decrypt_failed" and staging_files(tmp_path / "r2") == []


def test_extract_refuses_unexpected_members(tmp_path, keys):
    tar_path = tmp_path / "evil.tar"
    with tarfile.open(tar_path, "w") as tar:
        data = b"x"
        info = tarfile.TarInfo("20261020T090000Z-0badc0de/../../escape")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(OffsiteError) as refused:
        artifacts._extract(tar_path, tmp_path / "out", "20261020T090000Z-0badc0de")
    assert refused.value.code == "tar_unexpected_member" and not (tmp_path / "escape").exists()
