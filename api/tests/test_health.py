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


def test_m5a_discovery_routes_registered() -> None:
    paths = set(app.openapi()["paths"])
    assert {"/uploads/notice", "/uploads/intents", "/uploads/original", "/files/{file_id}/original"} <= paths
    assert "/search" in paths
    assert "/rag/ask" in paths
    assert {"/discovery/papers", "/discovery/topics", "/discovery/sources"} <= paths
