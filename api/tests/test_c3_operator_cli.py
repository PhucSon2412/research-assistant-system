"""C3 CLI processes with the provisioned operator LOGIN; Windows ConPTY for the real hidden prompt.

In-process prompt fakes below are labelled and are NOT counted as TTY evidence;
only the ConPTY cases exercise a real terminal. HTTP pending checks are unpatched.
"""
import io
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import recovery
from app.cli import auth_recovery as cli
from tests.conftest import API_DIR, db_env
from tests.helpers import DEV_ENV
from tests.test_auth_safety_foundation import NOW, SECRET
from tests.test_c2c1_bootstrap import installation  # noqa: F401 - fixture
from tests.test_c3_operator_recovery import (NEW, account, counts, hold, make_case, quarantine, recover,  # noqa: F401
                                             scalar, user, world)
from app import operator_recovery as ops

FORBIDDEN = [["--password", NEW], ["--pass", NEW], ["--password=" + NEW], ["--email", "x@example.test"],
             ["--enable"], ["--role", "admin"], ["--force"], ["--confirm-db-is-current"]]


def real_case(w, uid, action, **kw):
    """Subprocesses use the real clock, unlike in-process fixtures pinned to NOW."""
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC).replace(microsecond=0)
    return make_case(w, uid, action, approved=now - timedelta(minutes=5), expires=now + timedelta(hours=1), **kw)


def cli_env(w, url):
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAS_")}
    env.update(DEV_ENV)
    env.update(db_env(url))
    env["RAS_RECOVERY__JOURNAL_PATH"] = str(w.path)
    return env


def args_for(c, path, command="set-credential"):
    return [command, "--user-id", str(c.user_id), "--case-id", str(c.case_id), "--case-file", str(path),
            "--expected-installation", str(c.journal_id)]


def case_path(w, c):
    return w.cases / f"{c.case_id}.json"


def run_cli(w, args, *, url=None, stdin=""):
    return subprocess.run([sys.executable, "-m", "app.cli.auth_recovery", *args], input=stdin, capture_output=True,
                          text=True, cwd=API_DIR, env=cli_env(w, url or w.operator_url), timeout=60)


def scan(output, w, uid):
    email = scalar(w, "SELECT email FROM users WHERE id=:u", u=uid)
    verifier = scalar(w, "SELECT password_verifier FROM users WHERE id=:u", u=uid)
    for marker in (NEW, SECRET, email, verifier, "$argon2id$"):
        assert marker not in output


def test_cli_inspect_and_refusals_with_real_logins(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = real_case(w, uid, ops.SET_CREDENTIAL)
    proc = run_cli(w, ["inspect", "--user-id", str(uid), "--case-id", str(c.case_id)])
    assert proc.returncode == 0, proc.stderr
    out = dict(part.split("=", 1) for part in proc.stdout.split())
    assert (out["gate"], out["maintenance_hold"], out["credential"], out["status_proven"], out["account_live"]) == (
        "recovery_in_progress", "ready", "quarantined", "yes", "no")
    assert out["case_used_in_journal"] == "no" and out["case_outcome"] == "no_db_record"
    scan(proc.stdout + proc.stderr, w, uid)
    runtime = run_cli(w, ["inspect", "--user-id", str(uid)], url=w.runtime.url)
    assert runtime.returncode == 3 and "operator_authority_required" in runtime.stderr
    before = (counts(w), w.path.read_bytes())
    piped = run_cli(w, args_for(c, case_path(w, c)), stdin=NEW + "\n" + NEW + "\n")
    assert piped.returncode == 2 and "terminal" in piped.stderr
    for extra in FORBIDDEN:
        proc = run_cli(w, args_for(c, case_path(w, c)) + extra, stdin="")
        assert proc.returncode == 2
        assert NEW not in proc.stdout + proc.stderr and "x@example.test" not in proc.stdout + proc.stderr
    assert (counts(w), w.path.read_bytes()) == before


def test_operator_cannot_register_its_own_case_but_provisioner_can(world):
    w = world
    uid = account(w)
    hold(w)
    c = real_case(w, uid, ops.SET_CREDENTIAL, register=False)
    before=scalar(w,"SELECT count(*) FROM auth_recovery_cases")  # verified fixture already has two used cases
    env = cli_env(w, w.operator_url)
    command = [sys.executable, "-m", "app.cli.provision_auth_operator", "register-case", "--case-file", str(case_path(w, c))]
    denied = subprocess.run(command, capture_output=True, text=True, cwd=API_DIR, env=env, timeout=60)
    assert denied.returncode != 0 and scalar(w, "SELECT count(*) FROM auth_recovery_cases") == before
    assert scalar(w,"SELECT count(*) FROM auth_recovery_cases WHERE case_id=:id",id=c.case_id)==0
    owner = subprocess.run(command, capture_output=True, text=True, cwd=API_DIR, env=cli_env(w, w.engine.url), timeout=60)
    assert owner.returncode == 0, owner.stderr
    assert scalar(w, "SELECT count(*) FROM auth_recovery_cases") == before+1
    assert scalar(w,"SELECT count(*) FROM auth_recovery_cases WHERE case_id=:id",id=c.case_id)==1


@pytest.mark.parametrize("fake", ["eof", "interrupt", "getpass_warning"])
def test_in_process_prompt_failures_change_nothing(world, fake):
    """Labelled fake prompt: covers control flow only, not terminal behaviour (see ConPTY)."""
    import getpass
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    def prompt(_label):
        if fake == "eof":
            raise EOFError
        if fake == "interrupt":
            raise KeyboardInterrupt
        import warnings
        warnings.warn("echo", getpass.GetPassWarning)
        return NEW
    class Tty(io.StringIO):
        def isatty(self):
            return True
    before = (counts(w), w.path.read_bytes())
    err = io.StringIO()
    code = cli.main(args_for(c, case_path(w, c)), stdin=Tty(), stdout=io.StringIO(), stderr=err, getpass_fn=prompt,
                    engine=w.operator, clock=lambda: NOW)
    assert code == 2 and NEW not in err.getvalue()
    assert (counts(w), w.path.read_bytes()) == before
    assert w.operator.pool.checkedout() == 0


winpty = pytest.importorskip("winpty") if sys.platform == "win32" else None


def conpty(w, args, inputs, *, timeout=90):
    """Real Windows pseudo console: the CLI's getpass reads the console, never a pipe."""
    env = cli_env(w, w.operator_url)
    env["PYTHONPATH"] = str(API_DIR)
    command = subprocess.list2cmdline([sys.executable, "-m", "app.cli.auth_recovery", *args])
    proc = winpty.PtyProcess.spawn(command, cwd=str(API_DIR), env=env, dimensions=(40, 200))
    output, step, deadline = "", 0, time.time() + timeout
    while time.time() < deadline and proc.isalive():
        try:
            output += proc.read(4096)
        except EOFError:
            break
        if step < len(inputs) and inputs[step][0] in output:
            proc.write(inputs[step][1])
            step += 1
    while proc.isalive() and time.time() < deadline:
        time.sleep(0.1)
    try:
        output += proc.read(65536)
    except EOFError:
        pass
    assert not proc.isalive(), output
    return proc.exitstatus, output


TYPED_SHORT = "tinypw1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY")
@pytest.mark.parametrize("mode", ["success", "mismatch", "policy", "ctrl_c"])
def test_conpty_hidden_prompt_twice(world, mode):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = real_case(w, uid, ops.SET_CREDENTIAL)
    if mode == "ctrl_c":
        inputs = [("New password:", "\x03")]
    else:
        first = TYPED_SHORT if mode == "policy" else NEW
        second = {"success": NEW, "mismatch": NEW + "-other", "policy": TYPED_SHORT}[mode]
        inputs = [("New password:", first + "\r"), ("Repeat new password:", second + "\r")]
    before = (counts(w), w.path.read_bytes())
    code, output = conpty(w, args_for(c, case_path(w, c)), inputs)
    assert NEW not in output and TYPED_SHORT not in output  # nothing typed is echoed
    scan(output, w, uid)
    if mode == "success":
        assert code == 0 and "credential recovered:" in output and "account_live=yes" in output, output
        assert user(w, uid).credential_state == "usable"
    else:
        assert code == 2, output
        assert (counts(w), w.path.read_bytes()) == before


def test_pending_http_unpatched_during_hold_and_after_recover(world):
    """No bypass: hold closes HTTP; real recover completes the C4 contract."""
    from app.main import app
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    def check():
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 503
            for path in ("/auth/csrf", "/admin/users", "/library/items"):
                assert client.get(path).status_code == 503
            assert client.post("/auth/login", json={"email": "x@example.test", "password": NEW}).status_code == 503
    check()
    assert ops.run(w.operator, w.settings, make_case(w, uid, ops.SET_CREDENTIAL), user_id=uid,
                   installation=w.journal_id, action=ops.SET_CREDENTIAL, clock=lambda: NOW,
                   read_password=lambda: NEW).account_live
    check()
    recover(w)
    assert recovery.evaluate(w.settings, w.runtime).open
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/readyz').status_code == 200
        assert client.get('/auth/csrf').status_code == 200
        assert client.get('/library/items').status_code == 401
