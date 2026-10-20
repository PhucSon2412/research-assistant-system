"""M8b host entrypoint for pilot operations on the deploy/ topology (standard library only).

    python3 scripts/ras_ops.py <command> --env-file <pilot.env> --project <P> --compose-file deploy/compose.yaml [...]

Commands (each prints one JSON summary line last; codes, counts, IDs and times only):

``backup``       maintenance window: lock, edge maintenance flag, stop web/api/worker/beat, capture
                 (pg_dump inside db + objects, one snapshot), start them again, readiness, clear the flag;
                 then encrypt/upload/read back off-host and apply retention. Any failure after writers
                 stopped still restarts them; a failure or a failed reopen runs ``check`` (alert).
``replicate``    one journal replication cycle (the ``journal-replicator`` service runs it continuously)
``check``        alert check: runtime probe in the api container + the ops-check one-shot (mail)
``restore``      restore an off-host backup into this (fresh) project: journal from the local copy
                 and/or the replica (longer chain only when the other is its prefix), db, hold,
                 objects, migrate/provision; leaves the gate closed (``open --recover`` next)
``open``         [--recover] recover, compare the journal with the replica, start everything,
                 readiness, clear the maintenance flag
``drill``        monthly restore drill into a scratch ``*drill*`` project, then tear it down and record it
``drill-record`` record a drill done by hand; ``status`` prints the ops state

Never reads ``.env``: Compose gets ``--env-file`` explicitly and a clean environment (no RAS_*,
POSTGRES_*, MINIO_*, REDIS_*, COMPOSE_* from the caller). From the env file only the non-secret
directory/project names below are read. Only containers/projects this tool started are stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HOST_VARS = ("RAS_DEPLOY_PROJECT", "RAS_OPS_CONFIG_DIR", "RAS_OPS_STATE_DIR", "RAS_OPS_STAGING_DIR",
             "RAS_MAINTENANCE_DIR", "RAS_JOURNAL_DIR")
WRITERS = ("web", "api", "worker", "beat")
CLEAN_PREFIXES = ("RAS_", "POSTGRES_", "MINIO_", "REDIS_", "OPENROUTER_", "COMPOSE_")
FRAME = struct.Struct(">cI")
CHUNK = 1024 * 1024
MARKER_DB = "ras_restore_marker"
READYZ = ("import urllib.request,urllib.error\n"
          "try: print(urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=5).status)\n"
          "except urllib.error.HTTPError as e: print(e.code)\n"
          "except Exception: print(0)")
DEFAULT_WINDOW = {"stop_timeout_seconds": 180, "capture_timeout_seconds": 3600, "start_timeout_seconds": 600,
                  "readiness_timeout_seconds": 300, "ship_timeout_seconds": 7200, "restore_timeout_seconds": 7200,
                  "step_timeout_seconds": 600}


class OpsFailure(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code, self.detail = code, detail


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso() -> str:
    return now().isoformat()


def new_backup_id() -> str:
    return f"{now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def read_env_file(path: Path) -> dict[str, str]:
    """Only the whitelisted, non-secret names; every other line is skipped unread."""
    if path.name == ".env":
        raise OpsFailure("env_file_is_dotenv")  # the developer .env is never an ops source
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise OpsFailure("env_file_unreadable") from None
    for line in text.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() in HOST_VARS:
            values[name.strip()] = value.strip()
    return values


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(CLEAN_PREFIXES)}


class Deploy:
    def __init__(self, env_file: Path, project: str, compose_files: list[Path]):
        if not compose_files:
            raise OpsFailure("compose_file_required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project) or project == "ras":
            raise OpsFailure("project_refused")  # the dev project is never an ops target
        first = compose_files[0].resolve()
        try:
            text = first.read_text(encoding="utf-8")
        except OSError:
            raise OpsFailure("compose_file_unreadable") from None
        if "RAS_DEPLOY_PROJECT" not in text or "RAS_ENVIRONMENT: development" in text:
            raise OpsFailure("compose_file_not_deploy")
        self.env_file, self.project = env_file.resolve(), project
        self.compose_files = [p.resolve() for p in compose_files]
        self.vars = read_env_file(self.env_file)
        missing = [k for k in HOST_VARS if not self.vars.get(k)]
        if missing:
            raise OpsFailure("env_missing", ",".join(missing))
        if self.vars["RAS_DEPLOY_PROJECT"] != project:
            raise OpsFailure("project_mismatch")
        self.state_dir = Path(self.vars["RAS_OPS_STATE_DIR"])
        self.staging_dir = Path(self.vars["RAS_OPS_STAGING_DIR"])
        self.maintenance_dir = Path(self.vars["RAS_MAINTENANCE_DIR"])
        self.journal_dir = Path(self.vars["RAS_JOURNAL_DIR"])
        self.config_dir = Path(self.vars["RAS_OPS_CONFIG_DIR"])
        window = dict(DEFAULT_WINDOW)
        try:
            window.update(json.loads((self.config_dir / "ops.json").read_text(encoding="utf-8")).get("window") or {})
        except (OSError, ValueError):
            raise OpsFailure("ops_config_unreadable") from None
        self.window = window
        (self.state_dir / "logs").mkdir(parents=True, exist_ok=True)

    # --- compose -------------------------------------------------------------------
    def prefix(self) -> list[str]:
        cmd = ["docker", "compose", "--env-file", str(self.env_file), "-p", self.project]
        for path in self.compose_files:
            cmd += ["-f", str(path)]
        return cmd

    def compose(self, *args: str, timeout: float | None = None, input: bytes | None = None,
                check: bool = True) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run([*self.prefix(), *args], input=input, capture_output=True,
                                    timeout=timeout or self.window["step_timeout_seconds"], env=clean_env(),
                                    cwd=self.compose_files[0].parent)
        except subprocess.TimeoutExpired:
            raise OpsFailure("step_timeout", args[0]) from None
        if check and result.returncode != 0:
            self.log(f"compose-{args[0]}", result.stderr)
            raise OpsFailure("compose_failed", f"{args[0]} exit {result.returncode}")
        return result

    def log(self, name: str, data: bytes) -> None:
        path = self.state_dir / "logs" / f"{now().strftime('%Y%m%dT%H%M%S')}-{name}.log"
        with open(path, "ab") as handle:
            handle.write(data[-20000:] if data else b"")

    def running(self) -> set[str]:
        out = self.compose("ps", "--status", "running", "--services", check=False).stdout.decode()
        return set(out.split())

    def readyz(self) -> str:
        r = self.compose("exec", "-T", "api", "python", "-c", READYZ, check=False, timeout=30)
        return r.stdout.decode().strip() if r.returncode == 0 else "unavailable"

    def one_shot_name(self, what: str) -> str:
        return f"{self.project}-{what}-{secrets.token_hex(4)}"

    def remove_container(self, name: str) -> None:
        """Remove exactly the one-shot container this tool named, after checking its project label."""
        found = subprocess.run(["docker", "inspect", name, "--format",
                                "{{index .Config.Labels \"com.docker.compose.project\"}}"],
                               capture_output=True, text=True, env=clean_env())
        if found.returncode == 0 and found.stdout.strip() == self.project:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=clean_env())

    def ops(self, service: str, *args: str, timeout: float | None = None, input: bytes | None = None,
            volumes: tuple[str, ...] = ()) -> tuple[int, dict]:
        """``compose run --rm`` of an ops one-shot; returns its exit code and last JSON line."""
        name = self.one_shot_name(service)
        cmd = ["--profile", "ops", "run", "--rm", "-T", "--no-deps", "--name", name]
        for volume in volumes:
            cmd += ["-v", volume]
        cmd += [service, "python", "-m", "app.cli.ops", *args]
        try:
            result = self.compose(*cmd, timeout=timeout, input=input, check=False)
        except OpsFailure:
            self.remove_container(name)
            raise
        self.log(f"{service}-{args[0]}", result.stderr)
        return result.returncode, last_json(result.stdout)

    # --- maintenance flag and lock --------------------------------------------------------
    def set_maintenance(self, reason: str) -> None:
        self.maintenance_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.maintenance_dir / f".enabled.{secrets.token_hex(4)}"
        tmp.write_text(json.dumps({"reason": reason, "since": iso()}), encoding="utf-8")
        os.replace(tmp, self.maintenance_dir / "enabled")

    def clear_maintenance(self) -> None:
        (self.maintenance_dir / "enabled").unlink(missing_ok=True)

    def maintenance(self) -> dict | None:
        try:
            return json.loads((self.maintenance_dir / "enabled").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"reason": "unreadable"}

    def lock(self, operation: str):
        return Lock(self.state_dir / "maintenance.lock", operation)

    def write_state(self, name: str, value: dict) -> None:
        path = self.state_dir / name
        tmp = path.with_name(f".{name}.{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(value, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def read_state(self, name: str) -> dict:
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


class Lock:
    """One maintenance operation at a time (backup, restore, open, drill). Never broken automatically."""

    def __init__(self, path: Path, operation: str):
        self.path, self.operation = path, operation

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise OpsFailure("maintenance_lock_held") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"operation": self.operation, "since": iso(), "pid": os.getpid()}, handle)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def last_json(raw: bytes) -> dict:
    for line in reversed((raw or b"").decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def report(value: dict) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


# --- backup -----------------------------------------------------------------------------------

def _reader(stream, out: queue.Queue) -> None:
    for line in iter(stream.readline, b""):
        out.put(line)
    out.put(None)


def capture(d: Deploy, backup_id: str) -> dict:
    """The ops one-shot holds the snapshot; pg_dump runs inside db; this process relays the dump in frames."""
    deadline = time.monotonic() + d.window["capture_timeout_seconds"]
    name = d.one_shot_name("ops-capture")
    log = open(d.state_dir / "logs" / f"{backup_id}-capture.log", "ab")
    ops = subprocess.Popen([*d.prefix(), "--profile", "ops", "run", "--rm", "-T", "--no-deps", "--name", name, "ops",
                            "python", "-m", "app.cli.ops", "backup-capture", "--backup-id", backup_id,
                            "--source-project", d.project], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=log, env=clean_env(), cwd=d.compose_files[0].parent)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(ops.stdout, lines), daemon=True).start()
    dump = None
    try:
        first = lines.get(timeout=max(1.0, min(300.0, deadline - time.monotonic())))
        snapshot = last_json(first or b"").get("snapshot")
        if not snapshot or not re.fullmatch(r"[0-9A-F]+-[0-9A-F]+-\d+", snapshot):
            raise OpsFailure("snapshot_not_exported")
        dump = subprocess.Popen([*d.prefix(), "exec", "-T", "db", "sh", "-c",
                                 'exec pg_dump -Fc --snapshot="$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB"', "sh",
                                 snapshot], stdout=subprocess.PIPE, stderr=log, env=clean_env(),
                                cwd=d.compose_files[0].parent)
        digest, size = hashlib.sha256(), 0
        while True:
            if time.monotonic() > deadline:
                raise OpsFailure("capture_timeout")
            block = dump.stdout.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
            ops.stdin.write(FRAME.pack(b"D", len(block)) + block)
        code = dump.wait(timeout=max(1.0, deadline - time.monotonic()))
        trailer = json.dumps({"exit": code, "bytes": size, "sha256": digest.hexdigest()}).encode("ascii")
        ops.stdin.write(FRAME.pack(b"E", len(trailer)) + trailer)
        ops.stdin.close()
        ops.wait(timeout=max(1.0, deadline - time.monotonic()))
        result = {}
        while True:
            line = lines.get(timeout=30)
            if line is None:
                break
            result = last_json(line) or result
        if ops.returncode != 0 or "captured" not in result:
            raise OpsFailure(result.get("refused") or "capture_failed", result.get("detail"))
        return {**result, "dump_bytes": size}
    except (subprocess.TimeoutExpired, queue.Empty):
        raise OpsFailure("capture_timeout") from None
    except (BrokenPipeError, OSError):
        raise OpsFailure("capture_stream_failed") from None
    finally:
        for proc in (dump, ops):
            if proc is not None and proc.poll() is None:
                proc.kill()  # only the client processes this function started
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
        d.remove_container(name)
        log.close()


def reopen(d: Deploy) -> str:
    """Start the writers again and wait for readiness; the flag is cleared only when ready."""
    try:
        d.compose("start", *WRITERS, timeout=d.window["start_timeout_seconds"], check=False)
        deadline = time.monotonic() + d.window["readiness_timeout_seconds"]
        while time.monotonic() < deadline:
            if d.readyz() == "200" and set(WRITERS) <= d.running():
                d.clear_maintenance()
                return "ok"
            time.sleep(3)
    except Exception:  # noqa: BLE001 - any failure keeps the maintenance page up and is alerted
        pass
    return "failed"


def do_backup(d: Deploy, args) -> int:
    started, t0 = iso(), time.monotonic()
    backup_id = new_backup_id()
    run = {"backup_id": backup_id, "started_at": started, "status": "failed", "reopen": "not_needed", "steps": {}}
    window_open, captured = False, False
    exit_code = 1
    try:
        with d.lock("backup"):
            try:
                code, pre = d.ops("ops", "preflight")
                if code != 0:
                    raise OpsFailure("preflight_" + str(pre.get("refused") or "failed"))
                t_window = time.monotonic()
                d.set_maintenance("backup")
                window_open = True
                d.compose("stop", *WRITERS, timeout=d.window["stop_timeout_seconds"])
                if d.running() & set(WRITERS):
                    raise OpsFailure("writers_still_running")
                result = capture(d, backup_id)
                if d.running() & set(WRITERS):
                    raise OpsFailure("writers_started_during_backup")
                captured = True
                run["steps"]["capture"] = result
            except OpsFailure as exc:
                run["code"] = exc.code
            except Exception as exc:  # noqa: BLE001 - never leave writers stopped silently
                run["code"] = "unexpected_" + type(exc).__name__
            finally:
                if window_open:
                    run["reopen"] = reopen(d)
                    run["window_seconds"] = round(time.monotonic() - t_window, 2)
            if captured:
                code, shipped = d.ops("ops", "backup-ship", "--backup-id", backup_id,
                                      timeout=d.window["ship_timeout_seconds"])
                if code != 0:
                    run["code"] = "ship_" + str(shipped.get("refused") or "failed")
                else:
                    run["steps"]["ship"] = shipped
                    run["status"] = "ok"
            if run["status"] != "ok":
                d.ops("ops", "backup-discard", "--backup-id", backup_id)  # plaintext never left behind
            code, pruned = d.ops("ops", "backup-prune", *(["--last-run-ok"] if run["status"] == "ok" else []))
            run["steps"]["prune"] = pruned if code == 0 else {"refused": pruned.get("refused")}
            if run["status"] == "ok" and run["reopen"] == "ok":
                exit_code = 0
    except OpsFailure as exc:
        run["code"] = exc.code
    run["finished_at"] = iso()
    run["seconds"] = round(time.monotonic() - t0, 2)
    state = d.read_state("backup-state.json")
    state["last_run"] = {k: v for k, v in run.items() if k != "steps"}
    if run["status"] == "ok":
        state["last_success"] = state["last_run"]
    d.write_state("backup-state.json", state)
    if exit_code != 0 and not args.no_alert:
        code, run["alert"] = run_check(d)  # alert at once, not at the next timer tick
    report(run)  # last line: the run summary
    return exit_code


# --- replicate / check ------------------------------------------------------------------------

def do_replicate(d: Deploy, args) -> int:
    r = d.compose("run", "--rm", "-T", "--no-deps", "journal-replicator", "python", "-m", "app.cli.journal_replica",
                  "ship", "--once", check=False)
    report(last_json(r.stdout))
    return r.returncode


def do_check(d: Deploy, args) -> int:
    code, summary = run_check(d)
    report({"check": summary, "exit": code})
    return code


def run_check(d: Deploy) -> tuple[int, dict]:
    lock = d.read_state("maintenance.lock") if (d.state_dir / "maintenance.lock").exists() else None
    host = {"maintenance_lock": bool(lock), "maintenance_lock_since": (lock or {}).get("since"),
            "maintenance_lock_timeout_seconds": sum(d.window[k] for k in (
                "stop_timeout_seconds", "capture_timeout_seconds", "start_timeout_seconds",
                "readiness_timeout_seconds", "ship_timeout_seconds")),
            "maintenance_flag": (d.maintenance() or {}).get("reason")}
    probe = {"status": "unavailable"}
    if "api" in d.running():
        r = d.compose("exec", "-T", "api", "python", "-m", "app.cli.ops_probe", check=False, timeout=60)
        if r.returncode == 0 and last_json(r.stdout):
            probe = {"status": "ok", **last_json(r.stdout)}
    facts = json.dumps({"host": host, "probe": probe}).encode("utf-8")
    code, summary = d.ops("ops-check", "check", input=facts, timeout=180)
    if summary.get("fence"):
        d.set_maintenance("journal_rollback")  # traffic stays out until an operator runs the runbook
        summary["maintenance_flag_set"] = True
    return code, summary


# --- restore / open / drill -------------------------------------------------------------------

def psql(d: Deploy, sql: str, database: str = "postgres", read_only: bool = False) -> str:
    prefix = 'PGOPTIONS="-c default_transaction_read_only=on" ' if read_only else ""
    r = d.compose("exec", "-T", "db", "sh", "-c",
                  f'exec env {prefix}psql -X -q -At -v ON_ERROR_STOP=1 -v app_db="$POSTGRES_DB" '
                  f'-U "$POSTGRES_USER" -d "$1"', "sh", database, input=sql.encode("utf-8"), check=False)
    if r.returncode != 0:
        raise OpsFailure("psql_failed")
    return r.stdout.decode()


def target_is_fresh(d: Deploy) -> bool:
    """Read-only ownership check before any start: no volume/container, or the restore marker database."""
    volumes = subprocess.run(["docker", "volume", "ls", "-q", "--filter",
                              f"label=com.docker.compose.project={d.project}"], capture_output=True, text=True,
                             env=clean_env()).stdout.split()
    containers = d.compose("ps", "-a", "-q", check=False).stdout.split()
    if not volumes and not containers:
        return True
    if "db" not in d.running():
        raise OpsFailure("target_db_not_running")
    if psql(d, f"SELECT 1 FROM pg_database WHERE datname = '{MARKER_DB}';", read_only=True).strip() != "1":
        raise OpsFailure("target_not_restore_owned")
    return False


def restore(d: Deploy, backup_id: str, identity: Path, local_journal: Path | None, without_replica: bool) -> dict:
    timings: dict = {}

    def step(name):
        start = time.monotonic()

        class _T:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                timings[name] = round(time.monotonic() - start, 2)
                return False
        return _T()

    if (d.journal_dir / "revocations.jsonl").exists():
        raise OpsFailure("target_journal_exists")
    if not identity.is_file():
        raise OpsFailure("identity_missing")
    with step("target_check"):
        fresh = target_is_fresh(d)
    d.set_maintenance("restore")
    prepared = None
    try:
        with step("start_db_minio_redis"):
            if not fresh:
                d.compose("stop", *WRITERS, "journal-replicator", check=False)
            d.compose("up", "-d", "--wait", "db", "minio", "redis", timeout=d.window["start_timeout_seconds"])
        with step("fetch_decrypt_verify_journal"):
            volumes = [f"{identity.resolve().as_posix()}:/run/ras-identity/identity.txt:ro"]
            extra = ["--identity", "/run/ras-identity/identity.txt"]
            if local_journal is not None:
                volumes.append(f"{local_journal.resolve().as_posix()}:/var/lib/ras/journal-local:ro")
                extra += ["--local-journal", "/var/lib/ras/journal-local"]
            if without_replica:
                extra.append("--without-replica")
            code, prepared = d.ops("ops", "restore-prepare", "--backup-id", backup_id, *extra,
                                   volumes=tuple(volumes), timeout=d.window["restore_timeout_seconds"])
            if code != 0:
                raise OpsFailure(str(prepared.get("refused") or "restore_prepare_failed"))
        with step("database"):
            psql(d, f"""SELECT 'CREATE DATABASE {MARKER_DB}' WHERE NOT EXISTS
                         (SELECT 1 FROM pg_database WHERE datname = '{MARKER_DB}') \\gexec
                       COMMENT ON DATABASE {MARKER_DB} IS 'ras restore target; backup={backup_id} state=in_progress';
                       DROP DATABASE IF EXISTS :"app_db" WITH (FORCE);
                       CREATE DATABASE :"app_db";""")
            load_dump(d, backup_id)
        with step("hold"):
            held = d.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery",
                             "hold", check=False)
            if held.returncode != 0:
                d.log("hold", held.stderr)
                raise OpsFailure("recovery_hold_failed")
        with step("objects"):
            code, objects = d.ops("ops", "restore-objects", "--backup-id", backup_id,
                                  timeout=d.window["restore_timeout_seconds"])
            if code != 0:
                raise OpsFailure(str(objects.get("refused") or "restore_objects_failed"))
        with step("migrate_provision"):
            d.compose("run", "--rm", "-T", "--no-deps", "migrate", timeout=d.window["restore_timeout_seconds"])
            d.compose("run", "--rm", "-T", "--no-deps", "provision")
        status = d.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery",
                           "status", check=False)
        gate = re.search(rb"gate=(\S+)", status.stdout)
        gate = gate.group(1).decode() if gate else "unknown"
        if status.returncode != 3 or gate == "open":
            raise OpsFailure("gate_not_closed_after_restore", gate)
        psql(d, f"COMMENT ON DATABASE {MARKER_DB} IS 'ras restore target; backup={backup_id} state=restored';")
    finally:
        d.ops("ops", "restore-clean", "--backup-id", backup_id)
    return {"restored": backup_id, "project": d.project, "gate": gate, "fresh_project": fresh, "seconds": timings,
            **{k: prepared.get(k) for k in ("journal_source", "journal_relation", "local_seq", "replica_seq",
                                            "local_journal", "journal_head_seq", "backup_journal_db_head_seq",
                                            "objects", "alembic_revision")},
            "next": "ras_ops.py open --recover"}


def load_dump(d: Deploy, backup_id: str) -> None:
    name = d.one_shot_name("ops-dump")
    log = open(d.state_dir / "logs" / f"{backup_id}-restore-dump.log", "ab")
    cwd = d.compose_files[0].parent
    src = subprocess.Popen([*d.prefix(), "--profile", "ops", "run", "--rm", "-T", "--no-deps", "--name", name, "ops",
                            "python", "-m", "app.cli.ops", "restore-dump", "--backup-id", backup_id],
                           stdout=subprocess.PIPE, stderr=log, env=clean_env(), cwd=cwd)
    dst = subprocess.Popen([*d.prefix(), "exec", "-T", "db", "sh", "-c",
                            'exec pg_restore --exit-on-error --single-transaction --no-owner --no-privileges'
                            ' -U "$POSTGRES_USER" -d "$POSTGRES_DB"'], stdin=src.stdout, stdout=log, stderr=log,
                           env=clean_env(), cwd=cwd)
    src.stdout.close()
    try:
        timeout = d.window["restore_timeout_seconds"]
        dst.wait(timeout=timeout)
        src.wait(timeout=60)
    except subprocess.TimeoutExpired:
        for proc in (src, dst):
            proc.kill()
        raise OpsFailure("restore_timeout") from None
    finally:
        d.remove_container(name)
        log.close()
    if src.returncode != 0 or dst.returncode != 0:
        raise OpsFailure("pg_restore_failed", f"source {src.returncode} restore {dst.returncode}")


def do_restore(d: Deploy, args) -> int:
    with d.lock("restore"):
        out = restore(d, args.backup_id, Path(args.identity_file),
                      Path(args.local_journal) if args.local_journal else None, args.without_replica)
    report(out)
    return 0


def open_service(d: Deploy, recover: bool, without_replica: bool, services: tuple[str, ...] = ()) -> dict:
    timings = {}
    t = time.monotonic()
    if recover:
        r = d.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery",
                      "recover", timeout=d.window["restore_timeout_seconds"], check=False)
        d.log("recover", r.stdout + r.stderr)
        if r.returncode != 0 or b"safety_ready=True" not in r.stdout:
            raise OpsFailure("recover_failed")
        timings["recover"] = round(time.monotonic() - t, 2)
    code, compare = d.ops("ops-check", "replica-compare")
    if code != 0 and not without_replica:
        raise OpsFailure("journal_" + str(compare.get("relation") or compare.get("error") or "compare_failed"))
    status = d.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery",
                       "status", check=False)
    if status.returncode != 0 or b"gate=open" not in status.stdout:
        raise OpsFailure("gate_not_open")
    t = time.monotonic()
    if services:
        d.compose("up", "-d", "--wait", "--no-deps", *services, timeout=d.window["start_timeout_seconds"])
    else:
        d.compose("up", "-d", "--wait", timeout=d.window["start_timeout_seconds"])
    deadline = time.monotonic() + d.window["readiness_timeout_seconds"]
    while d.readyz() != "200":
        if time.monotonic() > deadline:
            raise OpsFailure("readiness_failed")
        time.sleep(3)
    timings["start_ready"] = round(time.monotonic() - t, 2)
    return {"opened": d.project, "replica": compare, "seconds": timings}


def do_open(d: Deploy, args) -> int:
    with d.lock("open"):
        out = open_service(d, args.recover, args.without_replica)
        d.clear_maintenance()
    report(out)
    return 0


def latest_good(d: Deploy) -> str:
    code, listed = d.ops("ops", "backup-list")
    good = [b["backup_id"] for b in listed.get("backups", []) if b.get("good")]
    if code != 0 or not good:
        raise OpsFailure("no_good_backup")
    return sorted(good)[-1]


def teardown_drill(d: Deploy) -> None:
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={d.project}"],
                         capture_output=True, text=True, env=clean_env()).stdout.split()
    for identifier in ids:
        project = subprocess.run(["docker", "inspect", identifier, "--format",
                                  "{{index .Config.Labels \"com.docker.compose.project\"}}"],
                                 capture_output=True, text=True, env=clean_env()).stdout.strip()
        if project != d.project:
            raise OpsFailure("drill_teardown_label_mismatch")
    d.compose("--profile", "ops", "down", "-v", "--remove-orphans", timeout=d.window["start_timeout_seconds"])
    (d.journal_dir / "revocations.jsonl").unlink(missing_ok=True)
    for torn in d.journal_dir.glob("revocations.jsonl.torn-*"):
        torn.unlink()


def do_drill(d: Deploy, args) -> int:
    if "drill" not in d.project:
        raise OpsFailure("drill_project_name")  # a drill never runs against a serving project
    t0 = time.monotonic()
    with d.lock("drill"):
        backup_id = args.backup_id if args.backup_id != "latest" else latest_good(d)
        try:
            restored = restore(d, backup_id, Path(args.identity_file), None, False)
            # Never the replicator, edge, worker or beat: the scratch journal must not ship anywhere.
            opened = open_service(d, True, False, services=("api",))
            r = d.compose("exec", "-T", "api", "python", "-m", "app.cli.ops_probe", check=False)
            probe = last_json(r.stdout)
            if probe.get("gate") != "open":
                raise OpsFailure("drill_gate_not_open")
            seconds = round(time.monotonic() - t0, 2)
        finally:
            if not args.keep:
                teardown_drill(d)
    result = {"drill": "ok", "backup_id": backup_id, "restore_seconds": seconds, "restore": restored,
              "open": opened, "probe": probe, "finished_at": iso()}
    if args.record_state_dir:
        record = {"last_success_at": result["finished_at"], "backup_id": backup_id, "restore_seconds": seconds,
                  "journal_relation": restored.get("journal_relation")}
        path = Path(args.record_state_dir) / "drill-state.json"
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    report(result)
    return 0


def do_drill_record(d: Deploy, args) -> int:
    d.write_state("drill-state.json", {"last_success_at": iso(), "backup_id": args.backup_id, "manual": True})
    report({"recorded": args.backup_id})
    return 0


def do_status(d: Deploy, args) -> int:
    report({"maintenance": d.maintenance(), "lock": d.read_state("maintenance.lock") or None,
            "backup": d.read_state("backup-state.json"), "replica": d.read_state("replica-status.json"),
            "drill": d.read_state("drill-state.json"), "running": sorted(d.running())})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/ras_ops.py")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name, fn in (("backup", do_backup), ("replicate", do_replicate), ("check", do_check),
                     ("restore", do_restore), ("open", do_open), ("drill", do_drill),
                     ("drill-record", do_drill_record), ("status", do_status)):
        p = sub.add_parser(name)
        p.add_argument("--env-file", type=Path, required=True)
        p.add_argument("--project", required=True)
        p.add_argument("--compose-file", type=Path, action="append", required=True)
        commands[name] = (p, fn)
    commands["backup"][0].add_argument("--no-alert", action="store_true")
    p = commands["restore"][0]
    p.add_argument("--backup-id", required=True)
    p.add_argument("--identity-file", required=True, help="age identity for this run only (never kept on the server)")
    p.add_argument("--local-journal", help="directory holding a surviving revocations.jsonl (read only)")
    p.add_argument("--without-replica", action="store_true", help="operator decision: replica unreachable")
    p = commands["open"][0]
    p.add_argument("--recover", action="store_true")
    p.add_argument("--without-replica", action="store_true")
    p = commands["drill"][0]
    p.add_argument("--backup-id", default="latest")
    p.add_argument("--identity-file", required=True)
    p.add_argument("--record-state-dir")
    p.add_argument("--keep", action="store_true")
    commands["drill-record"][0].add_argument("--backup-id", required=True)
    args = parser.parse_args(argv)
    try:
        d = Deploy(args.env_file, args.project, args.compose_file)
        return commands[args.command][1](d, args)
    except OpsFailure as exc:
        report({"refused": exc.code, **({"detail": exc.detail} if exc.detail else {}), "command": args.command})
        return 1


if __name__ == "__main__":
    sys.exit(main())
