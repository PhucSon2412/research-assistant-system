"""Remove original objects that no SourceFile names, through the runtime (C2d-5e2).

MANUAL ONLY, in a quiescent environment: first stop every upload/approval and
any other writer that can PUT/copy an original or commit a SourceFile, and
make sure every old attempt, SDK/DB thread and remote transaction has
settled. This command cannot verify that; the grace period does not prove it.

Same boundary as the direct entry ``app.ingest.orphans.reconcile_originals``:
actual runtime login with both orphan routines, open recovery gate at every
checkpoint, durable per-key orphan fence before the object delete and storage
outside every DB transaction. Only the configured bucket and the
``originals/`` prefix; no bucket/key arguments. Without ``--apply`` it is a
preview (no fence, cursor, audit or delete). Prints codes/counts only. Exit 0
when the bounded pass ran to its end or bound, 1 when it halted (gate closed,
uncertain commit, release/storage/clock fault, wrong login), 2 when the
runtime configuration is missing. Run again to resume; prepared fences are
finished first.
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import get_engine
from app.ingest.orphans import reconcile_originals
from app.storage import get_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.reconcile_originals")
    parser.add_argument("--apply", action="store_true",
                        help="fence and delete unreferenced originals (default: preview only)")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        if settings.db is None:
            raise ValueError("connection parts required")
        engine, storage = get_engine(), get_storage(settings)
    except Exception:
        print("refused: reconcile_runtime_configuration_required", file=sys.stderr)
        return 2
    counts = reconcile_originals(engine, storage, settings, apply=args.apply)
    mode = "apply" if args.apply else "preview"
    print(f"Orphan reconcile ({mode}): " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))
    return 1 if any(k.startswith("halted:") for k in counts) else 0


if __name__ == "__main__":
    sys.exit(main())
