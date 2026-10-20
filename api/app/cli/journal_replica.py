"""Off-host journal replicator (M8b). Prints codes and counts only.

``ship [--once] [--interval N]``
    Read the local journal (mounted read-only), ship complete records missing on the
    replica destination, record ``replica-status.json`` in the ops state directory.
    A fork, a rolled-back local journal or another journal stops shipping (nothing
    is written) and is recorded as the status code; the loop keeps checking.
    Exit (``--once``): 0 shipped/up to date, 3 refused (fork etc.), 4 destination error.
``verify``
    Download and verify the whole replica chain; compare with the local journal.

It holds no database connection, no journal lock and no credential except the
replica destination's (secret file named in the ops config).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from app.clock import utcnow
from app.ops import config as ops_config
from app.ops import replica
from app.ops.s3 import Target, TargetError

JOURNAL = Path("/var/lib/ras/journal/revocations.jsonl")


def _journal_path() -> Path:
    return Path(os.environ.get("RAS_RECOVERY__JOURNAL_PATH") or JOURNAL)


def cycle(cfg, target: Target, cache: dict) -> tuple[int, dict]:
    now = utcnow()
    try:
        result = replica.ship_once(_journal_path(), target, now, max_records=cfg.replicator.max_segment_records,
                                   max_segments=cfg.replicator.max_segments_per_cycle, cache=cache)
    except replica.ReplicaError as exc:
        cache.clear()
        return 3, replica.record_status(cfg.state_dir, now, None, exc.code)
    except TargetError as exc:
        cache.clear()
        return 4, replica.record_status(cfg.state_dir, now, None, exc.code)
    return 0, replica.record_status(cfg.state_dir, now, result, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.journal_replica")
    sub = parser.add_subparsers(dest="command", required=True)
    ship = sub.add_parser("ship")
    ship.add_argument("--once", action="store_true")
    ship.add_argument("--interval", type=float)
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    try:
        cfg = ops_config.load()
        target = Target(cfg.journal_replica)
    except ops_config.OpsConfigError as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return 2
    if args.command == "verify":
        try:
            remote, info = replica.fetch(target)
            local = replica.read_local(_journal_path())
            rel = replica.relation(local, remote)
        except (replica.ReplicaError, TargetError) as exc:
            print(json.dumps({"verify": "refused", "code": exc.code}))
            return 3
        print(json.dumps({"verify": "ok", "relation": rel, "local_seq": local.head.seq, **info}, sort_keys=True))
        return 0 if rel in ("equal", "local_ahead") else 3
    cache: dict = {}
    if args.once:
        code, status = cycle(cfg, target, cache)
        print(json.dumps({k: status[k] for k in ("code", "local_seq", "remote_seq", "lag_records")}, sort_keys=True))
        return code
    interval = args.interval or cfg.replicator.interval_seconds
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    last = None
    while not stopping:
        _, status = cycle(cfg, target, cache)
        if status["code"] != last:  # one line per change of outcome
            print(json.dumps({k: status[k] for k in ("code", "local_seq", "remote_seq", "lag_records")},
                             sort_keys=True), flush=True)
            last = status["code"]
        deadline = time.monotonic() + interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
