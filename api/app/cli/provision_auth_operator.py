"""Trusted provisioning for C3 operator recovery; migrator connection from RAS_DB__* only.

``operator --login NAME --operator-id UUID``
    Map an existing, unprivileged LOGIN (created by the deployment, password
    never handled here) to an operator UUID and grant only operator routines.
``register-case --case-file PATH``
    Register one approved case from the restricted case file (outside the repo
    and backups). The same file is later required by the operator CLI.
``revoke-operator --operator-id UUID``
    End a mapping; the login loses its routine grants.

The person running this is trusted to have verified the operator and the
recipient of the User UUID outside the app. Prints codes/IDs only.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from app.clock import utcnow
from app.config import get_settings
from app.db import get_engine
from app.db_roles import provision_operator, register_case, revoke_operator
from app.operator_cases import CaseError, load_case_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="provision_auth_operator", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    operator = sub.add_parser("operator", allow_abbrev=False)
    operator.add_argument("--login", required=True)
    operator.add_argument("--operator-id", type=uuid.UUID, required=True)
    case = sub.add_parser("register-case", allow_abbrev=False)
    case.add_argument("--case-file", type=Path, required=True)
    revoke = sub.add_parser("revoke-operator", allow_abbrev=False)
    revoke.add_argument("--operator-id", type=uuid.UUID, required=True)
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        if args.command == "operator":
            provision_operator(get_engine(), login=args.login, operator_id=args.operator_id)
            print(f"operator provisioned: operator_id={args.operator_id}")
        elif args.command == "register-case":
            recovery_case = load_case_file(args.case_file, max_bytes=settings.auth_recovery.case_file_max_bytes)
            register_case(get_engine(), recovery_case, now=utcnow(),
                          max_lifetime=settings.auth_recovery.case_max_lifetime)
            print(f"case registered: case_id={recovery_case.case_id} action={recovery_case.action}")
        else:
            revoke_operator(get_engine(), operator_id=args.operator_id, now=utcnow())
            print(f"operator revoked: operator_id={args.operator_id}")
    except CaseError as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - type only
        print(f"error: provisioning failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
