"""Encrypted off-host copies of backup artifacts, their retention and their retrieval (M8b).

A verified artifact (``app.backup``) is packed into one tar, encrypted with
``age`` to the operator's recipient public key(s) (``pyrage``, pinned; no crypto
written here), and the plaintext tar and artifact are deleted as soon as the
ciphertext is on disk. The ciphertext is created on the backup destination
without overwrite, read back and hashed, and only then a metadata record is
created that marks the backup good:

    <prefix>/backups/<backup_id>.tar.age   ciphertext
    <prefix>/backups/<backup_id>.json      size/SHA-256 of ciphertext and tar, manifest hash, times

The private key never lives on the server: a recipients file holding an
``AGE-SECRET-KEY`` is refused. Restore downloads, checks the ciphertext hash,
decrypts with an identity file given for that run only, checks the tar hash,
extracts only the artifact's own file names and verifies the manifest.

Retention (``prune_plan``) only ever deletes when the latest run succeeded, keeps
the newest ``min_keep`` good backups and deletes a good one only when a newer good
one exists. With bucket versioning a delete leaves a noncurrent version until the
bucket's own lifecycle removes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app import backup
from app.backup import BackupError
from app.ops.s3 import Target, TargetError

FORMAT = "ras-backup-offsite/1"
_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
_RECIPIENT = re.compile(r"age1[02-9ac-hj-np-z]{58}")
CHUNK = 1024 * 1024


class OffsiteError(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _age():
    import pyrage  # noqa: PLC0415 - only the ops image and tests need it
    return pyrage


def read_recipients(path: Path) -> list:
    """Public recipients only. Any secret key material in the file is refused outright."""
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise OffsiteError("recipients_unreadable") from None
    if "AGE-SECRET-KEY" in text.upper():
        raise OffsiteError("private_key_on_server")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines or len(lines) > 10 or not all(_RECIPIENT.fullmatch(line) for line in lines):
        raise OffsiteError("recipients_invalid")
    pyrage = _age()
    try:
        return [pyrage.x25519.Recipient.from_str(line) for line in lines]
    except Exception:  # noqa: BLE001
        raise OffsiteError("recipients_invalid") from None


def recipients_fingerprint(path: Path) -> str:
    lines = sorted(line.strip() for line in path.read_text(encoding="ascii").splitlines()
                   if line.strip() and not line.lstrip().startswith("#"))
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()[:16]


def _digest(path: Path) -> tuple[int, str]:
    return backup.file_digest(path)


def _fsync(path: Path) -> None:
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def pack(artifact: Path, out: Path) -> tuple[int, str]:
    """A plain tar of the verified artifact (``<id>/...``), deterministic metadata, never compressed."""
    manifest = backup.verify_artifact(artifact)
    names = [backup.MANIFEST, manifest["database"]["path"], *(e["path"] for e in manifest["objects"]["entries"])]
    with tarfile.open(out, "x", format=tarfile.PAX_FORMAT) as tar:
        for relative in names:
            info = tar.gettarinfo(str(artifact / relative), arcname=f"{manifest['backup_id']}/{relative}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o600
            with open(artifact / relative, "rb") as handle:
                tar.addfile(info, handle)
    _fsync(out)
    return _digest(out)


def encrypt(plain: Path, out: Path, recipients: list) -> tuple[int, str]:
    if out.exists():
        raise OffsiteError("ciphertext_exists")
    try:
        _age().encrypt_file(str(plain), str(out), recipients)
    except Exception:  # noqa: BLE001
        out.unlink(missing_ok=True)
        raise OffsiteError("encrypt_failed") from None
    _fsync(out)
    return _digest(out)


def decrypt(cipher: Path, out: Path, identity_file: Path) -> None:
    pyrage = _age()
    try:
        lines = [line.strip() for line in identity_file.read_text(encoding="ascii").splitlines()
                 if line.strip().startswith("AGE-SECRET-KEY-")]
        identities = [pyrage.x25519.Identity.from_str(line) for line in lines]
    except Exception:  # noqa: BLE001
        raise OffsiteError("identity_invalid") from None
    if not identities:
        raise OffsiteError("identity_invalid")
    try:
        pyrage.decrypt_file(str(cipher), str(out), identities)
    except Exception:  # noqa: BLE001
        out.unlink(missing_ok=True)
        raise OffsiteError("decrypt_failed") from None


def keys(target: Target, backup_id: str) -> tuple[str, str]:
    if not _ID.fullmatch(backup_id):
        raise OffsiteError("backup_id_invalid")
    base = target.key("backups", backup_id)
    return base + ".tar.age", base + ".json"


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise OffsiteError("staging_not_removed")


def ship(artifact: Path, staging: Path, target: Target, recipients_file: Path, now: datetime) -> dict:
    """Pack, encrypt, delete plaintext, upload without overwrite, read back, then mark good. Codes only.

    The plaintext (artifact directory and tar) is removed right after the
    ciphertext is written, on success and on every failure; the local
    ciphertext is removed after the destination returned the same bytes.
    """
    manifest = backup.verify_artifact(artifact)
    backup_id = manifest["backup_id"]
    cipher_key, meta_key = keys(target, backup_id)
    plain_tar = staging / f"{backup_id}.tar"
    cipher = staging / f"{backup_id}.tar.age"
    try:
        recipients = read_recipients(recipients_file)
        tar_bytes, tar_sha = pack(artifact, plain_tar)
        cipher_bytes, cipher_sha = encrypt(plain_tar, cipher, recipients)
    finally:
        plain_tar.unlink(missing_ok=True)
        remove_tree(artifact)
    try:
        target.require_ready()
        try:
            target.create_file(cipher_key, cipher)
        except TargetError as exc:
            raise OffsiteError("upload_conflict" if exc.code == "target_conflict" else exc.code) from None
        size, digest = target.digest(cipher_key)
        if (size, digest) != (cipher_bytes, cipher_sha):
            raise OffsiteError("upload_readback_mismatch")
        meta = {"format": FORMAT, "backup_id": backup_id, "verified": True, "ciphertext_bytes": cipher_bytes,
                "ciphertext_sha256": cipher_sha, "tar_bytes": tar_bytes, "tar_sha256": tar_sha,
                "manifest_sha256": manifest["manifest_sha256"], "snapshot_taken_at": manifest["snapshot"]["taken_at"],
                "alembic_revision": manifest["source"]["alembic_revision"],
                "journal_db_head_seq": (manifest["journal_at_backup"] or {}).get("db_head_seq"),
                "recipients": recipients_fingerprint(recipients_file), "uploaded_at": now.isoformat()}
        target.create(meta_key, json.dumps(meta, sort_keys=True).encode("ascii"), content_type="application/json")
    finally:
        cipher.unlink(missing_ok=True)
    return {"backup_id": backup_id, "ciphertext_bytes": cipher_bytes, "ciphertext_sha256": cipher_sha,
            "tar_sha256": tar_sha, "objects": manifest["objects"]["count"]}


@dataclass(frozen=True)
class Stored:
    backup_id: str
    good: bool
    taken: datetime
    meta: dict | None
    cipher_key: str
    meta_key: str
    has_cipher: bool


def list_backups(target: Target) -> list[Stored]:
    """Every backup id on the destination, newest first; ``good`` only with metadata matching the ciphertext."""
    listing = target.list(target.key("backups") + "/")
    ids = set()
    for key in listing:
        name = key.rsplit("/", 1)[-1]
        match = re.fullmatch(r"(\d{8}T\d{6}Z-[0-9a-f]{8})\.(tar\.age|json)", name)
        if match:
            ids.add(match.group(1))
    out = []
    for backup_id in ids:
        cipher_key, meta_key = keys(target, backup_id)
        meta = None
        if meta_key in listing:
            raw = target.get(meta_key)
            try:
                meta = json.loads(raw.decode("ascii")) if raw is not None else None
            except (UnicodeDecodeError, ValueError):
                meta = None
        has_cipher = cipher_key in listing
        good = bool(meta and meta.get("format") == FORMAT and meta.get("verified") is True and has_cipher
                    and listing[cipher_key][0] == meta.get("ciphertext_bytes") and meta.get("backup_id") == backup_id)
        taken = datetime.strptime(backup_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=None)
        out.append(Stored(backup_id, good, taken, meta, cipher_key, meta_key, has_cipher))
    return sorted(out, key=lambda s: s.backup_id, reverse=True)


def prune_plan(stored: list[Stored], now: datetime, retention: timedelta, min_keep: int,
               last_run_ok: bool) -> list[Stored]:
    """Backups that may be deleted now. Nothing when the latest run failed; never the newest ``min_keep`` good."""
    if not last_run_ok:
        return []
    naive_now = now.replace(tzinfo=None)
    good = sorted((s for s in stored if s.good), key=lambda s: s.backup_id, reverse=True)
    if not good:
        return []
    keep = {s.backup_id for s in good[:max(1, min_keep)]}
    newest_good = good[0].backup_id
    plan = []
    for s in stored:
        if s.backup_id in keep or naive_now - s.taken <= retention:
            continue
        if s.good and not s.backup_id < newest_good:
            continue  # a good backup goes only when a newer good one exists
        plan.append(s)
    return plan


def prune(target: Target, plan: list[Stored]) -> int:
    removed = 0
    for s in plan:
        for key in (s.meta_key, s.cipher_key):  # metadata first: a half-pruned backup is never "good"
            try:
                target.delete(key)
            except TargetError as exc:
                raise OffsiteError("prune_" + exc.code) from None
        removed += 1
    return removed


def fetch(target: Target, backup_id: str, staging: Path, identity_file: Path) -> Path:
    """Download, check, decrypt and extract one backup into ``staging/restore-<id>/<id>``; returns the artifact."""
    cipher_key, meta_key = keys(target, backup_id)
    raw = target.get(meta_key)
    if raw is None:
        raise OffsiteError("backup_not_found")
    meta = json.loads(raw.decode("ascii"))
    if meta.get("format") != FORMAT or meta.get("verified") is not True or meta.get("backup_id") != backup_id:
        raise OffsiteError("backup_not_good")
    root = staging / f"restore-{backup_id}"
    if root.exists():
        raise OffsiteError("restore_staging_exists")
    root.mkdir(parents=True)
    cipher, plain = root / "backup.tar.age", root / "backup.tar"
    try:
        with open(cipher, "xb") as handle:
            target.download(cipher_key, handle)
        if _digest(cipher) != (meta["ciphertext_bytes"], meta["ciphertext_sha256"]):
            raise OffsiteError("ciphertext_mismatch")
        decrypt(cipher, plain, identity_file)
        cipher.unlink()
        if _digest(plain) != (meta["tar_bytes"], meta["tar_sha256"]):
            raise OffsiteError("tar_mismatch")
        artifact = _extract(plain, root, backup_id)
        plain.unlink()
        manifest = backup.verify_artifact(artifact)
        if manifest["manifest_sha256"] != meta["manifest_sha256"]:
            raise OffsiteError("manifest_mismatch")
        return artifact
    except BaseException:
        remove_tree(root)
        raise


def _extract(tar_path: Path, root: Path, backup_id: str) -> Path:
    allowed = re.compile(re.escape(backup_id) + r"/(manifest\.json|database\.dump|objects/\d{6})")
    artifact = root / backup_id
    with tarfile.open(tar_path, "r:") as tar:
        members = tar.getmembers()
        for member in members:
            if not member.isfile() or not allowed.fullmatch(member.name):
                raise OffsiteError("tar_unexpected_member")
        (artifact / "objects").mkdir(parents=True)
        for member in members:
            target = root / member.name
            source = tar.extractfile(member)
            with open(target, "xb") as handle:
                shutil.copyfileobj(source, handle, CHUNK)
    return artifact


def clear_restore_staging(staging: Path, backup_id: str) -> None:
    if not _ID.fullmatch(backup_id):
        raise OffsiteError("backup_id_invalid")
    remove_tree(staging / f"restore-{backup_id}")


def staging_leftovers(staging: Path) -> list[str]:
    """Names (not contents) of anything plaintext-capable left in staging."""
    if not staging.exists():
        return []
    return sorted(p.name for p in staging.iterdir())


__all__ = ["BackupError", "OffsiteError", "ship", "fetch", "list_backups", "prune_plan", "prune", "read_recipients"]
