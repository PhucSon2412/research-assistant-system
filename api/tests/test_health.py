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
    assert "/discovery/papers/{paper_id}/citing-items" in paths
    assert "/library/items/{item_id}/references" in paths


def test_review_routes_registered_with_publish_and_revoke_only_on_common_copies() -> None:
    spec = app.openapi()["paths"]
    paths = set(spec)
    assert {"/review-requests", "/review-requests/notice", "/review-requests/{request_id}",
            "/review-requests/{request_id}/withdraw", "/admin/review-requests",
            "/admin/review-requests/{request_id}", "/admin/review-requests/{request_id}/reject",
            "/admin/review-requests/{request_id}/files/{file_id}/original",
            "/admin/review-requests/{request_id}/approve",
            "/admin/pending-publications", "/admin/pending-publications/{item_id}",
            "/admin/common-copies", "/admin/common-copies/{item_id}",
            "/admin/common-copies/{item_id}/publish", "/admin/common-copies/{item_id}/revoke"} <= paths
    # The pending view stays read-only; M6b-2a actions live on /admin/common-copies.
    assert set(spec["/admin/pending-publications"]) == set(spec["/admin/pending-publications/{item_id}"]) == {"get"}
    assert set(spec["/admin/common-copies/{item_id}/publish"]) == set(spec["/admin/common-copies/{item_id}/revoke"])         == {"post"}
    # Nothing opens or edits a common copy's rights or file (full text is a later PR).
    assert not [p for p in paths if any(word in p for word in ("promot", "access-rights"))
                or (p.startswith("/admin/pending-publications") and p.count("/") > 3)
                or (p.startswith("/admin/common-copies") and p.count("/") > 3
                    and not p.endswith(("/publish", "/revoke")))]
    assert [p for p in paths if "/publish" in p] == ["/admin/common-copies/{item_id}/publish"]
