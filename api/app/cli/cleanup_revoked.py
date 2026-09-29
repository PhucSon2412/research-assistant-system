"""Purge revoked common copies now (the beat task ``ras.revocation.cleanup`` also runs it).

Works from unpurged ``deletion_tombstones``: removes collection memberships
and the common object (never a key another non-revoked row still names), and
records the outcome. Safe to run repeatedly; failures stay unpurged and are
retried. Prints counts only.
"""

from __future__ import annotations

from app.clock import utcnow
from app.config import get_settings
from app.db import get_engine
from app.publication import cleanup_revoked
from app.storage import get_storage


def main() -> None:
    settings = get_settings()
    counts = cleanup_revoked(get_engine(), get_storage(settings), utcnow(), settings.retention.cleanup_batch_size)
    print("Cleanup outcomes: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))


if __name__ == "__main__":
    main()
