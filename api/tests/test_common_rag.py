"""M6b-2b3b cited RAG over indexed common copies under their AccessRights (PostgreSQL + MinIO).

Same world as ``test_common_search``: A and B index private Markdown (B with the
*same bytes and checksum* as A's file that became the common copy ``ok``), plus
a ``pending`` copy, a ``listed`` copy without provider processing (still
``metadata_only``) and a copy indexed then revoked (index rows await cleanup).
Embedding, rerank and generation are fakes; ``httpx.HTTPTransport`` refuses
every outbound request, so nothing reaches OpenRouter.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from threading import Thread

import pytest
from sqlalchemy import text

from app import rag as rag_module
from app import search as search_module
from app.common_index import SEARCH_ONLY_NOTE
from app.ingest.worker import process_job
from app.rag import GenerationResult
from app.search import money
from tests.test_common_cleanup import wait_for_lock_wait
from tests.test_common_index import INDEXABLE
from tests.test_common_search import (MISSING, TAMPERS, build, chunk_ids, copy_of, extracted, fake_query_embedding,
                                      hit_items, ok_ids, out_of_band, search)
from tests.test_pdf_ingest import partial_pdf, upload_pdf
from tests.test_publication import revoke, revoke_in_own_session
from tests.original_fixtures import upload_api, base_upload_api  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401

QUESTION = "Sharedcanary result?"
FORBIDDEN = ("Pendingcanary", "Metacanary", "Revokedcanary", "Refcanary")
SECOND = b"## Results\nSharedcanary result is eleven units in the second study.\n"


@pytest.fixture
def w(upload_api, monkeypatch):
    world = build(upload_api, monkeypatch)
    world.version = str(world.api.scalar("SELECT active_version_id FROM library_items WHERE id=:i",
                                         i=uuid.UUID(world.ok)))
    world.chunks = {"a": set(chunk_ids(world.api, world.a_main["item_id"]) + chunk_ids(world.api, world.a_secret["item_id"])
                             + world.ok_chunks),
                    "b": set(chunk_ids(world.api, world.b_same["item_id"]) + chunk_ids(world.api, world.b_secret["item_id"])
                             + world.ok_chunks),
                    "admin": set(world.ok_chunks)}
    return world


def ask(browser, question=QUESTION, mode="keyword", **scope):
    return browser.post("/rag/ask", {"question": question, "mode": mode} | (scope or {"scope": "all"}))


def cite_all(evidence: list[dict]) -> list[dict]:
    return [{"text": "The result is reported.", "citations": [
        {"chunk_id": row["chunk_id"], "quote": row["text"][:24]} for row in evidence]}]


def answering(monkeypatch, *, action=None, claims=cite_all) -> list[list[dict]]:
    """Fake generation: records each payload's evidence; ``action`` runs while the provider "holds" it."""
    sent: list[list[dict]] = []

    def generation(messages, settings):
        evidence = json.loads(messages[1]["content"])["evidence"]
        sent.append(evidence)
        if action:
            action()
        return GenerationResult(json.dumps({"claims": claims(evidence)}), 100, 20, settings.generation.model_id,
                                "SyntheticProvider")
    monkeypatch.setattr("app.rag.generate", generation)
    return sent


def citations(data: dict) -> list[dict]:
    return [cite for claim in data["claims"] for cite in claim["citations"]]


def evidence_items(payload: list[dict]) -> set[str]:
    return {row["item_id"] for row in payload}


def generation_usage(w) -> list[tuple]:
    with w.api.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text("""SELECT user_id, status, cost_status FROM search_usage
            WHERE task_type='generation' ORDER BY created_at"""))]


def user_id(w, email: str) -> uuid.UUID:
    return w.api.scalar("SELECT id FROM users WHERE email=:e", e=email)


def assert_payload_clean(w, who: str, payload: list[dict]) -> None:
    """Only the caller's audience, never another user's private chunk (same text or not) or a closed copy."""
    assert {row["chunk_id"] for row in payload} <= w.chunks[who], who
    blob = json.dumps(payload)
    other = {"a": "Bsecretcanary", "b": "Asecretcanary", "admin": "secretcanary"}[who]
    assert other not in blob and "References" not in blob, who
    assert not any(canary in blob for canary in FORBIDDEN), who


def assert_citations_open(w, browser, data: dict, who: str) -> None:
    for cite in citations(data):
        opened = browser.get(cite["url"].replace("/sources/", "/search/chunks/"))
        assert opened.status_code == 200, (who, opened.text)
        body = opened.json()
        assert cite["quote"] in body["text"], who
        for key in ("item_id", "version_id", "source_file_id", "locator_kind", "start_line", "end_line",
                    "start_page", "end_page", "start_offset", "end_offset", "visibility"):
            assert cite[key] == body[key], (who, key)
        assert cite["original_url"] == body["original_url"], who


# ----------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_every_account_asks_the_permitted_copy_and_never_anothers_private(w, monkeypatch, mode):
    fake_query_embedding(monkeypatch, [])
    sent = answering(monkeypatch)
    recorded = []
    from app import search_runtime
    real_search = search_runtime.search

    def recording_search(*args, **kwargs):
        found = real_search(*args, **kwargs)
        recorded.append(found[0])
        return found
    monkeypatch.setattr("app.rag_runtime.retrieval.search", recording_search)
    others = {"a": w.b_same["item_id"], "b": w.a_main["item_id"], "admin": w.a_main["item_id"]}
    for who, browser in w.browsers.items():
        # Item scope: the common copy itself, for every account alike.
        response = ask(browser, mode=mode, item_id=w.ok)
        data = response.json()
        assert response.status_code == 200 and data["status"] == "answered", (who, response.text)
        assert (data["scope"], data["coverage"], data["evidence_withheld"]) == (
            {"kind": "item", "item_id": w.ok}, "complete", 0), who
        cites = citations(data)
        assert cites and {c["item_id"] for c in cites} == {w.ok}, who
        assert {(c["version_id"], c["source_file_id"], c["visibility"]) for c in cites} == {
            (w.version, w.ok_file, "common")}, who
        # Download is its own gate; these rights permit it, so the citation carries this file's route only.
        assert {c["original_url"] for c in cites} == {f"/files/{w.ok_file}/original"}, who
        assert all(c["url"] == f"/sources/{c['chunk_id']}" for c in cites), who
        assert_citations_open(w, browser, data, who)
        assert evidence_items(sent[-1]) == {w.ok}, who
        assert_payload_clean(w, who, sent[-1])
        # All scope: own private items and the common copy; filtered before top-k, rechecked before sending.
        data = ask(browser, mode=mode).json()
        assert data["status"] == "answered" and data["scope"] == {"kind": "all"}, (who, data)
        assert evidence_items(sent[-1]) == w.allowed[who], who
        assert_payload_clean(w, who, sent[-1])
        assert {c["item_id"] for c in citations(data)} == w.allowed[who], who
        assert_citations_open(w, browser, data, who)
        # Neither another user's private item nor a closed copy is a scope: 404 like a missing ID.
        for item in (w.pending, w.meta, w.revoked, others[who], str(uuid.uuid4())):
            assert ask(browser, mode=mode, item_id=item).json() == MISSING, (who, item)
        # A reference entry is never evidence: keyword finds nothing to send (hybrid sends body chunks only).
        before = len(sent)
        refs = ask(browser, "Refcanary Linked Study", mode=mode, item_id=w.ok).json()
        assert "Refcanary" not in json.dumps(refs), who
        if mode == "keyword":
            assert refs["status"] == "insufficient_evidence" and len(sent) == before, who
        assert all("Refcanary" not in json.dumps(payload) for payload in sent), who
    # Collection membership narrows the audience: A's collection holds ok, the metadata-only and revoked copies
    # and one private item; only ok and the private item are evidence.
    data = ask(w.a, mode=mode, collection_id=w.collection).json()
    assert data["status"] == "answered" and evidence_items(sent[-1]) == {w.ok, w.a_secret["item_id"]}
    assert_payload_clean(w, "a", sent[-1])
    for browser in (w.b, w.admin):
        assert ask(browser, mode=mode, collection_id=w.collection).json() == MISSING
    # Search inside RAG saw only each caller's audience.
    for found in recorded:
        assert not any(hit["item_id"] in (w.pending, w.meta, w.revoked) for hit in found["hits"])
    # The UI flags follow the same gates.
    for who, browser in w.browsers.items():
        flags = {i["id"]: (i["fulltext_search_available"], i["rag_available"])
                 for i in browser.get("/library/items?scope=common&limit=100").json()["items"]}
        assert flags[w.ok] == (True, True) and flags[w.meta] == (False, False), who
    members = {i["id"]: i["rag_available"] for i in w.a.get(f"/collections/{w.collection}/items?limit=100").json()["items"]}
    assert members == {w.ok: True, w.meta: False, w.a_secret["item_id"]: True}


def test_generation_on_common_evidence_is_charged_to_the_asking_user(w, monkeypatch):
    answering(monkeypatch)
    with w.api.engine.connect() as conn:
        embedding_before = [tuple(r) for r in conn.execute(text(
            "SELECT count(*), coalesce(sum(coalesce(cost_usd, reserved_usd)), 0) FROM embedding_calls"))]
        budgets_before = [tuple(r) for r in conn.execute(text(
            "SELECT day, committed_usd, reserved_usd FROM embedding_budget_days ORDER BY day"))]
        user_budgets_before = [tuple(r) for r in conn.execute(text(
            "SELECT day, user_id, committed_usd, reserved_usd FROM embedding_user_budget_days ORDER BY 1, 2"))]
    for browser in (w.a, w.admin):
        assert ask(browser, item_id=w.ok).json()["status"] == "answered"
    a, admin = user_id(w, "a@example.org"), w.api.scalar("SELECT id FROM users WHERE role='admin'")
    # One generation row per answered request, charged to whoever asked (never to the copy or the Admin who
    # indexed it); settled once.
    assert generation_usage(w) == [(a, "completed", "usage_price_estimate"),
                                   (admin, "completed", "usage_price_estimate")]
    # Document-embedding accounting of the common copy is untouched by RAG.
    with w.api.engine.connect() as conn:
        assert [tuple(r) for r in conn.execute(text(
            "SELECT count(*), coalesce(sum(coalesce(cost_usd, reserved_usd)), 0) FROM embedding_calls"))] == embedding_before
        assert [tuple(r) for r in conn.execute(text(
            "SELECT day, committed_usd, reserved_usd FROM embedding_budget_days ORDER BY day"))] == budgets_before
        assert [tuple(r) for r in conn.execute(text(
            "SELECT day, user_id, committed_usd, reserved_usd FROM embedding_user_budget_days ORDER BY 1, 2"))] == \
            user_budgets_before
    # The per-user generation quota is the asker's: A (already spent) is refused, B (nothing spent) still asks the
    # same copy.
    policy = w.settings.generation
    w.settings.budget.user_generation_daily_usd = float(
        money(policy.max_input_tokens_estimate, policy.input_usd_per_million_ceiling)
        + money(policy.max_output_tokens, policy.max_output_usd_per_million_tokens))
    refused = ask(w.a, item_id=w.ok)
    assert (refused.status_code, refused.json()) == (429, {"error": "rag_user_budget_exhausted"})
    assert ask(w.b, item_id=w.ok).json()["status"] == "answered"
    assert [row[0] for row in generation_usage(w)] == [a, admin, user_id(w, "b@example.org")]


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_searchable_common_text_without_provider_rights_is_withheld(w, monkeypatch, mode):
    """Full text without external processing (only reachable out of band: indexing needs both): search serves
    the copy, RAG never sends it. Conditions close the copy entirely."""
    fake_query_embedding(monkeypatch, [])
    sent = answering(monkeypatch)
    params = ok_ids(w)
    only_common = w.a.post("/collections", {"name": "Common only"}).json()["collection"]["id"]
    assert w.a.post(f"/collections/{only_common}/items", {"item_id": w.ok}).status_code == 201
    out_of_band(w, "UPDATE access_rights SET permits_external_processing=false WHERE id=:r", params)
    for who, browser in w.browsers.items():
        assert w.ok in hit_items(search(browser, mode=mode)), who
        detail = browser.get(f"/library/items/{w.ok}").json()["item"]
        assert (detail["fulltext_search_available"], detail["rag_available"]) == (True, False), who
    index = w.admin.get(f"/admin/common-copies/{w.ok}").json()["item"]["index"]
    assert (index["search_open"], index["rag_open"], index["note"]) == (True, False, SEARCH_ONLY_NOTE)
    for browser, scope in ((w.admin, {"item_id": w.ok}), (w.admin, {}), (w.a, {"collection_id": only_common})):
        data = ask(browser, mode=mode, **scope).json()
        assert (data["status"], data.get("reason"), data["claims"]) == (
            "insufficient_evidence", "evidence_not_permitted_for_provider", []), scope
        assert data["evidence_withheld"] > 0 and data["cost"] is None, scope
    assert sent == []  # generation was never called
    # A still gets an answer from private text only; the common hits are counted as withheld.
    data = ask(w.a, mode=mode).json()
    assert data["status"] == "answered" and data["evidence_withheld"] > 0
    assert evidence_items(sent[-1]) == {w.a_main["item_id"], w.a_secret["item_id"]}
    assert not {row["chunk_id"] for row in sent[-1]} & set(w.ok_chunks)
    out_of_band(w, "UPDATE access_rights SET permits_external_processing=true WHERE id=:r", params)
    sent.clear()
    # scope_conditions (text, not machine-enforced) close search and RAG alike.
    out_of_band(w, "UPDATE access_rights SET scope_conditions='Chỉ trích dẫn ngắn' WHERE id=:r", params)
    for who, browser in w.browsers.items():
        assert ask(browser, mode=mode, item_id=w.ok).json() == MISSING, who
    data = ask(w.admin, mode=mode).json()
    assert (data["status"], data["evidence_withheld"], "reason" in data) == ("insufficient_evidence", 0, False)
    assert sent == []
    out_of_band(w, "UPDATE access_rights SET scope_conditions=NULL WHERE id=:r", params)
    assert ask(w.admin, mode=mode).json()["status"] == "answered"


# ----------------------------------------------------------------------------- comparisons


def compare_both(evidence: list[dict]) -> list[dict]:
    first = {}
    for row in evidence:
        first.setdefault(row["item_id"], row)
    return [{"text": "One paper reports nine units whereas the other reports eleven units.", "comparison": True,
             "citations": [{"chunk_id": row["chunk_id"], "quote": row["text"][:24]} for row in first.values()]}]


def cite_one_paper(evidence: list[dict]) -> list[dict]:
    return [{"text": "One paper reports more units than the other.", "comparison": True,
             "citations": [{"chunk_id": evidence[0]["chunk_id"], "quote": evidence[0]["text"][:24]}]}]


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_comparison_cites_exactly_the_two_resolved_papers(w, monkeypatch, mode):
    fake_query_embedding(monkeypatch, [])
    from app import search_runtime
    real_search = search_runtime.search
    # Keep the real gate; a shared fixture term lets title resolution, not keyword matching, pick the pair.
    monkeypatch.setattr("app.rag_runtime.retrieval.search", lambda db, principal, cfg, query, m, *args, **kwargs:
                        real_search(db, principal, cfg, "Sharedcanary result", m, *args, **kwargs))
    second = copy_of(w, extracted(w, w.a, SECOND), SECOND, index=True)
    for item, title, browser in ((w.ok, "Aurora Routing", w.admin), (second, "Birch Adapters", w.admin),
                                 (w.b_secret["item_id"], "Cedar Kernels", w.b)):
        assert browser.patch(f"/library/items/{item}", {"title": title}).status_code == 200
    sent = answering(monkeypatch, claims=compare_both)
    # Two common copies, for every account; and from a collection.
    for who, browser in w.browsers.items():
        data = ask(browser, "Compare Aurora Routing and Birch Adapters", mode=mode).json()
        assert data["status"] == "answered", (who, data)
        assert evidence_items(sent[-1]) == {w.ok, second}, who
        [claim] = data["claims"]
        assert claim["comparison"] and {c["item_id"] for c in claim["citations"]} == {w.ok, second}, who
        assert {c["visibility"] for c in claim["citations"]} == {"common"}, who
        assert_citations_open(w, browser, data, who)
    pair = w.a.post("/collections", {"name": "Pair"}).json()["collection"]["id"]
    for item in (w.ok, second):
        assert w.a.post(f"/collections/{pair}/items", {"item_id": item}).status_code == 201
    assert ask(w.a, "Compare Aurora Routing and Birch Adapters", mode=mode,
               collection_id=pair).json()["status"] == "answered"
    # A common copy and B's own private paper: B only. For A and the Admin, B's title resolves to nothing, so
    # the comparison is refused before generation and nothing of B is sent.
    data = ask(w.b, "Compare Aurora Routing and Cedar Kernels", mode=mode).json()
    assert data["status"] == "answered" and evidence_items(sent[-1]) == {w.ok, w.b_secret["item_id"]}
    assert {c["visibility"] for c in citations(data)} == {"common", "private"}
    assert_payload_clean(w, "b", sent[-1])
    count = len(sent)
    for browser in (w.a, w.admin):
        data = ask(browser, "Compare Aurora Routing and Cedar Kernels", mode=mode).json()
        assert (data["status"], data["reason"]) == ("insufficient_evidence", "comparison_requires_multiple_papers")
        assert "Cedar" not in json.dumps(data)
    assert len(sent) == count
    # A claim citing one paper does not answer a comparison (checked after generation).
    answering(monkeypatch, claims=cite_one_paper)
    data = ask(w.b, "Compare Aurora Routing and Birch Adapters", mode=mode).json()
    assert (data["status"], data["reason"], data["claims"]) == (
        "insufficient_evidence", "comparison_requires_multiple_papers", [])
    # Only one side still valid: the second copy may no longer reach a provider, so it is withheld and the
    # comparison is refused before any generation.
    sent = answering(monkeypatch, claims=compare_both)
    rights = w.api.scalar("SELECT id FROM access_rights WHERE item_id=:i", i=uuid.UUID(second))
    out_of_band(w, "UPDATE access_rights SET permits_external_processing=false WHERE id=:r", {"r": rights})
    data = ask(w.b, "Compare Aurora Routing and Birch Adapters", mode=mode).json()
    assert (data["status"], data["reason"]) == ("insufficient_evidence", "comparison_requires_multiple_papers")
    assert data["evidence_withheld"] > 0 and sent == []


# ----------------------------------------------------------------------------- revocation timing


def assert_gone_for_new_requests(w, monkeypatch) -> None:
    """From t_rev: no new RAG request retrieves, sends or cites the copy, although its index rows remain."""
    assert w.api.scalar("""SELECT count(*) FROM index_entries e JOIN index_generations g ON g.id=e.generation_id
        WHERE g.item_id=:i""", i=uuid.UUID(w.ok)) > 0
    sent = answering(monkeypatch)
    for who, browser in w.browsers.items():
        assert ask(browser, item_id=w.ok).json() == MISSING, who
        data = ask(browser).json()
        assert w.ok not in json.dumps(data), who
        assert data["status"] == ("insufficient_evidence" if who == "admin" else "answered"), who
    assert ask(w.a, collection_id=w.collection).json()["status"] == "answered"
    assert sent and all(w.ok not in evidence_items(payload) for payload in sent)
    for chunk in w.ok_chunks:
        assert w.b.get(f"/search/chunks/{chunk}").json() == MISSING


def test_revoke_before_generation_sends_nothing(w, monkeypatch):
    sent = answering(monkeypatch)
    from app import rag_runtime
    original = rag_runtime.checkpoint

    def revoke_after_reservation(engine, phase, operation):
        usage = original(engine, phase, operation)
        if phase != "reservation": return usage
        revoke_in_own_session(w.api, w.ok)
        return usage
    monkeypatch.setattr("app.rag_runtime.checkpoint", revoke_after_reservation)
    response = ask(w.b)
    assert (response.status_code, response.json()) == (409, {"error": "rag_scope_changed"})
    assert sent == [] and [row[1:] for row in generation_usage(w)] == [("reserved", "unknown")]
    monkeypatch.setattr("app.rag_runtime.checkpoint", original)
    assert_gone_for_new_requests(w, monkeypatch)


def test_revoke_during_generation_withholds_the_answer(w, monkeypatch):
    sent = answering(monkeypatch, action=lambda: revoke_in_own_session(w.api, w.ok))
    response = ask(w.b)
    assert (response.status_code, response.json()) == (409, {"error": "rag_scope_changed"})
    # What reached the provider was checked before sending (before t_rev) and cannot be recalled: a stated limit.
    [payload] = sent
    assert w.ok in evidence_items(payload)
    assert_payload_clean(w, "b", payload)
    # Settled once: the call happened, its usage is charged, nothing is reserved twice.
    assert [row[1:] for row in generation_usage(w)] == [("completed", "usage_price_estimate")]
    assert_gone_for_new_requests(w, monkeypatch)


def test_revoke_committed_before_the_response_lock_withholds_the_answer(w, monkeypatch):
    answering(monkeypatch)
    original = rag_module._validate_answer

    def revoke_then_validate(*args):
        revoke_in_own_session(w.api, w.ok)
        return original(*args)
    monkeypatch.setattr("app.rag._validate_answer", revoke_then_validate)
    response = ask(w.admin, item_id=w.ok)
    assert (response.status_code, response.json()) == (409, {"error": "rag_scope_changed"})
    assert [row[1:] for row in generation_usage(w)] == [("completed", "usage_price_estimate")]
    monkeypatch.setattr("app.rag._validate_answer", original)
    assert_gone_for_new_requests(w, monkeypatch)


def test_revoke_waiting_on_the_response_lock_lets_that_answer_finish(w, monkeypatch):
    answering(monkeypatch)
    original = rag_module.original_urls
    done = {}

    def urls_then_revoke(db, principal, file_ids):
        # Called under the response share locks: the revoke must wait for this response.
        thread = Thread(target=lambda: done.setdefault("status", revoke(w.api.admin_browser(), w.ok).status_code))
        thread.start()
        wait_for_lock_wait(w.api)
        done["thread"] = thread
        return original(db, principal, file_ids)
    monkeypatch.setattr("app.rag.original_urls", urls_then_revoke)
    data = ask(w.admin, item_id=w.ok).json()
    # The answer passed its last check before t_rev and is sent whole (a stated limit).
    assert data["status"] == "answered" and {c["item_id"] for c in citations(data)} == {w.ok}
    done["thread"].join(10)
    assert done["status"] == 200
    monkeypatch.setattr("app.rag.original_urls", original)
    assert_gone_for_new_requests(w, monkeypatch)


# ----------------------------------------------------------------------------- changes between the checks


def _membership_removed(w, params):
    with w.api.engine.begin() as conn:
        conn.execute(text("DELETE FROM collection_items WHERE collection_id=:c AND item_id=:i"),
                     {"c": uuid.UUID(w.collection), "i": params["i"]})


def _membership_restored(w, params):
    assert w.a.post(f"/collections/{w.collection}/items", {"item_id": w.ok}).status_code == 201


CHANGES = {
    "epoch": TAMPERS["item_epoch_ahead"],
    "version": TAMPERS["item_other_active_version"],
    "file": TAMPERS["rights_other_checksum"],
    "generation": TAMPERS["generation_not_active"],
    "provider_ok": ("UPDATE access_rights SET permits_external_processing=false WHERE id=:r",
                    "UPDATE access_rights SET permits_external_processing=true WHERE id=:r"),
    "membership": (_membership_removed, _membership_restored),
}


def _apply(w, sql_or_fn, params):
    if callable(sql_or_fn):
        sql_or_fn(w, params)
    else:
        out_of_band(w, sql_or_fn, params)


@pytest.mark.parametrize("stage", ["before_generation", "during_generation"])
def test_any_change_between_retrieval_and_response_withholds_the_answer(w, monkeypatch, stage):
    """Epoch, active version, rights file, generation, provider permission or membership changed after the
    pack was chosen: 409 before the provider (nothing sent) or before the response (sent before the change)."""
    params = ok_ids(w) | {"other": uuid.UUID(w.a_main["version_id"])}
    from app import rag_runtime
    original_reserve = rag_runtime.checkpoint
    for label, (change, undo) in CHANGES.items():
        before = len(generation_usage(w))
        if stage == "before_generation":
            sent = answering(monkeypatch)

            def change_after_reservation(engine, phase, operation, change=change):
                usage = original_reserve(engine, phase, operation)
                if phase != "reservation": return usage
                _apply(w, change, params)
                return usage
            monkeypatch.setattr("app.rag_runtime.checkpoint", change_after_reservation)
        else:
            sent = answering(monkeypatch, action=lambda change=change: _apply(w, change, params))
        response = ask(w.a, collection_id=w.collection)
        assert (response.status_code, response.json()) == (409, {"error": "rag_scope_changed"}), label
        usage = generation_usage(w)[before:]
        if stage == "before_generation":
            assert sent == [] and [row[1] for row in usage] == ["reserved"], label
            # Each matrix row is an independent attempt. Pre-send denial keeps
            # its ceiling and live slot until expiry; do not loosen concurrency
            # or pretend a definitely uncharged rejection to reset the fixture.
            from app import search_accounting
            w.api.clock.now += timedelta(seconds=w.settings.generation.reservation_lease_seconds + 1)
            search_accounting.reap(w.api.engine, w.settings, clock=lambda: w.api.clock.now)
            assert w.api.scalar("SELECT count(*) FILTER (WHERE status='reserved')=0 AND "
                "count(*) FILTER (WHERE status='uncertain' AND cost_usd IS NULL AND reserved_usd>0)>0 "
                "FROM search_usage WHERE user_id=:id AND task_type='generation'", id=usage[0][0]), label
        else:
            [payload] = sent
            assert w.ok in evidence_items(payload) and [row[1] for row in usage] == ["completed"], label
        monkeypatch.setattr("app.rag_runtime.checkpoint", original_reserve)
        _apply(w, undo, params)
        sent = answering(monkeypatch)
        restored = ask(w.a, collection_id=w.collection)
        assert restored.status_code == 200 and restored.json()["status"] == "answered", (label, restored.text)
        assert w.ok in evidence_items(sent[-1]), label


def test_a_half_opt_in_fails_closed_and_sends_nothing(w, monkeypatch):
    """Control for a partial opt-in: a recheck that forgets ``include_common`` loses the common rows and must
    surface as 409 (never an answer with unchecked text); a retrieval that forgets it never finds common text."""
    sent = answering(monkeypatch)
    real_hydrate = search_module.hydrate
    monkeypatch.setattr(search_module, "hydrate", lambda *args, **kwargs: real_hydrate(*args, **(kwargs | {
        "include_common": False})))
    for who, browser in w.browsers.items():
        response = ask(browser, item_id=w.ok)
        assert (response.status_code, response.json()) == (409, {"error": "rag_scope_changed"}), who
    assert sent == [] and generation_usage(w) == []
    monkeypatch.setattr(search_module, "hydrate", real_hydrate)
    real_scope = search_module._scope
    monkeypatch.setattr(search_module, "_scope", lambda *args, **kwargs: real_scope(*args, **(kwargs | {
        "include_common": False})))
    assert ask(w.admin, item_id=w.ok).json() == MISSING
    monkeypatch.setattr(search_module, "_scope", real_scope)
    real_rank = search_module._rank
    monkeypatch.setattr(search_module, '_rank', lambda *args, **kwargs: real_rank(*args, **(kwargs | {
        'include_common': False})))
    assert ask(w.admin).json()['status'] == 'insufficient_evidence' and sent == []
    monkeypatch.setattr(search_module, '_rank', real_rank)
    assert ask(w.admin, item_id=w.ok).json()["status"] == "answered"


# ----------------------------------------------------------------------------- coverage and download gate


def test_partial_pdf_common_copy_reports_the_coverage_of_its_permitted_version(w, monkeypatch):
    body = partial_pdf()
    ids = upload_pdf(w.a, body)
    assert process_job(uuid.UUID(ids["job_id"]), storage=w.storage) == "awaiting_index"
    copy = copy_of(w, ids, body, index=True)
    answering(monkeypatch)
    for who, browser in w.browsers.items():
        data = ask(browser, "measured observation", item_id=copy).json()
        assert (data["status"], data["coverage"]) == ("answered", "partial"), who
        cites = citations(data)
        assert cites and all(c["locator_kind"] == "pdf" and c["start_page"] is not None for c in cites), who
        assert_citations_open(w, browser, data, who)
        # No evidence: the coverage still describes the copy's active, permitted version.
        missing = ask(browser, "Absentword", item_id=copy).json()
        assert (missing["status"], missing["coverage"]) == ("insufficient_evidence", "partial"), who
        assert browser.get(f"/library/items/{copy}").json()["item"]["versions"][0]["coverage"] == "partial", who


def test_citation_links_the_original_only_through_the_download_gate(upload_api, monkeypatch):
    w = build(upload_api, monkeypatch, ok_scope=INDEXABLE | {"original_download": False})
    answering(monkeypatch)
    for who, browser in w.browsers.items():
        data = ask(browser, item_id=w.ok).json()
        assert data["status"] == "answered", who
        assert {c["original_url"] for c in citations(data)} == {None}, who
        assert browser.get(f"/search/chunks/{citations(data)[0]['chunk_id']}").json()["original_url"] is None, who
        assert browser.get(f"/files/{w.ok_file}/original").json() == MISSING, who
    # A's own private citations still link A's own files.
    data = ask(w.a).json()
    own = {c["original_url"] for c in citations(data) if c["item_id"] == w.a_main["item_id"]}
    assert own == {f"/files/{w.a_main['file_id']}/original"}
    assert {c["original_url"] for c in citations(data) if c["item_id"] == w.ok} == {None}


def test_same_bytes_in_another_private_library_are_never_evidence(w, monkeypatch):
    """B's private file has the same bytes and checksum as A's and as the common copy; each is its own item,
    and a caller's payload holds only the chunk IDs of their own audience."""
    sent = answering(monkeypatch)
    same = [set(chunk_ids(w.api, item)) for item in (w.a_main["item_id"], w.b_same["item_id"], w.ok)]
    assert all(same) and not (same[0] & same[1] or same[0] & same[2] or same[1] & same[2])
    for who, browser in w.browsers.items():
        for scope in ({}, {"item_id": w.ok}):
            assert ask(browser, **scope).json()["status"] == "answered", (who, scope)
            assert_payload_clean(w, who, sent[-1])
