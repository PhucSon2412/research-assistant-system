"""Two-user M5b-1 citation-link demo on Compose PostgreSQL/MinIO; no external requests.

Run from the repository root with the Conda ras interpreter after
``docker compose up -d --wait db minio``. It creates a temporary database and
bucket, uploads the same synthetic Markdown for User A and User B, runs the
real worker extraction and a fixture OAI feed, prints the A/B/Admin matrix and
fixture match counts, then drops both. The app database is not touched.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from sqlalchemy import create_engine  # noqa: E402

from tests.conftest import _admin_url, _read_dotenv, _unquote, create_empty_database, db_env, drop_database, upgrade  # noqa: E402
from tests.helpers import DEV_ENV  # noqa: E402


def main() -> None:
    admin_engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", hide_parameters=True)
    url = create_empty_database(admin_engine)
    dotenv = {k: _unquote(v) for k, v in _read_dotenv(ROOT / ".env").items()}
    bucket = f"ras-demo-{uuid.uuid4().hex[:12]}"
    os.environ.pop("RAS_DATABASE_URL", None)
    os.environ.update(DEV_ENV | db_env(url) | {
        "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{dotenv.get('MINIO_API_HOST_PORT', '9000')}",
        "RAS_STORAGE__ACCESS_KEY": dotenv["MINIO_ROOT_USER"], "RAS_STORAGE__SECRET_KEY": dotenv["MINIO_ROOT_PASSWORD"],
        "RAS_STORAGE__BUCKET": bucket})
    storage = None
    try:
        upgrade(url)
        from app import discovery
        from app.config import get_settings
        from app.db import get_engine
        from app.ingest.worker import process_job
        from app.storage import get_storage
        from tests.helpers import Harness, FakeClock
        from tests.test_citations import LATE, MD_REFS, NOW, SEED, harvest, markdown, oai_page, tally
        from tests.test_ingest_worker import upload

        discovery._reserve_request = lambda *_: None  # fixture feed only; nothing is fetched
        storage = get_storage(get_settings())
        storage.ensure_bucket()
        api = Harness(engine=get_engine(), clock=FakeClock())
        api.bootstrap_admin()
        api.create_user("demo-a@example.org")
        api.create_user("demo-b@example.org")
        a, b, admin = (api.user_browser("demo-a@example.org"), api.user_browser("demo-b@example.org"),
                       api.admin_browser())
        harvest(api.engine, oai_page(SEED))
        body = markdown()
        ids = {}
        for name, browser in (("A", a), ("B", b)):  # identical bytes and SHA-256 in two private libraries
            ids[name] = upload(browser, body)
            assert process_job(uuid.UUID(ids[name]["job_id"]), storage=storage) == "awaiting_index"
        refs_a = a.get(f"/library/items/{ids['A']['item_id']}/references").json()["references"]
        before = tally(refs_a, MD_REFS)
        harvest(api.engine, oai_page([LATE]), NOW + timedelta(hours=1))
        refs_a = a.get(f"/library/items/{ids['A']['item_id']}/references").json()["references"]
        after = tally(refs_a, MD_REFS)
        target = str(api.scalar("SELECT id FROM discovery_papers WHERE remote_id='2609.00011'"))

        def label(item_id: str) -> str:
            return {ids["A"]["item_id"]: "A-item", ids["B"]["item_id"]: "B-item"}.get(item_id, "other")

        matrix = {}
        for name, browser in (("A", a), ("B", b), ("Admin", admin)):
            citing = browser.get(f"/discovery/papers/{target}/citing-items").json()
            matrix[name] = {
                "A_references_status": browser.get(f"/library/items/{ids['A']['item_id']}/references").status_code,
                "B_references_status": browser.get(f"/library/items/{ids['B']['item_id']}/references").status_code,
                "A_detail_status": browser.get(f"/library/items/{ids['A']['item_id']}").status_code,
                "citing_total": citing["total"], "citing_items": [label(i["id"]) for i in citing["items"]],
                "response_mentions_other_users_item": any(
                    other["item_id"] in json.dumps(citing) for key, other in ids.items() if key != name)}
        print(json.dumps({"fixture_references": len(refs_a), "before_late_discovery": before,
                          "after_late_discovery": after, "matrix": matrix}, indent=1))
    finally:
        if storage is not None:
            for key, _ in storage.orphan_keys(""):
                storage.delete(key)
            storage.client.delete_bucket(Bucket=bucket)
        from app.db import get_engine
        get_engine().dispose()
        drop_database(admin_engine, url)
        admin_engine.dispose()


if __name__ == "__main__":
    main()
