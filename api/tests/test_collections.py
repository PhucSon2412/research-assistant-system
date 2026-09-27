"""M3c owner ACL, scoped retrieval and fake-provider payload on PostgreSQL."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.embedding import EmbeddingResult
from app.ingest.worker import process_job
from app.main import app
from app.models import Collection
from tests.test_ingest_worker import upload
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_uploads import upload_api


def _create(browser, name="Reading") -> str:
    response = browser.post("/collections", {"name": name})
    assert response.status_code == 201, response.text
    return response.json()["collection"]["id"]


def _add(browser, collection, item):
    return browser.post(f"/collections/{collection}/items", {"item_id": str(item)})


def _delete(browser, collection):
    return browser.client.request("DELETE", f"/collections/{collection}",
                                  headers={"X-CSRF-Token": browser.csrf()})


def _indexed(storage, browser, body, settings):
    ids = upload(browser, body)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    assert process_job(uuid.UUID(ids["job_id"]), settings, storage) == "ready"
    return ids


def test_collection_owner_matrix_metadata_and_crud(api):
    api.bootstrap_admin()
    aid = api.create_user("a@example.org")
    bid = api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    own = api.add_item(owner=aid, title="A private")
    foreign = api.add_item(owner=bid, title="B secret")
    common = api.add_item(owner=None, title="Common metadata")
    collection = _create(a)
    missing = str(uuid.uuid4())
    assert _add(a, collection, own).status_code == 201
    assert _add(a, collection, common).status_code == 201
    assert _add(a, collection, foreign).status_code == 404
    assert _add(a, collection, missing).json() == _add(a, collection, foreign).json() == {"error": "not_found"}
    listing = a.get(f"/collections/{collection}/items").json()
    assert listing["total"] == 2
    assert {row["content_status"] for row in listing["items"]} == {"metadata_only"}
    assert "B secret" not in str(listing)
    for other in (b, admin):
        assert other.get("/collections").json()["collections"] == []
        for identifier in (collection, missing):
            assert other.get(f"/collections/{identifier}/items").json() == {"error": "not_found"}
            assert other.patch(f"/collections/{identifier}", {"name": "stolen"}).json() == {"error": "not_found"}
            assert other.post(f"/collections/{identifier}/items", {"item_id": str(common)}).json() == {"error": "not_found"}
            assert other.post("/search", {"query": "secret", "mode": "keyword", "collection_id": identifier}).json() == {"error": "not_found"}
    assert a.patch(f"/collections/{collection}", {"name": "Renamed"}).json()["collection"]["name"] == "Renamed"
    assert a.client.request("DELETE", f"/collections/{collection}/items/{own}", headers={"X-CSRF-Token": a.csrf()}).status_code == 204
    assert a.get(f"/collections/{collection}/items").json()["total"] == 1
    assert a.client.request("DELETE", f"/collections/{collection}", headers={"X-CSRF-Token": a.csrf()}).status_code == 204
    assert a.get(f"/collections/{collection}/items").status_code == 404


def test_delete_collection_removes_memberships_but_preserves_items(api):
    owner = api.create_user("owner@example.org")
    browser = api.user_browser("owner@example.org")
    own = api.add_item(owner=owner, title="Private stays")
    common = api.add_item(owner=None, title="Common stays")
    collection = _create(browser)
    assert _add(browser, collection, own).status_code == 201
    assert _add(browser, collection, common).status_code == 201

    assert _delete(browser, collection).status_code == 204
    assert api.scalar("SELECT count(*) FROM collections WHERE id=:c", c=uuid.UUID(collection)) == 0
    assert api.scalar("SELECT count(*) FROM collection_items WHERE collection_id=:c", c=uuid.UUID(collection)) == 0
    for item in (own, common):
        assert api.scalar("SELECT count(*) FROM library_items WHERE id=:i", i=item) == 1
        assert browser.get(f"/library/items/{item}").status_code == 200


def test_database_rejects_direct_collection_delete_while_members_exist(api):
    owner = api.create_user("owner@example.org")
    browser = api.user_browser("owner@example.org")
    item = api.add_item(owner=owner, title="Still linked")
    collection = _create(browser)
    assert _add(browser, collection, item).status_code == 201

    with pytest.raises(IntegrityError):
        with api.engine.begin() as conn:
            conn.execute(text("DELETE FROM collections WHERE id=:c"), {"c": uuid.UUID(collection)})
    assert api.scalar("SELECT count(*) FROM collections WHERE id=:c", c=uuid.UUID(collection)) == 1
    assert api.scalar("SELECT count(*) FROM collection_items WHERE collection_id=:c", c=uuid.UUID(collection)) == 1


def test_delete_collection_failure_rolls_back_memberships(api, monkeypatch):
    owner = api.create_user("owner@example.org")
    browser = api.user_browser("owner@example.org")
    item = api.add_item(owner=owner, title="Rollback item")
    collection = _create(browser)
    assert _add(browser, collection, item).status_code == 201

    original_delete = Session.delete
    def fail_collection_delete(self, instance):
        if isinstance(instance, Collection):
            raise RuntimeError("synthetic failure after membership delete")
        return original_delete(self, instance)
    monkeypatch.setattr(Session, "delete", fail_collection_delete)
    with pytest.raises(RuntimeError, match="synthetic failure after membership delete"):
        _delete(browser, collection)
    assert api.scalar("SELECT count(*) FROM collections WHERE id=:c", c=uuid.UUID(collection)) == 1
    assert api.scalar("SELECT count(*) FROM collection_items WHERE collection_id=:c", c=uuid.UUID(collection)) == 1
    assert api.scalar("SELECT count(*) FROM library_items WHERE id=:i", i=item) == 1


def test_scope_before_keyword_vector_topk_and_rerank_payload(upload_api, monkeypatch):
    api, storage = upload_api
    aid = api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    settings = settings_with_fake_key()
    settings.search.keyword_top_k = 1
    settings.search.vector_top_k = 1
    settings.rerank.pricing_verified = True
    fake_provider(monkeypatch)
    wanted = _indexed(storage, a, b"## Methods\nTargetword inside collection.\n", settings)
    outside = _indexed(storage, a, b"## Methods\nTargetword Targetword Targetword outside collection.\n", settings)
    secret = _indexed(storage, b, b"## Methods\nTargetword B secret.\n", settings)
    common = api.add_item(owner=None, title="Common metadata")
    collection = _create(a)
    for item in (wanted["item_id"], common):
        assert _add(a, collection, item).status_code == 201
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE embedding_calls SET vector='[0.1,0.1,0.1]' WHERE source_file_id=:f"),
                     {"f": uuid.UUID(outside["file_id"])})
        conn.execute(text("UPDATE embedding_calls SET vector='[-0.1,0.1,0.1]' WHERE source_file_id=:f"),
                     {"f": uuid.UUID(wanted["file_id"])})
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("app.search.embed", lambda q, cfg, input_type: EmbeddingResult([.1, .1, .1], cfg.embedding.model_id, 1))
    sent = []
    def rerank(query, docs, cfg):
        sent.extend(docs)
        return ([{"index": i, "relevance_score": 1.0} for i in range(len(docs))], 2, None)
    monkeypatch.setattr("app.search.rerank", rerank)
    # Both unscoped top-1 paths choose the stronger outside paper. A scoped
    # query must instead admit the weaker member into that same single slot.
    for mode in ("keyword", "hybrid"):
        unscoped = a.post("/search", {"query": "Targetword", "mode": mode, "rerank": False}).json()
        assert unscoped["hits"][0]["item_id"] == outside["item_id"]
    for mode in ("keyword", "hybrid"):
        response = a.post("/search", {"query": "Targetword", "mode": mode,
                                      "rerank": True, "collection_id": collection})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["count"] == 1 and data["hits"][0]["item_id"] == wanted["item_id"]
        assert data["rerank_status"] == "applied"
    assert sent and all("inside collection" in doc for doc in sent)
    assert "outside collection" not in str(sent) and "B secret" not in str(sent)
    # Remove membership immediately after the rerank reservation commits.
    # The fresh gate must stop the provider call even though earlier ranks existed.
    from app import search as search_module
    original_reserve = search_module.reserve
    def revoke_at_reservation(db, principal, cfg, task, amount):
        usage_id = original_reserve(db, principal, cfg, task, amount)
        if task == "rerank":
            with api.engine.begin() as conn:
                conn.execute(text("DELETE FROM collection_items WHERE collection_id=:c AND item_id=:i"),
                             {"c": uuid.UUID(collection), "i": uuid.UUID(wanted["item_id"])})
        return usage_id
    monkeypatch.setattr("app.search.reserve", revoke_at_reservation)
    sent_before = len(sent)
    changed = a.post("/search", {"query": "Targetword", "mode": "keyword",
                                  "rerank": True, "collection_id": collection})
    assert changed.status_code == 409 and changed.json() == {"error": "search_scope_changed"}
    assert len(sent) == sent_before
    monkeypatch.setattr("app.search.reserve", original_reserve)
    assert _add(a, collection, wanted["item_id"]).status_code == 201
    assert a.post("/search", {"query": "Targetword", "mode": "keyword", "item_id": wanted["item_id"],
                              "collection_id": collection}).status_code == 422
    assert a.get(f"/collections/{collection}/items").json()["total"] == 2
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                     {"i": uuid.UUID(wanted["item_id"])})
    assert a.post("/search", {"query": "Targetword", "mode": "keyword", "collection_id": collection}).json()["count"] == 0
    assert a.post("/search", {"query": "Targetword", "mode": "keyword", "rerank": True,
                              "collection_id": collection}).json()["count"] == 0
    assert len(sent) == sent_before
    assert a.get(f"/collections/{collection}/items").json()["total"] == 2
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE index_generations SET state='superseded' WHERE item_id=:i"),
                     {"i": uuid.UUID(wanted["item_id"])})
    assert a.post("/search", {"query": "Targetword", "mode": "keyword", "collection_id": collection}).json()["count"] == 0
    assert secret["item_id"] != wanted["item_id"]
