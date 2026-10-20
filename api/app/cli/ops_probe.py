"""Read-only probe for ``ras_ops.py check`` (M8b), run inside the API container as its runtime LOGIN.

Prints one JSON line: the gate reason exactly as readiness evaluates it, and counts of
revoked common copies / deleted private items past ``purge_due_at`` and still not
purged. Codes and counts only; no IDs, names or paths. Never writes.
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import text

from app import recovery
from app.config import get_settings
from app.db import get_engine

OVERDUE = {
    "overdue_tombstones": "SELECT count(*) FROM deletion_tombstones WHERE purged_at IS NULL AND purge_due_at < now()",
    "overdue_private_deletions": ("SELECT count(*) FROM private_item_deletions"
                                  " WHERE purged_at IS NULL AND purge_due_at < now()"),
}


def main() -> int:
    settings, engine = get_settings(), get_engine()
    out: dict = {"status": "ok"}
    try:
        out["gate"] = recovery.evaluate(settings, engine).reason
    except Exception:  # noqa: BLE001
        out["gate"] = "probe_failed"
    for name, sql in OVERDUE.items():
        try:
            with engine.connect() as conn, conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                out[name] = int(conn.scalar(text(sql)))
        except Exception:  # noqa: BLE001 - no driver detail leaves
            out[name] = "unavailable"
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
