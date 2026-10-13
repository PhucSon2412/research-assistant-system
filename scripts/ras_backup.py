"""Back up, verify and restore one Compose project's PostgreSQL + object store (M6c-2a). Host side, Conda ``ras``.

    backup        --project P --out ROOT
    verify        ARTIFACT
    copy-journal  (--from-project P | --from-file FILE) --to DIR [--artifact ARTIFACT ...]
    restore       --artifact ARTIFACT --project TARGET --journal-dir DIR

``backup`` refuses while any writer of P (api, web, worker, beat) runs; ``db`` and ``minio``
must run. It dumps the database inside one exported snapshot (``pg_export_snapshot``, read
again for the manifest's snapshot identity, journal head and file rows), copies every object,
checks each file row against its object's SHA-256, and only then writes the manifest and
renames the artifact into place (``app.backup``). The revocation journal is never in it.

``restore`` verifies the artifact and the journal copy before touching anything, then loads
them into TARGET only: never the dev project ``ras`` nor the artifact's source project, its
own volumes (a project that already has volumes must carry this tool's marker database) and
ports (POSTGRES/API/WEB/MINIO host ports must be set to non-default values, and the
published ports are checked). Ownership is checked read-only (``target_is_fresh``) before any
``stop`` or ``up``: a project this tool did not create is never touched. It mounts the journal copy as TARGET's journal
(``RAS_JOURNAL_SOURCE``), loads the dump in one transaction, runs ``recovery hold`` (gate shut
until ``recover``), puts the bucket back to exactly the artifact's objects and migrates. It
never starts api/web/worker/beat; the operator runs ``recovery recover`` next. Running it
again reloads the same state.

Prints codes, counts, IDs and timings only; the last line is JSON. Reads only
``POSTGRES_*``/``MINIO_ROOT_*`` from ``.env``. Paths for artifacts and journal copies must be
outside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

import psycopg  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app import backup, journal  # noqa: E402
from app.backup import BackupError  # noqa: E402
from app.journal import JournalError  # noqa: E402
from app.storage import OriginalStorage  # noqa: E402

ALLOWED = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")
WRITERS = ("api", "web", "worker", "beat")
BUCKET = "ras-originals"  # docker-compose.yml
CONTAINER_JOURNAL = "/var/lib/ras/journal/revocations.jsonl"
DEV_PROJECT = "ras"
MARKER_DB = "ras_restore_marker"
PORTS = {"POSTGRES_HOST_PORT": "5432", "API_HOST_PORT": "8000", "WEB_HOST_PORT": "3000",
         "MINIO_API_HOST_PORT": "9000", "MINIO_CONSOLE_HOST_PORT": "9001"}


def env_subset() -> dict[str, str]:
    """Only the whitelisted names; every other line of .env (keys included) is skipped unread."""
    if all(os.environ.get(name) for name in ALLOWED):
        return {name: os.environ[name] for name in ALLOWED}
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() in ALLOWED:
            value = value.strip()
            values[name.strip()] = value[1:-1] if len(value) >= 2 and value[0] == value[-1] in "'\"" else value
    return values


def now() -> datetime:
    return datetime.now(UTC)


def outside_repo(path: Path, code: str) -> Path:
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise BackupError(code)
    return resolved


def compose_prefix(project: str) -> list[str]:
    # Explicit isolated env file prevents Compose from reading the developer .env.
    env_file = os.environ.get("RAS_COMPOSE_ENV_FILE")
    return ["docker", "compose", *(["--env-file", env_file] if env_file else []), "-p", project]


def compose(project: str, *args: str, extra_env: dict | None = None, check: bool = True, **kwargs):
    env = {**os.environ, **(extra_env or {})}
    if "stdout" not in kwargs:
        kwargs.setdefault("capture_output", True)
    result = subprocess.run([*compose_prefix(project), *args], cwd=ROOT, env=env, **kwargs)
    if check and result.returncode != 0:
        raise BackupError("compose_failed", f"{args[0]} exit {result.returncode}")
    return result


def compose_text(project: str, *args: str, **kwargs) -> str:
    result = subprocess.run([*compose_prefix(project), *args], cwd=ROOT, capture_output=True,
                            encoding="utf-8", errors="replace",
                            env={**os.environ, **kwargs.pop("extra_env", {})}, **kwargs)
    return result.stdout if result.returncode == 0 else ""


def running(project: str) -> set[str]:
    return set(compose_text(project, "ps", "--status", "running", "--services").split())


def published(project: str, service: str, port: int) -> int | None:
    out = compose_text(project, "port", service, str(port)).strip()
    return int(out.rsplit(":", 1)[1]) if out else None


def storage_at(port: int, env: dict) -> OriginalStorage:
    settings = SimpleNamespace(storage=SimpleNamespace(
        bucket=BUCKET, endpoint_url=f"http://127.0.0.1:{port}", access_key=SecretStr(env["MINIO_ROOT_USER"]),
        secret_key=SecretStr(env["MINIO_ROOT_PASSWORD"])))
    return OriginalStorage(settings)


def report(summary: dict) -> None:
    print(json.dumps(summary, sort_keys=True))


@contextmanager
def timed(timings: dict, name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        timings[name] = round(time.monotonic() - started, 2)


# --- backup -------------------------------------------------------------------

def do_backup(args, env: dict) -> int:
    project = args.project
    out = outside_repo(Path(args.out), "backup_inside_repository")
    timings: dict = {}
    started_at = now()
    t0 = time.monotonic()
    up = running(project)
    if up & set(WRITERS):
        raise BackupError("writers_running", ",".join(sorted(up & set(WRITERS))))
    if not {"db", "minio"} <= up:
        raise BackupError("db_or_minio_not_running")
    db_port, minio_port = published(project, "db", 5432), published(project, "minio", 9000)
    if db_port is None or minio_port is None:
        raise BackupError("ports_not_published")
    writer = backup.ArtifactWriter(out, backup.new_backup_id(started_at))
    try:
        with psycopg.connect(host="127.0.0.1", port=db_port, user=env["POSTGRES_USER"],
                             password=env["POSTGRES_PASSWORD"], dbname=env["POSTGRES_DB"], autocommit=True) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")  # held open while pg_dump runs
            snapshot = conn.execute(backup.SNAPSHOT_SQL).fetchone()
            snap_id, taken_at, lsn, txids, version, system_id, revision = snapshot
            journal_at = backup.journal_at_backup(conn.execute(backup.JOURNAL_SQL).fetchone())
            file_rows = [(k, int(s), c) for k, s, c in conn.execute(backup.FILES_SQL).fetchall()]
            with timed(timings, "database"):
                with open(writer.path(backup.DUMP), "xb") as handle:
                    # pg_dump joins the exported snapshot, so the dump and every value above are one state.
                    dumped = compose(project, "exec", "-T", "db", "sh", "-c",
                                     'exec pg_dump -Fc --snapshot="$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                                     "sh", snap_id, check=False, stdout=handle, stderr=subprocess.DEVNULL)
                if dumped.returncode != 0:
                    raise BackupError("pg_dump_failed", f"exit {dumped.returncode}")
                database = writer.entry(backup.DUMP)
            conn.execute("ROLLBACK")
        with timed(timings, "objects"):
            entries = backup.copy_objects(storage_at(minio_port, env), writer)
            checked = backup.check_db_objects(file_rows, entries)
        after = running(project)
        if after & set(WRITERS):
            raise BackupError("writers_started_during_backup", ",".join(sorted(after & set(WRITERS))))
        final = writer.finish({
            "started_at": started_at.isoformat(), "finished_at": now().isoformat(),
            "source": {"compose_project": project, "server_version": version, "system_identifier": system_id,
                       "alembic_revision": revision},
            "snapshot": {"id": snap_id, "taken_at": taken_at.isoformat(), "wal_lsn": lsn, "txid_snapshot": txids},
            "writers_stopped": list(WRITERS), "journal_at_backup": journal_at, "database": database,
            "objects": {"bucket": BUCKET, "count": len(entries), "bytes": sum(e["bytes"] for e in entries),
                        "entries": entries},
            "db_object_check": {"file_rows_checked": checked}})
    except BaseException:
        writer.abort()
        raise
    timings["total"] = round(time.monotonic() - t0, 2)
    report({"backup_id": final.name, "artifact": str(final), "database_bytes": database["bytes"],
            "objects": len(entries), "object_bytes": sum(e["bytes"] for e in entries), "file_rows_checked": checked,
            "snapshot_taken_at": taken_at.isoformat(), "journal_db_head_seq": journal_at and journal_at["db_head_seq"],
            "seconds": timings})
    return 0


# --- verify / copy-journal ------------------------------------------------------

def do_verify(args, env: dict) -> int:
    started = time.monotonic()
    manifest = backup.verify_artifact(Path(args.artifact).resolve())
    report({"verified": manifest["backup_id"], "objects": manifest["objects"]["count"],
            "database_bytes": manifest["database"]["bytes"], "snapshot_taken_at": manifest["snapshot"]["taken_at"],
            "seconds": round(time.monotonic() - started, 2)})
    return 0


def do_copy_journal(args, env: dict) -> int:
    dest = outside_repo(Path(args.to), "journal_copy_inside_repository")
    apart = [ROOT, *(Path(a).resolve() for a in args.artifact)]
    apart += [p.parent for p in apart[1:]]  # the backup root too
    if args.from_file:
        info = backup.copy_journal(Path(args.from_file), dest, now(), apart_from=apart)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.parent / f".journal-incoming-{uuid.uuid4().hex[:8]}"
        try:
            # docker cp only reads the container's journal (works on a stopped container).
            compose(args.from_project, "cp", f"api:{CONTAINER_JOURNAL}", str(staging))
            info = backup.copy_journal(staging, dest, now(), apart_from=apart)
        finally:
            staging.unlink(missing_ok=True)
    report({"journal_copy": str(dest), **{k: info[k] for k in ("head_seq", "records", "source_sha256")}})
    return 0


# --- restore ------------------------------------------------------------------

def psql(project: str, sql: str, extra_env: dict, database: str = "postgres", read_only: bool = False) -> str:
    # read_only: the session refuses any write (default_transaction_read_only), for checks on a project
    # this tool may not own.
    prefix = 'PGOPTIONS="-c default_transaction_read_only=on" ' if read_only else ""
    result = subprocess.run(
        [*compose_prefix(project), "exec", "-T", "db", "sh", "-c",
         f'exec env {prefix}psql -X -q -At -v ON_ERROR_STOP=1 -v app_db="$POSTGRES_DB" -U "$POSTGRES_USER" -d "$1"',
         "sh", database], cwd=ROOT, input=sql, capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ, **extra_env})
    if result.returncode != 0:
        raise BackupError("psql_failed", f"exit {result.returncode}")
    return result.stdout


def target_is_fresh(target: str, extra: dict) -> bool:
    """Read-only: True for a project with no volume and no container; else it must carry the marker.

    Runs only ``docker volume ls``, ``compose ps`` and one read-only ``SELECT`` in a db container
    that is already running: never ``stop``, ``up``, ``run`` or any write, so a project this tool
    did not create is left exactly as it was. A project with volumes whose db is not running is
    refused rather than started.
    """
    volumes = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={target}"],
                             capture_output=True, text=True, check=True).stdout.split()
    containers = compose_text(target, "ps", "-a", "-q", extra_env=extra).split()
    if not volumes and not containers:
        return True
    if "db" not in set(compose_text(target, "ps", "--status", "running", "--services", extra_env=extra).split()):
        raise BackupError("target_db_not_running", target)
    marker = psql(target, f"SELECT 1 FROM pg_database WHERE datname = '{MARKER_DB}';", extra, read_only=True)
    if marker.strip() != "1":
        raise BackupError("target_not_restore_owned", target)
    return False


def do_restore(args, env: dict) -> int:
    target = args.project
    timings: dict = {}
    t0 = time.monotonic()
    artifact = Path(args.artifact).resolve()
    journal_dir = outside_repo(Path(args.journal_dir), "journal_copy_inside_repository")
    with timed(timings, "verify"):
        manifest = backup.verify_artifact(artifact)
        source = manifest["source"]["compose_project"]
        if target in (DEV_PROJECT, source):
            raise BackupError("target_not_isolated", target)
        for name, default in PORTS.items():
            if os.environ.get(name, default) == default:
                raise BackupError("target_ports_not_isolated", name)
        if any(backup.inside(journal_dir, p) or backup.inside(p, journal_dir) for p in (artifact, artifact.parent)):
            raise BackupError("journal_copy_not_apart")
        try:
            state = journal.read(journal_dir / backup.JOURNAL_FILE)
        except JournalError as exc:
            raise BackupError(exc.code) from None
        backup.check_journal_for_backup(state, manifest)
    extra = {"RAS_JOURNAL_SOURCE": str(journal_dir)}
    with timed(timings, "target_check"):
        fresh = target_is_fresh(target, extra)  # read-only; before any stop/up on the target
    with timed(timings, "start_db_minio"):
        if not fresh:
            compose(target, "stop", *WRITERS, extra_env=extra)
        compose(target, "up", "-d", "--wait", "db", "minio", extra_env=extra)
        db_port, minio_port = published(target, "db", 5432), published(target, "minio", 9000)
        if (db_port, minio_port) != (int(os.environ["POSTGRES_HOST_PORT"]), int(os.environ["MINIO_API_HOST_PORT"])):
            raise BackupError("target_ports_not_isolated", "published")
        if minio_port in (published(source, "minio", 9000), published(DEV_PROJECT, "minio", 9000)):
            raise BackupError("target_ports_not_isolated", "minio")
    backup_id = manifest["backup_id"]
    with timed(timings, "database"):
        psql(target, f"""
            SELECT 'CREATE DATABASE {MARKER_DB}' WHERE NOT EXISTS
                (SELECT 1 FROM pg_database WHERE datname = '{MARKER_DB}') \\gexec
            COMMENT ON DATABASE {MARKER_DB} IS 'ras restore target; backup={backup_id} state=in_progress';
            DROP DATABASE IF EXISTS :"app_db" WITH (FORCE);
            CREATE DATABASE :"app_db";
            """, extra)
        with open(artifact / backup.DUMP, "rb") as dump:
            loaded = compose(target, "exec", "-T", "db", "sh", "-c",
                             'exec pg_restore --exit-on-error --single-transaction --no-owner'
                             ' -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                             extra_env=extra, check=False, stdin=dump, capture_output=True)
        if loaded.returncode != 0:
            raise BackupError("pg_restore_failed", f"exit {loaded.returncode}")
    with timed(timings, "hold"):
        # Before objects or migrations: from here the gate is shut until `recover` ends this recovery.
        recovery_env=extra|{"RAS_DB__USER":env["POSTGRES_USER"],"RAS_DB__PASSWORD":env["POSTGRES_PASSWORD"]}
        held = compose(target, "run", "--rm", "--no-deps","-e","RAS_DB__USER","-e","RAS_DB__PASSWORD",
                       "api", "python", "-m", "app.cli.recovery", "hold",
                       extra_env=recovery_env, check=False, encoding="utf-8", errors="replace")
        if held.returncode != 0:
            raise BackupError("recovery_hold_failed", (held.stderr or "").strip().split(";")[0][-80:])
    with timed(timings, "objects"):
        counts = backup.restore_objects(storage_at(minio_port, env), artifact, manifest)
    with timed(timings, "migrate"):
        compose(target, "run", "--rm", "migrate", extra_env=extra)
    status = compose(target, "run", "--rm", "--no-deps","-e","RAS_DB__USER","-e","RAS_DB__PASSWORD",
                     "api", "python", "-m", "app.cli.recovery", "status",
                     extra_env=recovery_env, check=False, encoding="utf-8", errors="replace")
    gate = next((part.split("=", 1)[1] for part in status.stdout.split() if part.startswith("gate=")), "unknown")
    if status.returncode != 3 or gate == "open":
        raise BackupError("gate_not_closed_after_restore", gate)
    psql(target, f"COMMENT ON DATABASE {MARKER_DB} IS 'ras restore target; backup={backup_id} state=restored';",
         extra)
    timings["total"] = round(time.monotonic() - t0, 2)
    report({"restored": backup_id, "project": target, "objects": counts, "gate": gate,
            "journal_head_seq": state.head.seq, "backup_journal_db_head_seq":
                manifest["journal_at_backup"] and manifest["journal_at_backup"]["db_head_seq"],
            "fresh_project": fresh, "seconds": timings,
            "next": "recovery recover with api/web/worker/beat stopped, then start them"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/ras_backup.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("backup")
    p.add_argument("--project", required=True)
    p.add_argument("--out", required=True)
    sub.add_parser("verify").add_argument("artifact")
    p = sub.add_parser("copy-journal")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-project")
    source.add_argument("--from-file")
    p.add_argument("--to", required=True)
    p.add_argument("--artifact", action="append", default=[])
    p = sub.add_parser("restore")
    p.add_argument("--artifact", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--journal-dir", required=True)
    args = parser.parse_args(argv)
    handlers = {"backup": do_backup, "verify": do_verify, "copy-journal": do_copy_journal, "restore": do_restore}
    try:
        return handlers[args.command](args, env_subset())
    except BackupError as exc:
        print(f"{args.command} refused: {exc.code}" + (f" ({exc.detail})" if exc.detail else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
