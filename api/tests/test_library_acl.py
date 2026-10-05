"""ACL leak matrix for library metadata (docs/testing.md "Ma trận chống rò rỉ").

Actors: Admin, User A, User B. Items: two common, two A-private, one
B-private (plus Admin-private), each with a unique marker in its title.
"""

from __future__ import annotations

import uuid

import pytest

from tests.helpers import Harness
from tests.resource_fixtures import resource_api, resource_world, installation

A_EMAIL, B_EMAIL = "a@example.org", "b@example.org"


class World:
    def __init__(self, h: Harness) -> None:
        self.h = h
        self.admin_id = h.bootstrap_admin()
        self.a_id = h.create_user(A_EMAIL)
        self.b_id = h.create_user(B_EMAIL)
        self.common = [h.add_item(owner=None, title=f"COMMON-{i}", created_offset_s=i) for i in range(2)]
        self.a_private = [h.add_item(owner=self.a_id, title=f"A-PRIVATE-{i}", created_offset_s=10 + i) for i in range(2)]
        self.b_private = h.add_item(owner=self.b_id, title="B-PRIVATE-SECRET", created_offset_s=20)
        self.admin_private = h.add_item(owner=self.admin_id, title="ADMIN-PRIVATE", created_offset_s=30)
        self.admin = h.admin_browser()
        self.a = h.user_browser(A_EMAIL)
        self.b = h.user_browser(B_EMAIL)


@pytest.fixture
def w(resource_api: Harness) -> World:
    return World(resource_api)


def titles(browser, **params) -> list[str]:
    r = browser.get("/library/items", params=params)
    assert r.status_code == 200, r.text
    return [i["title"] for i in r.json()["items"]]


def test_user_a_list_shows_common_and_own_private_only(w: World) -> None:
    r = w.a.get("/library/items").json()
    assert sorted(i["title"] for i in r["items"]) == ["A-PRIVATE-0", "A-PRIVATE-1", "COMMON-0", "COMMON-1"]
    assert r["total"] == 4
    assert "B-PRIVATE" not in str(r) and "ADMIN-PRIVATE" not in str(r)
    assert all("owner_user_id" not in i for i in r["items"])
    assert {i["title"]: i["owned_by_me"] for i in r["items"]}["A-PRIVATE-0"] is True


def test_user_b_sees_only_own_private(w: World) -> None:
    assert sorted(titles(w.b)) == ["B-PRIVATE-SECRET", "COMMON-0", "COMMON-1"]


def test_count_is_filtered_before_counting(w: World) -> None:
    assert w.a.get("/library/items/count").json()["total"] == 4
    assert w.b.get("/library/items/count").json()["total"] == 3
    assert w.admin.get("/library/items/count").json()["total"] == 3  # common + Admin's own
    assert w.a.get("/library/items/count", params={"scope": "mine"}).json()["total"] == 2
    assert w.a.get("/library/items/count", params={"scope": "common"}).json()["total"] == 2
    # B's private item does not change A's numbers.
    extra = w.h.add_item(owner=w.b_id, title="B-PRIVATE-2")
    assert w.a.get("/library/items/count").json()["total"] == 4
    assert w.b.get("/library/items/count").json()["total"] == 4
    assert extra


def test_pagination_is_filtered_before_limit_and_offset(w: World) -> None:
    seen: list[str] = []
    for offset in range(0, 10):
        r = w.a.get("/library/items", params={"limit": 1, "offset": offset}).json()
        assert r["total"] == 4
        seen += [i["title"] for i in r["items"]]
    # Newest first; B's (newer) private item takes no slot and leaves no gap.
    assert seen == ["A-PRIVATE-1", "A-PRIVATE-0", "COMMON-1", "COMMON-0"]


def test_scope_parameter_narrows_but_never_widens(w: World) -> None:
    assert sorted(titles(w.a, scope="mine")) == ["A-PRIVATE-0", "A-PRIVATE-1"]
    assert sorted(titles(w.a, scope="common")) == ["COMMON-0", "COMMON-1"]
    # Tampered/unknown parameters cannot reach B's items.
    for params in (
        {"owner_user_id": str(w.b_id)},
        {"scope": "all", "visibility": "private", "owner": str(w.b_id)},
        {"user_id": str(w.b_id)},
    ):
        assert "B-PRIVATE-SECRET" not in titles(w.a, **params)
    assert w.a.get("/library/items", params={"scope": "private"}).status_code == 422
    assert w.a.get("/library/items", params={"scope": "everything"}).status_code == 422


def test_detail_of_other_users_private_item_looks_like_missing(w: World) -> None:
    forbidden = w.a.get(f"/library/items/{w.b_private}")
    missing = w.a.get(f"/library/items/{uuid.uuid4()}")
    malformed = w.a.get("/library/items/not-a-uuid")
    for r in (forbidden, missing, malformed):
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
    assert forbidden.headers.get("content-length") == missing.headers.get("content-length")
    assert "B-PRIVATE" not in forbidden.text


def test_owner_and_common_detail_are_readable(w: World) -> None:
    r = w.a.get(f"/library/items/{w.a_private[0]}")
    assert r.status_code == 200
    item = r.json()["item"]
    assert (item["title"], item["visibility"], item["can_edit"]) == ("A-PRIVATE-0", "private", True)
    assert item["versions"][0]["version_label"] == "v1"
    common = w.a.get(f"/library/items/{w.common[0]}").json()["item"]
    assert (common["visibility"], common["can_edit"]) == ("common", False)


def test_admin_does_not_read_other_users_private_items_by_default(w: World) -> None:
    assert sorted(titles(w.admin)) == ["ADMIN-PRIVATE", "COMMON-0", "COMMON-1"]
    for item in (w.b_private, w.a_private[0]):
        r = w.admin.get(f"/library/items/{item}")
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        r = w.admin.patch(f"/library/items/{item}", {"title": "admin was here"})
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
    assert w.h.scalar("SELECT title FROM library_items WHERE id = :i", i=w.b_private) == "B-PRIVATE-SECRET"


def test_users_cannot_read_admins_private_item(w: World) -> None:
    assert w.a.get(f"/library/items/{w.admin_private}").status_code == 404


def test_admin_manages_common_metadata(w: World) -> None:
    r = w.admin.patch(f"/library/items/{w.common[0]}", {"title": "COMMON-0 (edited)"})
    assert r.status_code == 200 and r.json()["item"]["title"] == "COMMON-0 (edited)"
    assert w.h.scalar(
        "SELECT count(*) FROM audit_events WHERE action = 'library.item.update' AND target_id = :t",
        t=str(w.common[0]),
    ) == 1


def test_user_edits_own_private_but_not_common_or_others(w: World) -> None:
    assert w.a.patch(f"/library/items/{w.a_private[0]}", {"title": "mine"}).status_code == 200
    r = w.a.patch(f"/library/items/{w.common[0]}", {"title": "vandal"})
    assert (r.status_code, r.json()) == (403, {"error": "forbidden"})
    r = w.a.patch(f"/library/items/{w.b_private}", {"title": "vandal"})
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})
    assert w.h.scalar("SELECT title FROM library_items WHERE id = :i", i=w.b_private) == "B-PRIVATE-SECRET"
    assert w.h.scalar("SELECT title FROM library_items WHERE id = :i", i=w.common[0]) == "COMMON-0"


def test_expired_or_missing_session_gets_no_metadata(w: World) -> None:
    w.h.clock.advance(minutes=30)
    for path in ("/library/items", "/library/items/count", f"/library/items/{w.a_private[0]}"):
        r = w.a.get(path)
        assert r.status_code == 401 and "A-PRIVATE" not in r.text
    guest = w.h.browser()
    assert guest.get(f"/library/items/{w.common[0]}").status_code == 401
