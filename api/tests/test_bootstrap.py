"""Empty DB -> migrate -> bootstrap succeeds once -> every later attempt is refused."""

from __future__ import annotations

import io
import logging
import threading
import getpass
import warnings
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app.cli import bootstrap_admin as cli
from app.config import PasswordPolicy
from app.identity.bootstrap import BootstrapRefused, bootstrap_admin
from app.identity.passwords import verify_password
from tests.test_c2c1_bootstrap import installation, world, cli_process

PASSWORD = "Tr0ub4dor&3-long-enough"
API_DIR = Path(__file__).resolve().parents[1]


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def run_cli(engine: Engine, argv: list[str], stdin: io.StringIO, getpass_fn=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        argv,
        stdin=stdin,
        stdout=out,
        stderr=err,
        getpass_fn=getpass_fn or (lambda prompt: pytest.fail("unexpected prompt")),
        engine=engine,
    )
    return code, out.getvalue(), err.getvalue()


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t: conn.scalar(text(f"SELECT count(*) FROM {t}"))
            for t in ("users", "bootstrap_state", "audit_events")
        }


def dump_all_text(engine: Engine) -> str:
    """Every row of every table, as text, to search for leaked secrets."""
    with engine.connect() as conn:
        tables = conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
        return "\n".join(
            str(row) for t in tables for row in conn.execute(text(f'SELECT * FROM "{t}"')).all()
        )


def test_first_bootstrap_creates_admin_with_verifier_and_audit(world) -> None:
    engine = world.runtime
    assert counts(engine) == {"users": 0, "bootstrap_state": 0, "audit_events": 0}

    code, out, err = run_cli(
        engine, ["--email", " Admin@Example.org ", "--password-stdin"], io.StringIO(PASSWORD + "\n")
    )

    assert code == cli.EXIT_OK, err
    assert "id=" in out and "admin@example.org" not in out
    assert counts(engine) == {"users": 1, "bootstrap_state": 1, "audit_events": 1}
    with engine.connect() as conn:
        user = conn.execute(text("SELECT id, email, role, status, password_verifier FROM users")).one()
        marker = conn.scalar(text("SELECT admin_user_id FROM bootstrap_state"))
        audit = conn.execute(text("SELECT actor_type, action, outcome, target_id, details FROM audit_events")).one()
    assert (user.email, user.role, user.status) == ("admin@example.org", "admin", "active")
    assert user.password_verifier.startswith("$argon2id$")
    assert verify_password(user.password_verifier, PASSWORD)
    assert not verify_password(user.password_verifier, PASSWORD + "x")
    assert marker == user.id
    assert (audit.actor_type, audit.action, audit.outcome, audit.target_id) == (
        "system", "admin.bootstrap", "success", str(user.id)
    )
    assert audit.details == {"via": "cli"}


def test_second_bootstrap_refused_without_new_success_audit(world) -> None:
    engine = world.runtime
    assert run_cli(engine, ["--email", "a@example.org", "--password-stdin"], io.StringIO(PASSWORD))[0] == 0

    for email in ("a@example.org", "other@example.org"):
        code, out, err = run_cli(engine, ["--email", email, "--password-stdin"], io.StringIO(PASSWORD + "2"))
        assert code == cli.EXIT_REFUSED
        assert "bootstrap refused" in err
        assert out == ""

    c = counts(engine)
    assert (c["users"], c["bootstrap_state"]) == (1, 1)
    with engine.connect() as conn:
        outcomes = conn.scalars(text("SELECT outcome FROM audit_events ORDER BY id")).all()
        email = conn.scalar(text("SELECT email FROM users"))
    assert outcomes == ["success"]
    assert email == "a@example.org"


def test_bootstrap_refused_if_any_user_already_exists(world) -> None:
    engine = world.runtime
    from app.identity.passwords import hash_password

    with world.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (email, password_verifier) VALUES ('u@example.org', :v)"),
            {"v": hash_password(PASSWORD)},
        )
    code, _, _ = run_cli(engine, ["--email", "a@example.org", "--password-stdin"], io.StringIO(PASSWORD))
    assert code == cli.EXIT_REFUSED
    assert counts(engine)["bootstrap_state"] == 0


def test_concurrent_bootstraps_create_exactly_one_admin(world) -> None:
    engine = world.runtime
    n = 8
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            bootstrap_admin(engine, email=f"admin{i}@example.org", password=PASSWORD, policy=PasswordPolicy())
            outcome = "ok"
        except BootstrapRefused:
            outcome = "refused"
        except Exception as exc:  # noqa: BLE001
            outcome = f"error:{type(exc).__name__}"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(results) == ["ok"] + ["refused"] * (n - 1)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE role = 'admin'")) == 1
        assert conn.scalar(text("SELECT count(*) FROM bootstrap_state")) == 1


def test_interactive_prompt_uses_getpass_and_confirms(world) -> None:
    engine = world.runtime
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return PASSWORD

    code, out, err = run_cli(engine, ["--email", "a@example.org"], FakeTTY(), fake_getpass)
    assert code == cli.EXIT_OK, err
    assert len(prompts) == 2
    assert PASSWORD not in out + err


def test_interactive_mismatch_creates_nothing(world) -> None:
    engine = world.runtime
    answers = iter([PASSWORD, PASSWORD + "-typo"])
    code, _, err = run_cli(engine, ["--email", "a@example.org"], FakeTTY(), lambda p: next(answers))
    assert code == cli.EXIT_INVALID
    assert "do not match" in err
    assert counts(engine) == {"users": 0, "bootstrap_state": 0, "audit_events": 0}


def test_non_tty_without_password_stdin_is_rejected(world) -> None:
    engine = world.runtime
    code, _, err = run_cli(engine, ["--email", "a@example.org"], io.StringIO(PASSWORD))
    assert code == cli.EXIT_INVALID
    assert "--password-stdin" in err
    assert counts(engine)["users"] == 0


def test_getpass_fallback_warning_never_enters_echo_mode(world) -> None:
    def unsafe(prompt):
        warnings.warn("synthetic hidden terminal unavailable", getpass.GetPassWarning)
        pytest.fail("must not continue into echo fallback")
    code, out, err = run_cli(world.runtime, ["--email", "a@example.org"], FakeTTY(), unsafe)
    assert code == cli.EXIT_INVALID and "safe hidden terminal" in err
    assert not out and counts(world.runtime)["users"] == 0


@pytest.mark.parametrize("flag", ["--password", "--pass", "--password=" + PASSWORD])
def test_password_cannot_be_passed_as_argument(world, flag: str, capsys) -> None:
    engine = world.runtime
    with pytest.raises(SystemExit) as info:
        run_cli(engine, ["--email", "a@example.org", flag, PASSWORD], io.StringIO(""))
    assert info.value.code == 2
    captured = capsys.readouterr()
    assert "arguments (values not shown)" in captured.err
    assert PASSWORD not in captured.out + captured.err
    assert counts(engine)["users"] == 0


@pytest.mark.parametrize(
    ("password", "reason"),
    [("short", "at least"), ("x" * 11, "at least"), (" " * 20, "blank"), ("x" * 1025, "at most"), ("", "at least")],
    ids=["short", "one-below-min", "blank", "above-max", "empty"],
)
def test_weak_passwords_are_rejected_without_echo(world, password: str, reason: str) -> None:
    engine = world.runtime
    code, out, err = run_cli(engine, ["--email", "a@example.org", "--password-stdin"], io.StringIO(password))
    assert code == cli.EXIT_INVALID
    assert reason in err
    if password.strip():
        assert password not in out + err
    assert counts(engine) == {"users": 0, "bootstrap_state": 0, "audit_events": 0}


def test_password_equal_to_email_is_rejected(world) -> None:
    engine = world.runtime
    email = "long-admin-name@example.org"
    code, _, err = run_cli(engine, ["--email", email, "--password-stdin"], io.StringIO(email.upper()))
    assert code == cli.EXIT_INVALID
    assert "email" in err


def test_invalid_email_is_rejected(world) -> None:
    engine = world.runtime
    code, _, err = run_cli(engine, ["--email", "not-an-email", "--password-stdin"], io.StringIO(PASSWORD))
    assert code == cli.EXIT_INVALID
    assert counts(engine)["users"] == 0


def test_secrets_never_reach_logs_output_or_audit(world, caplog) -> None:
    engine = world.runtime
    caplog.set_level(logging.DEBUG)
    # Maximum SQL logging: parameters must still be hidden.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.DEBUG)
    try:
        code, out, err = run_cli(engine, ["--email", "a@example.org", "--password-stdin"], io.StringIO(PASSWORD))
        run_cli(engine, ["--email", "b@example.org", "--password-stdin"], io.StringIO(PASSWORD + "-second"))
    finally:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    assert code == 0
    with engine.connect() as conn:
        verifier = conn.scalar(text("SELECT password_verifier FROM users"))

    assert "ras_auth_bootstrap_prepare" in caplog.text  # logging really was active
    for secret in (PASSWORD, verifier):
        assert secret not in caplog.text
        assert secret not in out + err
    db_text = dump_all_text(engine)
    assert PASSWORD not in db_text
    assert db_text.count(verifier) == 1  # only in users.password_verifier, not in audit details


def test_cli_subprocess_end_to_end(world) -> None:
    first = cli_process(world, ["--password-stdin"], scoped=True)
    second = cli_process(world, ["--password-stdin"], scoped=True)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 3
    for proc in (first, second):
        assert PASSWORD not in proc.stdout + proc.stderr
    assert counts(world.runtime)["users"] == 1
