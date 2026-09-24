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


def test_m1a_exposes_no_auth_or_upload_routes() -> None:
    paths = {route.path for route in app.routes}
    assert not any(p.startswith(("/auth", "/uploads", "/login", "/sessions")) for p in paths)
