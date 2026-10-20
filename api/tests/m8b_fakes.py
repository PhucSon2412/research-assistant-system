"""In-memory S3 stand-in for M8b unit tests (versioned bucket, conditional create, outages).

Only the calls ``app.ops.s3.Target`` makes. Real-MinIO behaviour (If-None-Match,
versioning, object lock) is covered separately by ``test_m8b_s3_minio.py`` and the
isolated deploy drills.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError, EndpointConnectionError

from app import journal
from app.ops.config import S3Target
from app.ops.s3 import Target


def _error(code: str, status: int):
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "op")


class _Body:
    def __init__(self, data: bytes):
        self._io = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._io.read(n)

    def close(self) -> None:
        pass


class FakeS3:
    def __init__(self, versioning: str = "Enabled"):
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, list[bytes]] = {}
        self.metadata: dict[str, dict] = {}
        self.versioning = versioning
        self.down = False
        self.calls: list[tuple[str, str, dict]] = []
        self.corrupt_reads: set[str] = set()

    def _check(self, name, key="", **kw):
        self.calls.append((name, key, kw))
        if self.down:
            raise EndpointConnectionError(endpoint_url="http://offsite.invalid")

    def get_bucket_versioning(self, Bucket):
        self._check("get_bucket_versioning")
        return {"Status": self.versioning} if self.versioning else {}

    def get_object_lock_configuration(self, Bucket):
        self._check("get_object_lock_configuration")
        raise _error("ObjectLockConfigurationNotFoundError", 404)

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, ContentType=None, Metadata=None):
        self._check("put_object", Key, if_none_match=IfNoneMatch)
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        if IfNoneMatch == "*" and Key in self.objects:
            raise _error("PreconditionFailed", 412)
        self.objects[Key] = data
        self.versions.setdefault(Key, []).append(data)
        self.metadata[Key] = dict(Metadata or {})
        return {}

    def get_object(self, Bucket, Key):
        self._check("get_object", Key)
        if Key not in self.objects:
            raise _error("NoSuchKey", 404)
        data = self.objects[Key]
        if Key in self.corrupt_reads:
            data = data[:-1] + b"X"
        return {"Body": _Body(data)}

    def delete_object(self, Bucket, Key):
        self._check("delete_object", Key)
        self.objects.pop(Key, None)
        return {}

    def get_paginator(self, name):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix=""):
                fake._check("list_objects_v2", Prefix)
                items = [{"Key": k, "Size": len(v), "ETag": '"' + hashlib.md5(v).hexdigest() + '"'}
                         for k, v in sorted(fake.objects.items()) if k.startswith(Prefix)]
                yield {"Contents": items}
        return _P()

    def puts(self) -> list[str]:
        return [key for name, key, _ in self.calls if name == "put_object"]


def target_cfg(tmp: Path, bucket: str = "ras-offsite-test", prefix: str = "pilot") -> S3Target:
    creds = tmp / f"{bucket}-creds.json"
    creds.write_text(json.dumps({"access_key_id": "synthetic", "secret_access_key": "synthetic-secret"}))
    return S3Target(endpoint_url="http://offsite.invalid:9000", bucket=bucket, prefix=prefix,
                    credentials_file=creds, read_credentials_file=creds)


def fake_target(tmp: Path, s3: FakeS3 | None = None, **kw) -> tuple[Target, FakeS3]:
    s3 = s3 or FakeS3()
    return Target(target_cfg(tmp, **kw), client=s3), s3


T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def new_journal(path: Path, recoveries: int = 2, journal_id=None) -> journal.JournalState:
    """A real hash-chained journal: genesis + ``recoveries`` begin/end pairs."""
    import uuid
    records = [journal.genesis(journal_id or uuid.uuid4(), T0, "new_install")]
    state = journal.create(path, records)
    return extend(path, state, recoveries)


def extend(path: Path, state: journal.JournalState, recoveries: int = 1) -> journal.JournalState:
    import uuid
    for _ in range(recoveries):
        rid = str(uuid.uuid4())
        begin = journal.next_record(state.head, journal.RECOVERY_BEGIN,
                                    {"recovery_id": rid, "started_at": T0.isoformat(), "db_head_seq": 0})
        end = journal.next_record(begin, journal.RECOVERY_END,
                                  {"recovery_id": rid, "finished_at": T0.isoformat(), "replayed": 0,
                                   "tombstones_purged": 0, "objects_removed": 0})
        state = journal.append(path, state, [begin, end])
    return state
