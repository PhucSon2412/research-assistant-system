"""Backfill/refresh exact arXiv citation links for extracted references (C2d-5f1 runtime).

Same boundary as the Celery task ``ras.citations.reconcile`` and the direct
entry ``app.citations.reconcile_pending``: actual runtime login with the
citation grants, open recovery gate before every batch, and per batch a short
transaction (common items -> references -> citation lock -> links -> journal
exclusive -> exact rebinding -> fresh clock -> known commit). Links are
derived metadata ("lists in its bibliography"), never evidence; no network,
storage or provider call.

Safe to rerun or run beside workers: rows are keyed by reference, recomputed
from the current raw citation and discovery metadata, and bounded per run by
``RAS_CITATIONS__RECONCILE_*``. Prints codes/counts only, never citation text.
Exit 0 when the bounded pass ran to its end or bound, 1 when it halted (gate
closed, wrong login/grants, uncertain commit, release/clock fault), 2 when the
runtime configuration is missing. Run again to resume.
"""

from __future__ import annotations

import argparse
import sys

from app.citations import reconcile_pending
from app.config import get_settings
from app.db import get_engine


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m app.cli.reconcile_citations").parse_args(argv)
    try:
        settings = get_settings()
        if settings.db is None:
            raise ValueError("connection parts required")
        engine = get_engine()
    except Exception:
        print("refused: reconcile_runtime_configuration_required", file=sys.stderr)
        return 2
    counts = reconcile_pending(engine, settings.citations)
    print("Citation reconcile: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))
    return 1 if any(k.startswith("halted:") for k in counts) else 0


if __name__ == "__main__":
    sys.exit(main())
