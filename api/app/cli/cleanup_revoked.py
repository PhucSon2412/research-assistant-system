"""Purge revoked common copies now through the runtime boundary (C2d-5e1).

Same boundary as the beat task ``ras.revocation.cleanup``
(``app.ingest.cleanup``): actual runtime login with the fence routine, open
recovery gate at every checkpoint, durable per-key deletion fence before the
object delete and S3 outside every DB transaction. Works from unpurged
``deletion_tombstones``; safe to run repeatedly, failures stay unpurged and
are retried. Prints codes/counts only. Exit 0 when the batch ran to its end,
1 when a checkpoint halted it (gate closed, uncertain commit, release fault,
wrong login), 2 when the runtime configuration is missing.

Restore recovery does NOT use this command: ``python -m app.cli.recovery
recover`` runs its own operator cleanup while traffic is stopped.
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.db import get_engine
from app.ingest.cleanup import cleanup_revoked
from app.storage import get_storage


def main() -> int:
    try:
        settings = get_settings()
        if settings.db is None:
            raise ValueError("connection parts required")
        engine, storage = get_engine(), get_storage(settings)
    except Exception:
        print("refused: cleanup_runtime_configuration_required", file=sys.stderr)
        return 2
    counts = cleanup_revoked(engine, storage, settings)
    print("Cleanup outcomes: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))
    return 1 if any(k.startswith("halted:") for k in counts) else 0


if __name__ == "__main__":
    sys.exit(main())
