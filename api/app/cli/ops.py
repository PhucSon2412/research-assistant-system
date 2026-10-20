"""Container side of the M8b operations, driven by ``scripts/ras_ops.py``. Prints codes/counts as JSON.

Runs as the one-shot ``ops`` service (ops image = API image + pinned ``pyrage``):

``preflight``                 config, recipients (public keys only), backup destination ready, staging space
``backup-capture --backup-id ID --source-project P``
    Opens a REPEATABLE READ snapshot (migrator LOGIN), prints ``{"snapshot": ...}``, then
    reads the ``pg_dump`` output the host relays on stdin in frames (``D`` + 4-byte length +
    bytes, then ``E`` + JSON with the dump's exit code, size and SHA-256), copies every
    object and writes the verified artifact into staging (``app.backup``).
``backup-ship --backup-id ID``  encrypt (age), delete plaintext, upload without overwrite, read back
``backup-prune --last-run-ok``  retention on the destination (nothing when the last run failed)
``backup-list``                 good/incomplete backups on the destination
``restore-prepare --backup-id ID --identity FILE [--local-journal DIR] [--without-replica]``
    download/decrypt/verify the artifact, choose the journal (local copy vs replica: the
    longer only when the other is its prefix), check it against the backup, write it as
    this project's (empty) journal.
``restore-dump --backup-id ID``  the artifact's ``database.dump`` on stdout (for ``pg_restore``)
``restore-objects --backup-id ID``  put the bucket back to exactly the artifact's objects
``restore-clean --backup-id ID``  delete the plaintext artifact from staging
``replica-compare``             local journal vs replica (relation, seqs)
``check``                       alert facts (host facts on stdin) -> evaluate, dedupe, mail
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
from datetime import timedelta
from pathlib import Path

from app import backup, journal
from app.backup import BackupError
from app.clock import utcnow
from app.ops import alerts, artifacts, replica
from app.ops import config as ops_config
from app.ops import state as state_file
from app.ops.artifacts import OffsiteError
from app.ops.s3 import Target, TargetError

FRAME = struct.Struct(">cI")
MAX_FRAME = 8 * 1024 * 1024
BACKUP_STATE = "backup-state.json"
DRILL_STATE = "drill-state.json"
ALERT_STATE = "alerts-state.json"


class Refused(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code, self.detail = code, detail


def emit(value: dict) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()


def _journal_path(settings=None) -> Path:
    """The journal file; ``ops-check`` has no database settings, so it reads only the path variable."""
    raw = settings.recovery.journal_path if settings is not None else os.environ.get("RAS_RECOVERY__JOURNAL_PATH")
    if not raw:
        raise Refused("journal_not_configured")
    return Path(raw)


def _staging(cfg) -> Path:
    path = cfg.backup.staging_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- backup ---------------------------------------------------------------------------

def preflight(cfg) -> dict:
    artifacts.read_recipients(cfg.backup.recipients_file)
    props = Target(cfg.backup_target).require_ready()
    staging = _staging(cfg)
    probe = staging / ".preflight"
    probe.write_bytes(b"ok")
    probe.unlink()
    return {"preflight": "ok", "backup_target": props, "staging_free_bytes": shutil.disk_usage(staging).free}


def _read_exact(stream, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        block = stream.read(size - len(out))
        if not block:
            raise Refused("dump_stream_truncated")
        out += block
    return bytes(out)


def receive_dump(stream, target_path: Path) -> dict:
    """Frames from the host until ``E``; the host's own size/SHA-256 of the dump must match ours."""
    digest, size = hashlib.sha256(), 0
    with open(target_path, "xb") as handle:
        while True:
            kind, length = FRAME.unpack(_read_exact(stream, FRAME.size))
            if length > MAX_FRAME:
                raise Refused("dump_frame_too_large")
            payload = _read_exact(stream, length)
            if kind == b"D":
                handle.write(payload)
                digest.update(payload)
                size += len(payload)
            elif kind == b"E":
                trailer = json.loads(payload.decode("ascii"))
                break
            else:
                raise Refused("dump_stream_invalid")
        handle.flush()
    if trailer.get("exit") != 0:
        raise Refused("pg_dump_failed", f"exit {trailer.get('exit')}")
    if trailer.get("bytes") != size or trailer.get("sha256") != digest.hexdigest() or size == 0:
        raise Refused("dump_stream_mismatch")
    return {"bytes": size, "sha256": digest.hexdigest()}


def backup_capture(cfg, settings, backup_id: str, source_project: str) -> dict:
    import psycopg  # noqa: PLC0415

    from app.storage import get_storage  # noqa: PLC0415
    root = _staging(cfg) / "artifacts"
    started_at = utcnow()
    writer = backup.ArtifactWriter(root, backup_id)
    try:
        db = settings.db
        with psycopg.connect(host=db.host, port=db.port, user=db.user, password=db.password.get_secret_value(),
                             dbname=db.name, autocommit=True) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snap_id, taken_at, lsn, txids, version, system_id, revision = conn.execute(backup.SNAPSHOT_SQL).fetchone()
            journal_at = backup.journal_at_backup(conn.execute(backup.JOURNAL_SQL).fetchone())
            rows = [(k, int(s), c) for k, s, c in conn.execute(backup.FILES_SQL).fetchall()]
            emit({"snapshot": snap_id})
            receive_dump(sys.stdin.buffer, writer.path(backup.DUMP))
            database = writer.entry(backup.DUMP)
            conn.execute("ROLLBACK")
        entries = backup.copy_objects(get_storage(settings), writer)
        checked = backup.check_db_objects(rows, entries)
        final = writer.finish({
            "started_at": started_at.isoformat(), "finished_at": utcnow().isoformat(),
            "source": {"compose_project": source_project, "server_version": version, "system_identifier": system_id,
                       "alembic_revision": revision},
            "snapshot": {"id": snap_id, "taken_at": taken_at.isoformat(), "wal_lsn": lsn, "txid_snapshot": txids},
            "writers_stopped": ["api", "web", "worker", "beat"], "journal_at_backup": journal_at, "database": database,
            "objects": {"bucket": settings.storage.bucket, "count": len(entries),
                        "bytes": sum(e["bytes"] for e in entries), "entries": entries},
            "db_object_check": {"file_rows_checked": checked}})
    except BaseException:
        writer.abort()
        raise
    return {"captured": final.name, "database_bytes": database["bytes"], "objects": len(entries),
            "file_rows_checked": checked, "journal_db_head_seq": journal_at and journal_at["db_head_seq"],
            "snapshot_taken_at": taken_at.isoformat()}


def backup_ship(cfg, backup_id: str) -> dict:
    artifact = _staging(cfg) / "artifacts" / backup_id
    return artifacts.ship(artifact, _staging(cfg), Target(cfg.backup_target), cfg.backup.recipients_file, utcnow())


def discard_capture(cfg, backup_id: str) -> dict:
    root = _staging(cfg) / "artifacts"
    for name in (backup_id, backup.PARTIAL + backup_id):
        artifacts.remove_tree(root / name)
    for suffix in (".tar", ".tar.age"):
        (_staging(cfg) / (backup_id + suffix)).unlink(missing_ok=True)
    return {"discarded": backup_id}


def backup_prune(cfg, last_run_ok: bool) -> dict:
    target = Target(cfg.backup_target)
    stored = artifacts.list_backups(target)
    plan = artifacts.prune_plan(stored, utcnow(), cfg.backup_retention, cfg.backup.min_keep, last_run_ok)
    removed = artifacts.prune(target, plan)
    return {"pruned": removed, "pruned_ids": [s.backup_id for s in plan],
            "good": sum(1 for s in stored if s.good) - sum(1 for s in plan if s.good),
            "incomplete": sum(1 for s in stored if not s.good) - sum(1 for s in plan if not s.good)}


def backup_list(cfg) -> dict:
    stored = artifacts.list_backups(Target(cfg.backup_target))
    return {"backups": [{"backup_id": s.backup_id, "good": s.good} for s in stored]}


# --- restore --------------------------------------------------------------------------

def _local_state(directory: Path | None):
    if directory is None:
        return None, None
    path = directory / backup.JOURNAL_FILE
    try:
        state = journal.read(path)
    except journal.JournalError as exc:
        if exc.code == "journal_missing":
            return None, "missing"
        raise Refused("local_" + exc.code) from None
    return state, ("torn" if state.torn is not None else "ok")


def restore_prepare(cfg, settings, backup_id: str, identity: Path, local_dir: Path | None,
                    without_replica: bool) -> dict:
    journal_path = _journal_path(settings)
    if journal_path.exists():
        raise Refused("target_journal_exists")
    artifact = artifacts.fetch(Target(cfg.backup_target), backup_id, _staging(cfg), identity)
    try:
        manifest = backup.verify_artifact(artifact)
        local, local_note = _local_state(local_dir)
        remote, info = (None, {"replica": "not_consulted"}) if without_replica else replica.fetch(
            Target(cfg.journal_replica, read_only=True))
        choice = replica.choose(local, remote)
        backup.check_journal_for_backup(choice.state, manifest)
        journal.create(journal_path, list(choice.state.records))
    except BaseException:
        artifacts.clear_restore_staging(_staging(cfg), backup_id)
        raise
    return {"prepared": backup_id, "journal_source": choice.source, "journal_relation": choice.relation,
            "local_seq": choice.local_seq, "replica_seq": choice.replica_seq, "local_journal": local_note,
            "replica": info.get("replica"), "journal_head_seq": choice.state.head.seq,
            "backup_journal_db_head_seq": (manifest["journal_at_backup"] or {}).get("db_head_seq"),
            "objects": manifest["objects"]["count"], "alembic_revision": manifest["source"]["alembic_revision"]}


def _restored_artifact(cfg, backup_id: str) -> tuple[Path, dict]:
    artifact = _staging(cfg) / f"restore-{backup_id}" / backup_id
    return artifact, backup.verify_artifact(artifact)


def restore_dump(cfg, backup_id: str) -> None:
    artifact, manifest = _restored_artifact(cfg, backup_id)
    with open(artifact / manifest["database"]["path"], "rb") as handle:
        shutil.copyfileobj(handle, sys.stdout.buffer, 1024 * 1024)
    sys.stdout.buffer.flush()


def restore_objects(cfg, settings, backup_id: str) -> dict:
    from app.storage import get_storage  # noqa: PLC0415
    artifact, manifest = _restored_artifact(cfg, backup_id)
    return {"objects": backup.restore_objects(get_storage(settings), artifact, manifest)}


# --- replica / check ------------------------------------------------------------------

def replica_facts(cfg, settings) -> dict:
    out: dict = {}
    try:
        local = replica.read_local(_journal_path(settings))
        out["local_seq"] = local.head.seq
        out["local_torn"] = local.torn is not None
    except (replica.ReplicaError, Refused) as exc:
        return {"error": "local_" + exc.code}
    try:
        remote, _ = replica.fetch(Target(cfg.journal_replica, read_only=True))
    except (replica.ReplicaError, TargetError) as exc:
        return {**out, "error": exc.code}
    out["replica_seq"] = remote.head.seq if remote else 0
    rel = replica.relation(local, remote)
    out["relation"] = rel
    if rel in ("equal", "local_ahead"):
        out["unshipped_records"] = local.head.seq - out["replica_seq"]
    out["journal_id"] = str(local.journal_id)
    return out


def check(cfg, settings, host_facts: dict) -> tuple[int, dict]:
    now = utcnow()
    facts: dict = {"host": host_facts.get("host") or {}, "probe": host_facts.get("probe") or {}, "errors": []}
    facts["replica"] = replica_facts(cfg, settings)
    for name, file in (("replicator", replica.STATUS_FILE), ("backup", BACKUP_STATE), ("drill", DRILL_STATE)):
        try:
            facts[name] = state_file.read(cfg.state_dir / file) or {}
        except (ValueError, OSError):
            facts[name] = {}
            facts["errors"].append(name + "_state_unreadable")
    try:
        stored = artifacts.list_backups(Target(cfg.backup_target, read_only=True))
        good = [s for s in stored if s.good]
        facts["backup"]["good_count"] = len(good)
        facts["backup"]["newest_good_taken_at"] = (good[0].taken.isoformat() + "+00:00") if good else None
    except (TargetError, OffsiteError, ops_config.OpsConfigError) as exc:
        facts["errors"].append("backup_target_" + getattr(exc, "code", "error"))
        facts["backup"]["newest_good_taken_at"] = facts["backup"].get("last_success_taken_at")
    disk = {}
    for which, path in (("journal", _journal_path(settings).parent), ("staging", cfg.backup.staging_dir)):
        try:
            disk[which] = shutil.disk_usage(path).free
        except OSError:
            facts["errors"].append(which + "_disk_unreadable")
    facts["disk"] = disk
    conditions = alerts.evaluate(facts, cfg.alerts, now)
    try:
        previous = state_file.read(cfg.state_dir / ALERT_STATE) or {}
    except (ValueError, OSError):
        previous = {}
    new, repeat, resolved, nxt = alerts.plan(previous.get("active", {}), conditions, now,
                                             timedelta(hours=cfg.alerts.renotify_hours))
    sent_to, delivery_error = 0, None
    if new or repeat or resolved:
        from app.delivery import DeliveryFailed, SmtpDelivery  # noqa: PLC0415
        adapter = SmtpDelivery(cfg.alerts.smtp)
        installation = facts["replica"].get("journal_id") or "unknown"
        for recipient in cfg.alerts.recipients:
            msg = alerts.render(cfg.alerts, new, repeat, resolved, now, installation, recipient)
            try:
                adapter.send_email(msg, recipient)
                sent_to += 1
            except DeliveryFailed as exc:
                delivery_error = exc.code
                break
    if delivery_error is None:
        nxt = alerts.mark_sent(nxt, new + repeat, now)
        state_file.write(cfg.state_dir / ALERT_STATE, {"active": nxt, "checked_at": now.isoformat()})
    summary = {"conditions": sorted(c.key for c in conditions), "new": sorted(c.key for c in new),
               "repeat": sorted(c.key for c in repeat), "resolved": sorted(resolved), "mailed": sent_to,
               "delivery_error": delivery_error,
               "fence": any(c.code == "journal_rollback" for c in conditions) and cfg.alerts.fence_on_journal_rollback}
    return (5 if delivery_error else 0), summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.ops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    p = sub.add_parser("backup-capture")
    p.add_argument("--backup-id", required=True)
    p.add_argument("--source-project", required=True)
    for name in ("backup-ship", "backup-discard", "restore-dump", "restore-objects", "restore-clean"):
        sub.add_parser(name).add_argument("--backup-id", required=True)
    sub.add_parser("backup-prune").add_argument("--last-run-ok", action="store_true")
    sub.add_parser("backup-list")
    p = sub.add_parser("restore-prepare")
    p.add_argument("--backup-id", required=True)
    p.add_argument("--identity", type=Path, required=True)
    p.add_argument("--local-journal", type=Path)
    p.add_argument("--without-replica", action="store_true")
    sub.add_parser("replica-compare")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    try:
        cfg = ops_config.load()
        settings = None
        if args.command in ("backup-capture", "restore-prepare", "restore-objects"):
            from app.config import get_settings  # noqa: PLC0415
            settings = get_settings()
        if args.command == "preflight":
            emit(preflight(cfg))
        elif args.command == "backup-capture":
            emit(backup_capture(cfg, settings, args.backup_id, args.source_project))
        elif args.command == "backup-ship":
            emit(backup_ship(cfg, args.backup_id))
        elif args.command == "backup-discard":
            emit(discard_capture(cfg, args.backup_id))
        elif args.command == "backup-prune":
            emit(backup_prune(cfg, args.last_run_ok))
        elif args.command == "backup-list":
            emit(backup_list(cfg))
        elif args.command == "restore-prepare":
            emit(restore_prepare(cfg, settings, args.backup_id, args.identity, args.local_journal,
                                 args.without_replica))
        elif args.command == "restore-dump":
            restore_dump(cfg, args.backup_id)
        elif args.command == "restore-objects":
            emit(restore_objects(cfg, settings, args.backup_id))
        elif args.command == "restore-clean":
            artifacts.clear_restore_staging(_staging(cfg), args.backup_id)
            emit({"cleaned": args.backup_id})
        elif args.command == "replica-compare":
            facts = replica_facts(cfg, settings)
            emit(facts)
            return 0 if facts.get("relation") in ("equal", "local_ahead") else 3
        elif args.command == "check":
            raw = sys.stdin.buffer.read(64 * 1024)
            code, summary = check(cfg, settings, json.loads(raw.decode("utf-8") or "{}"))
            emit(summary)
            return code
        return 0
    except (Refused, BackupError, OffsiteError, TargetError, replica.ReplicaError, ops_config.OpsConfigError,
            journal.JournalError) as exc:
        detail = getattr(exc, "detail", None)
        emit({"refused": exc.code, **({"detail": detail} if detail else {})})
        return 1


if __name__ == "__main__":
    sys.exit(main())
