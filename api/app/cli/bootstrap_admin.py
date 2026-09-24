"""Create the first Admin account. Succeeds only once per database.

Usage (interactive, password prompted twice without echo):

    python -m app.cli.bootstrap_admin --email admin@example.org

Non-interactive (password read from the first line of stdin, e.g. a secret
file; never pass it as an argument):

    python -m app.cli.bootstrap_admin --email admin@example.org --password-stdin < secret

There is deliberately no ``--password`` option: arguments leak through shell
history and process listings.

Exit codes: 0 created, 2 invalid input, 3 already bootstrapped, 1 other error.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from sqlalchemy import Engine

from app.config import get_settings
from app.db import make_engine
from app.identity.bootstrap import BootstrapRefused, bootstrap_admin
from app.identity.emails import EmailFormatError
from app.identity.passwords import PasswordPolicyError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2
EXIT_REFUSED = 3


class _Parser(argparse.ArgumentParser):
    """Never echo unexpected arguments: a mistyped ``--password <secret>``
    would otherwise be printed back in the usage error."""

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            self.error(
                f"{len(extras)} unrecognized argument(s) (values not shown); "
                "passwords are never accepted as arguments, use the prompt or --password-stdin"
            )
        return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="bootstrap_admin",
        description="Create the first Admin account (one time only).",
        # Prevent '--password' from silently abbreviating '--password-stdin'.
        allow_abbrev=False,
    )
    parser.add_argument("--email", required=True, help="Admin email address (not secret).")
    parser.add_argument("--display-name", default=None)
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from the first line of standard input instead of prompting.",
    )
    return parser


def _read_password(
    args: argparse.Namespace, stdin: TextIO, getpass_fn: Callable[[str], str]
) -> str:
    if args.password_stdin:
        line = stdin.readline()
        return line.rstrip("\r\n")
    if not stdin.isatty():
        raise PasswordPolicyError(
            "no terminal for a hidden prompt; use --password-stdin to read the password from stdin"
        )
    first = getpass_fn("Admin password: ")
    second = getpass_fn("Repeat password: ")
    if first != second:
        raise PasswordPolicyError("passwords do not match")
    return first


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    engine: Engine | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = _parser().parse_args(argv)

    try:
        settings = get_settings()
        password = _read_password(args, stdin, getpass_fn)
        own_engine = engine is None
        if engine is None:
            engine = make_engine(settings.database_url)
        try:
            result = bootstrap_admin(
                engine,
                email=args.email,
                password=password,
                policy=settings.password,
                display_name=args.display_name,
            )
        finally:
            if own_engine:
                engine.dispose()
    except (PasswordPolicyError, EmailFormatError) as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_INVALID
    except BootstrapRefused:
        print("error: bootstrap has already been completed; no account was created", file=stderr)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001
        # Only the exception type: messages may carry connection details.
        print(f"error: bootstrap failed ({type(exc).__name__})", file=stderr)
        return EXIT_ERROR

    print(f"Admin account created: id={result.user_id} email={result.email}", file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
