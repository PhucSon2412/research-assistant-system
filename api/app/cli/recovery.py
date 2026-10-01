"""Revocation journal registration and restore recovery (M6c-1). Prints codes and counts only.

``status``
    Gate reason (``open`` or why it is closed), journal and database seqs,
    unpurged/unjournaled tombstones. Exit 0 when open, 3 when closed.
``init [--if-new]``
    New install only: an empty database (no item, tombstone or registration)
    and no journal file yet. Writes the journal's genesis and registers the
    database. ``--if-new`` exits 0 without changing anything when those
    conditions do not hold (Compose runs it on every start); the gate then
    stays closed until ``register-existing`` or ``recover``.
``register-existing --confirm-db-is-current``
    A database that already has content, **and is the live one, not a
    restored backup**: writes a new journal holding every existing tombstone.
``hold``
    Right after a restore, before anything starts (M6c-2a): open a recovery
    in the journal so the gate stays closed until ``recover`` finishes, even
    when nothing was revoked after the backup. Refuses, writing nothing, when
    the journal is missing, torn, another install's, or behind the database.
``recover [--confirm-db-is-current]``
    After a restore, with API/web/worker/beat stopped: verify the journal,
    replay what the database lacks, run cleanup until every tombstone is
    purged, remove any revoked copy's object the object store brought back,
    verify, and only then close the recovery (the gate opens). Safe to repeat;
    any failure leaves the gate closed. ``--confirm-db-is-current`` also lets
    it repair the *journal* from the database (lost, truncated or older
    file): only when the database is known to be the live one.
"""

from __future__ import annotations

import argparse
import sys

from app import recovery
from app.clock import utcnow
from app.config import get_settings
from app.db import get_engine
from app.storage import get_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.recovery")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("init").add_argument("--if-new", action="store_true")
    sub.add_parser("register-existing").add_argument("--confirm-db-is-current", action="store_true")
    sub.add_parser("hold")
    sub.add_parser("recover").add_argument("--confirm-db-is-current", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    engine = get_engine()
    try:
        if args.command == "status":
            out = recovery.status(engine, settings)
            print(" ".join(f"{k}={v}" for k, v in out.items()))
            return 0 if out["gate"] == "open" else 3
        if args.command == "init":
            try:
                record = recovery.init_new(engine, settings, utcnow())
            except recovery.RecoveryError as exc:
                if args.if_new and exc.code in ("already_registered", "database_not_new", "journal_exists"):
                    print(f"init skipped: {exc.code}; gate: {recovery.evaluate(settings, engine).reason}")
                    return 0
                raise
            print(f"Registered a new install: journal seq {record.seq}")
            return 0
        if args.command == "register-existing":
            if not args.confirm_db_is_current:
                print("refused: pass --confirm-db-is-current only for the live database, never a restored backup",
                      file=sys.stderr)
                return 2
            _, imported = recovery.register_existing(engine, settings, utcnow())
            print(f"Registered the existing database: {imported} tombstones journaled")
            return 0
        if args.command == "hold":
            recovery_id = recovery.hold(engine, settings, utcnow())
            print(f"Recovery held open: {recovery_id}; gate={recovery.evaluate(settings, engine).reason}")
            return 0
        report = recovery.recover(engine, get_storage(settings), settings, utcnow(),
                                  confirm_db_current=args.confirm_db_is_current)
        print("Recovered: " + " ".join(f"{k}={v}" for k, v in vars(report).items()))
        return 0
    except recovery.RecoveryError as exc:
        print(f"refused: {exc.code}; the recovery gate stays closed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
