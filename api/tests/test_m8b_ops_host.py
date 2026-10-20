"""M8b host entrypoint (scripts/ras_ops.py): never .env, explicit project/compose, window always reopens.

Compose and the ops one-shots are replaced by recorders here; the real Docker paths run in
the isolated deploy drills (docs/m8b-pilot-operations.md).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ras_ops", REPO / "scripts" / "ras_ops.py")
ras_ops = importlib.util.module_from_spec(spec)
sys.modules["ras_ops"] = ras_ops
spec.loader.exec_module(ras_ops)

DEPLOY_COMPOSE = REPO / "deploy" / "compose.yaml"


@pytest.fixture
def deploy_dirs(tmp_path):
    dirs = {name: tmp_path / name for name in ("config", "state", "staging", "maintenance", "journal")}
    for path in dirs.values():
        path.mkdir()
    (dirs["config"] / "ops.json").write_text(json.dumps({"window": {"readiness_timeout_seconds": 1,
                                                                     "step_timeout_seconds": 30}}))
    env = tmp_path / "pilot.env"
    env.write_text("\n".join([
        "RAS_DEPLOY_PROJECT=rasm8b-unit", f"RAS_OPS_CONFIG_DIR={dirs['config']}", f"RAS_OPS_STATE_DIR={dirs['state']}",
        f"RAS_OPS_STAGING_DIR={dirs['staging']}", f"RAS_MAINTENANCE_DIR={dirs['maintenance']}",
        f"RAS_JOURNAL_DIR={dirs['journal']}", "POSTGRES_PASSWORD=never-read-by-the-host-tool",
        "MINIO_ROOT_PASSWORD=never-read-either"]) + "\n", encoding="utf-8")
    return env, dirs


def make(env, project="rasm8b-unit", compose=DEPLOY_COMPOSE):
    return ras_ops.Deploy(env, project, [compose])


def test_env_file_only_whitelisted_names_and_never_dotenv(tmp_path, deploy_dirs):
    env, _ = deploy_dirs
    values = ras_ops.read_env_file(env)
    assert set(values) == set(ras_ops.HOST_VARS) and "POSTGRES_PASSWORD" not in values
    dotenv = tmp_path / ".env"
    dotenv.write_text(env.read_text(), encoding="utf-8")
    with pytest.raises(ras_ops.OpsFailure) as refused:
        ras_ops.read_env_file(dotenv)
    assert refused.value.code == "env_file_is_dotenv"


@pytest.mark.parametrize("case,code", [("dev_project", "project_refused"), ("dev_compose", "compose_file_not_deploy"),
                                       ("mismatch", "project_mismatch"), ("missing", "env_missing"),
                                       ("no_compose", "compose_file_required")])
def test_deploy_target_guards(deploy_dirs, case, code):
    env, _ = deploy_dirs
    with pytest.raises(ras_ops.OpsFailure) as refused:
        if case == "dev_project":
            make(env, project="ras")
        elif case == "dev_compose":
            make(env, compose=REPO / "docker-compose.yml")
        elif case == "mismatch":
            make(env, project="rasm8b-other")
        elif case == "missing":
            env.write_text("RAS_DEPLOY_PROJECT=rasm8b-unit\n", encoding="utf-8")
            make(env)
        else:
            ras_ops.Deploy(env, "rasm8b-unit", [])
    assert refused.value.code == code


def test_no_dotenv_is_ever_opened(tmp_path, deploy_dirs, monkeypatch):
    """Countercheck target: any fallback that opens a file named .env fails this test."""
    env, _ = deploy_dirs
    opened = []
    sys.addaudithook(lambda event, args: opened.append(str(args[0])) if event == "open" and args and
                     isinstance(args[0], (str, Path)) else None)
    monkeypatch.setattr(ras_ops.subprocess, "run", lambda *a, **k: type("R", (), {
        "returncode": 0, "stdout": b"", "stderr": b""})())
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-the-caller-shell")
    d = make(env)
    ras_ops.do_status(d, None)
    assert not any(Path(p).name == ".env" for p in opened)
    assert "POSTGRES_PASSWORD" not in ras_ops.clean_env() and "PATH" in ras_ops.clean_env()


def test_compose_always_gets_explicit_env_file_project_and_files(deploy_dirs, monkeypatch):
    env, _ = deploy_dirs
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
    monkeypatch.setattr(ras_ops.subprocess, "run", fake_run)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ras")
    make(env).compose("ps")
    cmd = seen["cmd"]
    assert cmd[:7] == ["docker", "compose", "--env-file", str(env.resolve()), "-p", "rasm8b-unit", "-f"]
    assert "COMPOSE_PROJECT_NAME" not in seen["env"]


def test_lock_is_exclusive(deploy_dirs):
    env, _ = deploy_dirs
    d = make(env)
    with d.lock("backup"):
        with pytest.raises(ras_ops.OpsFailure) as held:
            with d.lock("restore"):
                pass
        assert held.value.code == "maintenance_lock_held"
    with d.lock("restore"):
        pass


class Recorder:
    """Stands in for Docker: records compose calls, ops one-shots and service state."""

    def __init__(self, d, *, ready=True, capture_error=None, ship_ok=True):
        self.d, self.calls, self.ops_calls = d, [], []
        self.up = set(ras_ops.WRITERS) | {"db", "minio", "redis", "edge"}
        self.ready, self.capture_error, self.ship_ok = ready, capture_error, ship_ok
        self.flag_during_capture = None

    def compose(self, *args, **kw):
        self.calls.append(args)
        if args[0] == "stop":
            self.up -= set(args[1:])
        if args[0] == "start":
            self.up |= set(args[1:])
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    def ops(self, service, *args, **kw):
        self.ops_calls.append(args)
        if args[0] == "backup-ship":
            return (0, {"backup_id": "x", "ciphertext_sha256": "0" * 64}) if self.ship_ok else (1, {"refused": "target_unavailable"})
        return 0, {"preflight": "ok"} if args[0] == "preflight" else {}

    def capture(self, d, backup_id):
        self.flag_during_capture = d.maintenance()
        self.writers_during_capture = self.up & set(ras_ops.WRITERS)
        if self.capture_error:
            raise ras_ops.OpsFailure(self.capture_error)
        return {"captured": backup_id}


@pytest.fixture
def backup_run(deploy_dirs, monkeypatch):
    env, dirs = deploy_dirs

    def run(**kw):
        d = make(env)
        rec = Recorder(d, **kw)
        monkeypatch.setattr(d, "compose", rec.compose)
        monkeypatch.setattr(d, "ops", rec.ops)
        monkeypatch.setattr(d, "running", lambda: set(rec.up))
        monkeypatch.setattr(d, "readyz", lambda: "200" if rec.ready else "503")
        monkeypatch.setattr(ras_ops, "capture", rec.capture)
        monkeypatch.setattr(ras_ops.time, "sleep", lambda s: None)
        checks = []
        monkeypatch.setattr(ras_ops, "run_check", lambda d_: (checks.append(1) or 0, {"new": ["backup_failed"]}))
        args = type("A", (), {"no_alert": False})()
        code = ras_ops.do_backup(d, args)
        state = json.loads((dirs["state"] / "backup-state.json").read_text())
        return code, rec, state, checks, d
    return run


def test_successful_window_stops_captures_reopens_and_prunes(backup_run):
    code, rec, state, checks, d = backup_run()
    assert code == 0 and checks == []
    assert rec.flag_during_capture["reason"] == "backup" and rec.writers_during_capture == set()
    assert d.maintenance() is None and set(ras_ops.WRITERS) <= rec.up
    assert state["last_run"]["status"] == "ok" and state["last_run"]["reopen"] == "ok"
    assert state["last_success"]["backup_id"] == state["last_run"]["backup_id"]
    assert ("backup-prune", "--last-run-ok") in rec.ops_calls
    assert isinstance(state["last_run"]["window_seconds"], float)


def test_failure_mid_window_still_reopens_writers_and_alerts(backup_run):
    """Countercheck target: forgetting to restart writers after a failed capture fails this test."""
    code, rec, state, checks, d = backup_run(capture_error="capture_timeout")
    assert code == 1 and checks == [1]
    assert ("start", *ras_ops.WRITERS) in rec.calls and set(ras_ops.WRITERS) <= rec.up
    assert d.maintenance() is None  # ready again, so the page is lifted
    assert state["last_run"]["status"] == "failed" and state["last_run"]["code"] == "capture_timeout"
    assert "last_success" not in state
    assert ("backup-discard", "--backup-id", state["last_run"]["backup_id"]) in rec.ops_calls
    assert ("backup-prune",) in rec.ops_calls and ("backup-prune", "--last-run-ok") not in rec.ops_calls


def test_reopen_failure_keeps_maintenance_page_and_alerts(backup_run):
    code, rec, state, checks, d = backup_run(ready=False)
    assert code == 1 and checks == [1]
    assert d.maintenance()["reason"] == "backup"  # never a silent outage: page stays and mail goes out
    assert state["last_run"]["reopen"] == "failed"


def test_upload_failure_is_a_failed_run_without_pruning(backup_run):
    code, rec, state, checks, d = backup_run(ship_ok=False)
    assert code == 1 and checks == [1] and state["last_run"]["code"] == "ship_target_unavailable"
    assert ("backup-prune", "--last-run-ok") not in rec.ops_calls and d.maintenance() is None


def test_check_sets_the_fence_on_journal_rollback(deploy_dirs, monkeypatch):
    env, _ = deploy_dirs
    d = make(env)
    monkeypatch.setattr(d, "running", lambda: {"api"})
    monkeypatch.setattr(d, "compose", lambda *a, **k: type("R", (), {
        "returncode": 0, "stdout": b'{"gate": "open", "overdue_tombstones": 0}\n', "stderr": b""})())
    monkeypatch.setattr(d, "ops", lambda *a, **k: (0, {"conditions": ["journal_rollback:journal_behind_replica"],
                                                       "fence": True}))
    assert ras_ops.do_check(d, None) == 0
    assert d.maintenance()["reason"] == "journal_rollback"


def test_drill_refuses_a_serving_project(deploy_dirs):
    env, _ = deploy_dirs
    with pytest.raises(ras_ops.OpsFailure) as refused:
        ras_ops.do_drill(make(env), type("A", (), {"backup_id": "latest"})())
    assert refused.value.code == "drill_project_name"
