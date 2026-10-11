"""Operator recovery of an existing account (C3). Prints IDs and codes only.

Run with the provisioned **operator** DB login (RAS_DB__*), never runtime,
recovery, migrator or owner credentials, and only inside a maintenance hold
created by ``python -m app.cli.recovery recover --hold-for-operator``.

``inspect --user-id UUID [--case-id UUID]``
    Read-only: gate/hold, credential (usable with receipt / quarantined),
    status (proven / held / disabled), latest heads and eligibility; with a
    case, whether the journal records it and its outcome by receipt.
``set-credential --user-id UUID --case-id UUID --case-file PATH --expected-installation UUID``
    New password typed twice at a real terminal (no echo). Keeps role, business
    status and status safety; closes every reset token and session.
``resolve-initial-status --user-id UUID --case-id UUID --case-file PATH --expected-installation UUID``
    A NEW initial active decision for an account that is active/held with no
    valid latest status receipt. Never enables a disabled account.

There is no ``--password``, ``--email``, ``--enable``, ``--role``, ``--force``
or ``--confirm-db-is-current``. Nothing is retried automatically; an
uncertain outcome is resolved with ``inspect --case-id`` and ``recover``.

Exit codes: 0 done and proven, 2 invalid input, 3 refused (nothing written),
4 not a success and the journal/DB may hold the attempt (inspect, recover),
5 committed but the journal line is unconfirmed (run recover), 1 other error.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import uuid
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

from app import operator_recovery as ops
from app.clock import utcnow
from app.config import get_settings
from app.identity.passwords import PasswordPolicyError
from app.operator_cases import CaseError, load_case_file

EXIT_OK, EXIT_ERROR, EXIT_INVALID, EXIT_REFUSED, EXIT_OUTCOME, EXIT_UNCONFIRMED = 0, 1, 2, 3, 4, 5


class _Parser(argparse.ArgumentParser):
    """Never echo arguments: a mistyped secret must not come back in a usage error."""

    def error(self, message):
        self.exit(EXIT_INVALID, "error: unrecognized or invalid arguments (values not shown); "
                                "passwords are only read from a hidden terminal prompt\n")

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            self.error("extra")
        return parsed


def _uuid(value: str) -> uuid.UUID:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("uuid")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="auth_recovery", allow_abbrev=False,
                     description="Operator recovery for an existing User UUID (IDs/codes only).")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    inspect = sub.add_parser("inspect", allow_abbrev=False)
    inspect.add_argument("--user-id", type=_uuid, required=True)
    inspect.add_argument("--case-id", type=_uuid)
    for name in ("set-credential", "resolve-initial-status"):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument("--user-id", type=_uuid, required=True)
        command.add_argument("--case-id", type=_uuid, required=True)
        command.add_argument("--case-file", type=Path, required=True)
        command.add_argument("--expected-installation", type=_uuid, required=True)
    return parser


def read_password(stdin: TextIO, getpass_fn: Callable[[str], str]) -> str:
    """Two hidden prompts on a real terminal; never stdin/argv/env/file."""
    if not stdin.isatty():
        raise PasswordPolicyError("a real terminal is required for the hidden password prompt")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            first = getpass_fn("New password: ")
            second = getpass_fn("Repeat new password: ")
        except getpass.GetPassWarning:
            raise PasswordPolicyError("no safe hidden terminal") from None
    if first != second:
        raise PasswordPolicyError("passwords do not match")
    return first


def operator_engine(settings) -> Engine:
    """No pool: closing really ends the DB session, so nothing stays connected across a prompt or hash."""
    from app.identity.log_safety import protect_engine_logs
    engine = create_engine(settings.sqlalchemy_url(), poolclass=NullPool, hide_parameters=True)
    protect_engine_logs(engine)
    return engine


def _line(values: dict) -> str:
    def show(v):
        if v is None:
            return "none"
        if isinstance(v, bool):
            return "yes" if v else "no"
        return str(v)
    return " ".join(f"{k}={show(v)}" for k, v in values.items())


def main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None, getpass_fn: Callable[[str], str] = getpass.getpass,
         engine: Engine | None = None, clock=utcnow) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = _parser().parse_args(argv)
    own_engine = engine is None
    try:
        settings = get_settings()
        if args.command == "set-credential" and not stdin.isatty():
            # Refuse before touching the DB: no password from stdin/pipe/file.
            raise PasswordPolicyError("a real terminal is required for the hidden password prompt")
        if engine is None:
            engine = operator_engine(settings)
        if args.command == "inspect":
            print(_line(ops.inspect(engine, settings, args.user_id, args.case_id)), file=stdout)
            return EXIT_OK
        case = load_case_file(args.case_file, max_bytes=settings.auth_recovery.case_file_max_bytes)
        action = ops.SET_CREDENTIAL if args.command == "set-credential" else ops.RESOLVE_STATUS
        result = ops.run(engine, settings, case, user_id=args.user_id, installation=args.expected_installation,
                         action=action, clock=clock, read_password=lambda: read_password(stdin, getpass_fn))
    except (PasswordPolicyError, CaseError) as exc:
        print(f"error: {getattr(exc, 'code', exc)}", file=stderr)
        return EXIT_INVALID
    except (EOFError, KeyboardInterrupt):
        print("error: input cancelled; nothing was changed", file=stderr)
        return EXIT_INVALID
    except ops.OperatorRefused as exc:
        print(f"refused: {exc.code}; nothing was changed", file=stderr)
        return EXIT_REFUSED
    except ops.OperatorOutcomeError as exc:
        if exc.code == "auth_journal_unavailable":
            print("not confirmed: committed with an unconfirmed journal batch; the gate stays closed, run recover",
                  file=stderr)
            return EXIT_UNCONFIRMED
        print(f"not a success: {exc.code}; do not retry, run inspect --case-id and recover", file=stderr)
        return EXIT_OUTCOME
    except Exception as exc:  # noqa: BLE001 - type only, messages may carry connection details
        print(f"error: operator recovery failed ({type(exc).__name__})", file=stderr)
        return EXIT_ERROR
    finally:
        if own_engine and engine is not None:
            engine.dispose()
    label = "credential recovered" if result.action == ops.SET_CREDENTIAL else "initial status decided"
    print(label + ": " + _line({"user_id": result.user_id, "case_id": result.case_id,
                                "operation_id": result.operation_id, "generation": result.generation,
                                "credential": result.credential_state, "status": result.status,
                                "status_safety": result.status_safety, "account_live": result.account_live}),
          file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
