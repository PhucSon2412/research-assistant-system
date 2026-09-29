"""Remove crash-left original objects with no SourceFile after a grace period.

Run when no old upload request or review approval (M6b-1 common copy) is still
in flight. Objects remain unreachable through the application while
unreferenced; this is operational cleanup, not part of the request success
path. An approval whose commit outcome was unknown leaves its copy here: if
the commit did land, a SourceFile references the key and it is kept.
"""

from __future__ import annotations

from app.clock import utcnow
from app.config import get_settings
from app.db import get_engine
from app.models import SourceFile
from app.storage import get_storage
from sqlalchemy import select
from sqlalchemy.orm import Session


def main() -> None:
    settings = get_settings()
    storage = get_storage(settings)
    cutoff = utcnow() - settings.files.orphan_grace
    deleted = 0
    with Session(get_engine()) as db:
        for key, modified in storage.orphan_keys():
            if modified >= cutoff:
                continue
            if db.scalar(select(SourceFile.id).where(SourceFile.storage_key == key)) is None:
                storage.delete(key)
                deleted += 1
    print(f"Removed {deleted} unreferenced originals")


if __name__ == "__main__":
    main()
