"""Isolated empty-DB M5a demo against Compose PostgreSQL; no external requests.

Run with the Conda ras interpreter from the repository root. The temporary
database is dropped after the two runs; the existing Compose app DB is intact.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.config import DiscoveryPolicy  # noqa: E402
from app.discovery import run_sync  # noqa: E402
from tests.conftest import _admin_url, create_empty_database, drop_database, upgrade  # noqa: E402
from tests.test_discovery import PAGE_ONE, PAGE_TWO  # noqa: E402


def main() -> None:
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", hide_parameters=True)
    url = create_empty_database(admin)
    try:
        upgrade(url)
        engine = create_engine(url, hide_parameters=True)
        try:
            import app.discovery as discovery
            discovery._reserve_request = lambda *_: None  # fixture feed has no network request
            policy = DiscoveryPolicy(topics=["cs.AI"], max_pages_per_topic=2)
            def feed(params, _policy):
                return PAGE_TWO if params.get("resumptionToken") == "page-two" else PAGE_ONE
            now = datetime.now(UTC)
            first = run_sync(engine, policy, fetcher=feed, now=now)
            with engine.connect() as db:
                first_count = db.scalar(text("SELECT count(*) FROM discovery_papers"))
            second = run_sync(engine, policy, fetcher=feed, now=now + timedelta(days=1), force=True)
            with engine.connect() as db:
                second_count = db.scalar(text("SELECT count(*) FROM discovery_papers"))
                other_rows = db.scalar(text("SELECT (SELECT count(*) FROM source_files) + (SELECT count(*) FROM chunks) + (SELECT count(*) FROM index_entries)"))
            print({"first_run": first, "first_count": first_count,
                   "second_run": second, "second_count": second_count,
                   "new_on_second": second_count - first_count,
                   "file_chunk_index_rows": other_rows})
        finally:
            engine.dispose()
    finally:
        drop_database(admin, url)
        admin.dispose()


if __name__ == "__main__":
    main()
