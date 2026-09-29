"""Export or replay the revocation tombstone journal (M6b-2a restore contract).

``export --out FILE`` writes one JSON object per tombstone: IDs, object key,
reason code, epoch and times; never titles, text, evidence or bytes. Keep the
file outside the database backup it is meant to correct.

``apply --in FILE`` runs against a database restored from a backup, after
``alembic upgrade head`` and **before** the API/web serve traffic: every
tombstone missing from the restored database is re-recorded and its copy is
revoked again (epoch bump, rights and publication revoked, jobs cancelled),
then the cleanup task removes the object and memberships. Safe to repeat.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import get_engine
from app.publication import apply_tombstones, export_tombstones


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli.tombstones")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export").add_argument("--out", required=True, type=Path)
    sub.add_parser("apply").add_argument("--in", dest="source", required=True, type=Path)
    args = parser.parse_args(argv)
    engine = get_engine()
    if args.command == "export":
        with Session(engine) as db:
            entries = export_tombstones(db)
        args.out.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries), encoding="utf-8")
        print(f"Exported {len(entries)} tombstones")
        return
    lines = args.source.read_text(encoding="utf-8").splitlines()
    counts = apply_tombstones(engine, [json.loads(line) for line in lines if line.strip()])
    print("Applied: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"))


if __name__ == "__main__":
    main()
