"""Backfill/refresh exact arXiv citation links for extracted references.

Safe to rerun or run beside workers: rows are keyed by reference, recomputed
from the current raw citation and discovery metadata, and bounded per run by
``RAS_CITATIONS__RECONCILE_*``. Prints counts only, never citation text.
"""

from __future__ import annotations

from app.citations import reconcile_pending
from app.config import get_settings
from app.db import get_engine


def main() -> None:
    print(reconcile_pending(get_engine(), get_settings().citations))


if __name__ == "__main__":
    main()
