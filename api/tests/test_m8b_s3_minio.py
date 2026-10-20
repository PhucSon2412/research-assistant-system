"""M8b against a real S3-compatible store: the digest-pinned MinIO of the isolated test host.

Conditional create (If-None-Match) refuses overwrites, versioning is required, object lock
keeps shipped journal versions, and the replica/backup round trips work with real requests.
Uses MINIO_API_HOST_PORT / MINIO_ROOT_* from scripts/m8a_test_host.py (loopback only).
"""

from __future__ import annotations

import json
import os
import uuid

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from app.ops import artifacts, replica
from app.ops.config import S3Target
from app.ops.s3 import Target, TargetError
from tests.m8b_fakes import T0, extend, new_journal

PORT = os.environ.get("MINIO_API_HOST_PORT")
pytestmark = pytest.mark.skipif(not PORT, reason="needs the isolated MinIO of scripts/m8a_test_host.py")


@pytest.fixture
def minio(tmp_path):
    endpoint = f"http://127.0.0.1:{PORT}"
    user, password = os.environ["MINIO_ROOT_USER"], os.environ["MINIO_ROOT_PASSWORD"]
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=user, aws_secret_access_key=password,
                          region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"access_key_id": user, "secret_access_key": password}))
    made = []

    def bucket(*, versioning=True, lock=False):
        name = "m8b-" + uuid.uuid4().hex[:12]
        client.create_bucket(Bucket=name, **({"ObjectLockEnabledForBucket": True} if lock else {}))
        if versioning and not lock:
            client.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
        if lock:
            client.put_object_lock_configuration(Bucket=name, ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}}})
        made.append(name)
        cfg = S3Target(endpoint_url=endpoint, bucket=name, prefix="pilot", credentials_file=creds,
                       read_credentials_file=creds)
        return Target(cfg), name

    yield client, bucket
    for name in made:
        versions = client.list_object_versions(Bucket=name)
        for v in versions.get("Versions", []) + versions.get("DeleteMarkers", []):
            client.delete_object(Bucket=name, Key=v["Key"], VersionId=v["VersionId"], BypassGovernanceRetention=True)
        client.delete_bucket(Bucket=name)


def test_conditional_create_never_overwrites(minio):
    client, bucket = minio
    target, name = bucket()
    assert target.require_ready()["versioning"] == "Enabled"
    target.create("pilot/x", b"first")
    with pytest.raises(TargetError) as conflict:
        target.create("pilot/x", b"second")
    assert conflict.value.code == "target_conflict" and target.get("pilot/x") == b"first"
    assert len(client.list_object_versions(Bucket=name, Prefix="pilot/x")["Versions"]) == 1


def test_unversioned_destination_is_refused(minio):
    _, bucket = minio
    target, _ = bucket(versioning=False)
    with pytest.raises(TargetError) as refused:
        target.require_ready()
    assert refused.value.code == "target_versioning_disabled"


def test_replica_round_trip_with_object_lock(minio, tmp_path):
    client, bucket = minio
    target, name = bucket(lock=True)
    assert target.properties() == {"versioning": "Enabled", "object_lock": "Enabled"}
    path = tmp_path / "journal" / "revocations.jsonl"
    state = new_journal(path, 2)
    assert replica.ship_once(path, target, T0)["remote_seq"] == 5
    state = extend(path, state, 1)
    assert replica.ship_once(path, target, T0)["remote_seq"] == 7
    fetched, info = replica.fetch(target)
    assert fetched.head.hash == state.head.hash and info["head_seq"] == 7
    segment = f"pilot/journal/{state.journal_id}/segments/000000000001.jsonl"
    version = client.list_object_versions(Bucket=name, Prefix=segment)["Versions"][0]["VersionId"]
    with pytest.raises(ClientError):  # the shipped version is locked (governance, no bypass)
        client.delete_object(Bucket=name, Key=segment, VersionId=version)


def test_encrypted_backup_round_trip(minio, tmp_path):
    pyrage = pytest.importorskip("pyrage")
    from tests.test_m8b_artifacts import make_artifact
    _, bucket = minio
    target, _ = bucket()
    identity = pyrage.x25519.Identity.generate()
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(str(identity.to_public()) + "\n", encoding="ascii")
    secret = tmp_path / "id.txt"
    secret.write_text(str(identity) + "\n", encoding="ascii")
    staging = tmp_path / "staging"
    shipped = artifacts.ship(make_artifact(staging / "artifacts"), staging, target, recipients, T0)
    assert list(staging.rglob("*")) == [] or all(p.is_dir() for p in staging.rglob("*"))
    [stored] = artifacts.list_backups(target)
    assert stored.good and stored.backup_id == shipped["backup_id"]
    restored = artifacts.fetch(target, shipped["backup_id"], tmp_path / "restore", secret)
    assert (restored / "manifest.json").exists()
