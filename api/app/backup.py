"""Backup artifacts of PostgreSQL + the paper object store, checked before any restore (M6c-2a).

An artifact is one directory, never inside the repository::

    <root>/<backup_id>/
        manifest.json     written last: snapshot identity, times, every file's size and SHA-256
        database.dump     pg_dump custom format, taken in one exported snapshot
        objects/000001    one file per object of the bucket, in manifest order

It is built under ``<root>/.partial-<backup_id>/`` and renamed to its final name only after
every file and the manifest are written and synced and the whole directory verifies, so an
interrupted or failed backup never carries the name or the manifest of a complete one.
``verify_artifact`` refuses a partial directory, a missing or edited manifest, and any missing,
truncated, changed or unlisted file.

The revocation journal is **never** part of an artifact: restoring it together with the
database would roll both back, which the recovery gate cannot detect (``app.recovery``). A
restore takes a copy of the *current* journal (``copy_journal``) into a directory apart from
the artifact; ``check_journal_for_backup`` refuses a copy that belongs to another install or is
older than the backup. This module reads and writes local files and the object store only; the
host-side tool ``scripts/ras_backup.py`` drives pg_dump/pg_restore and Compose.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from app import journal
from app.journal import JournalError, JournalState

FORMAT = "ras-backup/1"
MANIFEST = "manifest.json"
DUMP = "database.dump"
PARTIAL = ".partial-"
JOURNAL_FILE = "revocations.jsonl"
JOURNAL_INFO = "journal-copy.json"
CHUNK = 1024 * 1024

_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
_OBJECT_PATH = re.compile(r"objects/\d{6}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
TOP_FIELDS = frozenset({"format", "backup_id", "started_at", "finished_at", "source", "snapshot",
                        "writers_stopped", "journal_at_backup", "database", "objects", "db_object_check",
                        "manifest_sha256"})


# Read inside the exported snapshot the dump is taken in, so the manifest describes the dumped state.
SNAPSHOT_SQL = """SELECT pg_export_snapshot(), now(), pg_current_wal_lsn()::text, pg_current_snapshot()::text,
                         current_setting('server_version'), (SELECT system_identifier FROM pg_control_system())::text,
                         (SELECT version_num FROM alembic_version)"""
JOURNAL_SQL = """SELECT r.journal_id::text, r.genesis_hash, (SELECT count(*) FROM revocation_journal_records),
                        h.seq, h.record_hash,
                        (SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed)
                 FROM recovery_registrations r
                 LEFT JOIN LATERAL (SELECT seq, record_hash FROM revocation_journal_records
                                    ORDER BY seq DESC LIMIT 1) h ON true
                 WHERE r.id = 1"""
FILES_SQL = """SELECT f.storage_key, f.byte_size, f.checksum_sha256 FROM source_files f
               JOIN library_items i ON i.id = f.item_id
               WHERE i.publication_state IS DISTINCT FROM 'revoked'"""


def journal_at_backup(row) -> dict | None:
    """The database's journal registration and head (a ``JOURNAL_SQL`` row); ``None`` before registration."""
    if row is None:
        return None
    return {"journal_id": str(row[0]), "genesis_hash": row[1], "db_records": int(row[2]),
            "db_head_seq": int(row[3] or 0), "db_head_hash": row[4], "unconfirmed": int(row[5])}


class BackupError(Exception):
    """A backup, check or restore step refused; ``code`` (and ``detail``: a relative path or name) is safe to print."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def new_backup_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def seal(manifest: dict) -> dict:
    """The manifest with ``manifest_sha256``: SHA-256 of its canonical JSON without that field."""
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return {**body, "manifest_sha256": hashlib.sha256(canonical(body).encode()).hexdigest()}


def file_digest(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":  # Windows cannot open a directory for fsync; NTFS journals its metadata
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def inside(child: Path, parent: Path) -> bool:
    child, parent = child.resolve(), parent.resolve()
    return child == parent or parent in child.parents


# --- writing an artifact -------------------------------------------------------

class ArtifactWriter:
    """Builds ``<root>/.partial-<id>/``; ``finish`` renames it to ``<root>/<id>`` once it verifies."""

    def __init__(self, root: Path, backup_id: str):
        if not _ID.fullmatch(backup_id):
            raise BackupError("backup_id_invalid")
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.backup_id = root, backup_id
        self.final = root / backup_id
        self.partial = root / f"{PARTIAL}{backup_id}"
        if self.final.exists():
            raise BackupError("backup_exists", backup_id)
        self.partial.mkdir()  # never reuses a directory
        (self.partial / "objects").mkdir()

    def path(self, relative: str) -> Path:
        return self.partial / relative

    def entry(self, relative: str) -> dict:
        """Size and SHA-256 of a file already written into the partial directory (read back from disk)."""
        target = self.path(relative)
        with open(target, "rb+") as handle:
            os.fsync(handle.fileno())
        size, digest = file_digest(target)
        return {"path": relative, "bytes": size, "sha256": digest}

    def write_stream(self, relative: str, chunks: Iterable[bytes]) -> dict:
        with open(self.path(relative), "xb") as handle:
            for block in chunks:
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        return self.entry(relative)

    def finish(self, manifest: dict) -> Path:
        sealed = seal({**manifest, "format": FORMAT, "backup_id": self.backup_id})
        _write_new(self.partial / MANIFEST, canonical(sealed).encode("ascii") + b"\n")
        _fsync_dir(self.partial / "objects")
        _fsync_dir(self.partial)
        verify_artifact(self.partial, partial_ok=True)  # never name a directory the verifier would refuse
        os.rename(self.partial, self.final)
        _fsync_dir(self.root)
        return self.final

    def abort(self) -> None:
        shutil.rmtree(self.partial, ignore_errors=True)


def listing(storage) -> dict[str, tuple[int, str, str]]:
    """Every object of the bucket: key -> (size, ETag, Last-Modified)."""
    out = {}
    paginator = storage.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=storage.bucket):
        for item in page.get("Contents", []):
            out[item["Key"]] = (int(item["Size"]), str(item.get("ETag")), str(item.get("LastModified")))
    return out


def _body_chunks(body) -> Iterator[bytes]:
    try:
        while block := body.read(CHUNK):
            yield block
    finally:
        body.close()


def copy_objects(storage, writer: ArtifactWriter) -> list[dict]:
    """Copy every object into the artifact; refuses when the bucket changed while copying."""
    before = listing(storage)
    entries = []
    for number, key in enumerate(sorted(before), start=1):
        if not _KEY.fullmatch(key) or ".." in key.split("/"):
            raise BackupError("object_key_unsupported")
        response = storage.client.get_object(Bucket=storage.bucket, Key=key)
        entry = writer.write_stream(f"objects/{number:06d}", _body_chunks(response["Body"]))
        if entry["bytes"] != before[key][0]:
            raise BackupError("objects_changed_during_backup")
        entries.append({"key": key, "content_type": response.get("ContentType") or "application/octet-stream",
                        **entry})
    if listing(storage) != before:
        raise BackupError("objects_changed_during_backup")
    return entries


def check_db_objects(rows: Iterable[tuple[str, int, str]], entries: list[dict]) -> int:
    """Every file row the database still serves (not a revoked copy) has its object, same size and SHA-256."""
    by_key = {e["key"]: e for e in entries}
    checked = 0
    for key, size, digest in rows:
        entry = by_key.get(key)
        if entry is None:
            raise BackupError("object_missing_for_database_row")
        if entry["bytes"] != size or entry["sha256"] != digest:
            raise BackupError("object_differs_from_database_row")
        checked += 1
    return checked


# --- verifying an artifact -----------------------------------------------------

def _need(condition: bool, code: str = "manifest_corrupt", detail: str | None = None) -> None:
    if not condition:
        raise BackupError(code, detail)


def _check_entry(entry, path_rule) -> None:
    _need(isinstance(entry, dict) and {"path", "bytes", "sha256"} <= set(entry))
    _need(isinstance(entry["path"], str) and path_rule(entry["path"]))
    _need(isinstance(entry["bytes"], int) and not isinstance(entry["bytes"], bool) and entry["bytes"] >= 0)
    _need(isinstance(entry["sha256"], str) and bool(_HEX64.fullmatch(entry["sha256"])))


def read_manifest(artifact: Path) -> dict:
    """The manifest, if it is intact (format, fields, self-checksum). Files are not checked here."""
    try:
        raw = (artifact / MANIFEST).read_bytes()
    except FileNotFoundError:
        raise BackupError("manifest_missing") from None
    except OSError:
        raise BackupError("manifest_unreadable") from None
    try:
        manifest = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError("manifest_corrupt") from None
    _need(isinstance(manifest, dict) and set(manifest) == TOP_FIELDS)
    _need(manifest["format"] == FORMAT, "format_unsupported")
    _need(seal(manifest)["manifest_sha256"] == manifest["manifest_sha256"], "manifest_checksum_mismatch")
    return manifest


def verify_artifact(artifact: Path, *, partial_ok: bool = False) -> dict:
    """Refuse anything but a complete artifact whose every file matches its manifest; return the manifest."""
    if artifact.name.startswith(PARTIAL) and not partial_ok:
        raise BackupError("artifact_partial")
    if not artifact.is_dir():
        raise BackupError("artifact_missing")
    manifest = read_manifest(artifact)
    expected_name = f"{PARTIAL}{manifest['backup_id']}" if partial_ok else manifest["backup_id"]
    _need(artifact.name == expected_name, "manifest_mismatch")
    database, objects = manifest["database"], manifest["objects"]
    _check_entry(database, lambda p: p == DUMP)
    _need(isinstance(objects, dict) and isinstance(objects.get("entries"), list))
    entries = objects["entries"]
    for entry in entries:
        _check_entry(entry, lambda p: bool(_OBJECT_PATH.fullmatch(p)))
        _need(isinstance(entry.get("key"), str) and bool(_KEY.fullmatch(entry["key"])))
        _need(isinstance(entry.get("content_type"), str) and 0 < len(entry["content_type"]) <= 200)
    _need(len({e["key"] for e in entries}) == len(entries) and len({e["path"] for e in entries}) == len(entries))
    _need(objects.get("count") == len(entries) and objects.get("bytes") == sum(e["bytes"] for e in entries))
    listed = [database, *entries]
    names = {e["path"] for e in listed} | {MANIFEST}
    for path in sorted(p for p in artifact.rglob("*") if p.is_file()):
        relative = path.relative_to(artifact).as_posix()
        if relative not in names:
            raise BackupError("unexpected_file", relative)
    for entry in listed:
        target = artifact / entry["path"]
        if not target.is_file():
            raise BackupError("file_missing", entry["path"])
        size, digest = file_digest(target)
        if size != entry["bytes"]:
            raise BackupError("file_size_mismatch", entry["path"])
        if digest != entry["sha256"]:
            raise BackupError("file_checksum_mismatch", entry["path"])
    return manifest


# --- the journal copy ----------------------------------------------------------

def copy_journal(source: Path, dest_dir: Path, now: datetime, *, apart_from: Iterable[Path] = ()) -> dict:
    """Copy the current journal to ``dest_dir/revocations.jsonl``; the source is only read.

    ``dest_dir`` must be apart from every path in ``apart_from`` (backup artifacts and their
    root, the repository): a journal kept with the backup would be restored with it. Refuses an
    existing copy (a later restore reuses it as it is), a torn or corrupt journal, and a source
    that changed while it was read (writers must be stopped).
    """
    dest_dir = dest_dir.resolve()
    for other in apart_from:
        if inside(dest_dir, other) or inside(other, dest_dir):
            raise BackupError("journal_copy_not_apart")
    target = dest_dir / JOURNAL_FILE
    if source.resolve() == target:
        raise BackupError("journal_copy_is_source")
    if target.exists() or (dest_dir / JOURNAL_INFO).exists():
        raise BackupError("journal_copy_exists")
    try:
        data = source.read_bytes()
        state = journal.parse(data)
        again = source.read_bytes()
    except FileNotFoundError:
        raise BackupError("journal_missing") from None
    except OSError:
        raise BackupError("journal_unreadable") from None
    except JournalError as exc:
        raise BackupError(exc.code) from None
    if state.torn is not None:
        raise BackupError("journal_torn")
    if again != data:
        raise BackupError("journal_changed_during_copy")
    dest_dir.mkdir(parents=True, exist_ok=True)
    _write_new(target, data)
    info = {"copied_at": now.isoformat(), "source_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "records": len(state.records), "head_seq": state.head.seq, "head_hash": state.head.hash,
            "journal_id": str(state.journal_id), "genesis_hash": state.genesis_hash}
    _write_new(dest_dir / JOURNAL_INFO, canonical(info).encode("ascii") + b"\n")
    _fsync_dir(dest_dir)
    return info


def check_journal_for_backup(state: JournalState, manifest: dict) -> None:
    """The journal copy must be this install's and hold every record the backed-up database had.

    Necessary, not sufficient: a journal rolled back together with the database still
    passes; nothing compares either with a third copy.
    """
    if state.torn is not None:
        raise BackupError("journal_torn")
    at = manifest["journal_at_backup"]
    if at is None:  # backed up before registration: recover adopts the database
        return
    if str(state.journal_id) != at["journal_id"] or state.genesis_hash != at["genesis_hash"]:
        raise BackupError("journal_mismatch")
    seq = at["db_head_seq"]
    if seq == 0:
        return
    if seq > state.head.seq:
        raise BackupError("journal_behind_backup")
    if state.records[seq - 1].hash != at["db_head_hash"]:
        raise BackupError("journal_diverged")


# --- restoring objects ---------------------------------------------------------

def restore_objects(storage, artifact: Path, manifest: dict) -> dict:
    """Make the bucket hold exactly the artifact's objects: put missing or different ones, delete the rest.

    Running it again changes nothing. Each file is hashed again as it is read for upload, and
    afterwards every object is read back from the store and must have the artifact's SHA-256
    (same-size wrong bytes included): otherwise ``objects_restore_mismatch``.
    """
    storage.ensure_bucket()
    wanted = {e["key"]: e for e in manifest["objects"]["entries"]}
    present = listing(storage)
    counts = {"put": 0, "unchanged": 0, "deleted": 0}
    for key in sorted(set(present) - set(wanted)):
        storage.delete(key)
        counts["deleted"] += 1
    for key, entry in wanted.items():
        if key in present and present[key][0] == entry["bytes"] and stored_digest(storage, key) == entry["sha256"]:
            counts["unchanged"] += 1
            continue
        data = (artifact / entry["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise BackupError("file_checksum_mismatch", entry["path"])
        storage.put(key, io.BytesIO(data), entry["content_type"])
        counts["put"] += 1
    after = listing(storage)
    if set(after) != set(wanted) or any(after[k][0] != wanted[k]["bytes"] for k in wanted):
        raise BackupError("objects_restore_incomplete")
    for key, entry in wanted.items():  # read back: size alone does not prove the bytes
        if stored_digest(storage, key) != entry["sha256"]:
            raise BackupError("objects_restore_mismatch")
    return counts


def stored_digest(storage, key: str) -> str:
    """SHA-256 of an object as the store returns it now."""
    digest = hashlib.sha256()
    for block in _body_chunks(storage.open(key)):
        digest.update(block)
    return digest.hexdigest()
