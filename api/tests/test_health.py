from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_is_up_without_database() -> None:
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_readyz_requires_migrated_database(app_env, empty_db_url: str) -> None:
    app_env(empty_db_url)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_readyz_ok_after_migration(app_env, migrated_db_url: str) -> None:
    app_env(migrated_db_url)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200


def test_no_upload_or_file_routes_before_m1c() -> None:
    # M1a asserted "no auth routes"; M1b adds auth but still no upload/file path.
    paths = set(app.openapi()["paths"])
    assert "/auth/login" in paths  # sanity: included routers are listed
    assert not any(p.startswith(("/uploads", "/files")) for p in paths)
    assert not any("upload" in p or "file" in p for p in paths)
