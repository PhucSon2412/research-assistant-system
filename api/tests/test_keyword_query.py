"""M4d-1 bounded keyword query on PostgreSQL: tokens, hostile input, ACL gates and ranking."""
import json
import random
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import SearchPolicy, get_settings
from app.ingest.embedding import EmbeddingResult
from app.keyword_query import build, min_match, terms_from_tokens
from app.main import app
from app.rag import GenerationResult
from app.search import BASE, KEYWORD
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_search import indexed
from tests.test_uploads import upload_api  # noqa: F401 - fixture
from tests.test_c2c1_bootstrap import installation  # noqa: F401 - fixture dependency

HOSTILE = [
    "'); DROP TABLE library_items; --",
    "a:* & !b | <-> (c",
    "\\' \" ` $$ $1 :q :any :terms",
    "' OR 1=1 --",
    "()&|!<->:*'\\",
    "𝔘𝔫𝔦𝔠𝔬𝔡𝔢 😀 تست 日本語 Tiếng Việt",
    "x" * 900,
    "https://example.org/a?b=c&d=e user@example.org /usr/local/bin C:\\temp\\x",
]


def _tokens(*words):
    return [(alias, [word] if word is not None else None) for alias, word in words]


def test_terms_cap_dedupe_and_threshold_rules():
    policy = SearchPolicy()
    many = _tokens(*[("asciiword", w) for w in
                     "one two three four five six seven eight nine ten two one".split()])
    query = terms_from_tokens(many, policy)
    # The cap keeps the first 8 distinct terms in input order; repeats do not count.
    assert [t[0] for t in query.terms] == "one two three four five six seven eight".split()
    assert query.dropped == 2 and query.min_match == 4
    assert terms_from_tokens(_tokens(("asciiword", "the"), ("blank", None), ("asciiword", "of")),
                             policy).summary() == {"status": "no_terms", "terms": 0, "min_match": 0,
                                                   "dropped_terms": 0}
    assert [min_match(n, policy) for n in range(9)] == [0, 1, 2, 2, 2, 3, 3, 4, 4]
    strict = SearchPolicy(keyword_min_match_fraction=1)
    assert [min_match(n, strict) for n in (1, 3, 8)] == [1, 3, 8]
    floor3 = SearchPolicy(keyword_min_match_terms=3)
    assert [min_match(n, floor3) for n in (1, 2, 3, 4)] == [1, 2, 3, 3]


def test_postgres_tokens_match_index_normalization(api):
    policy = SearchPolicy()
    with Session(api.engine) as db:
        rank = build(db, "What rank do the adapters in Dense Adapter Stacks have?", policy)
        assert rank.terms == (("rank",), ("adapters",), ("dense",), ("adapter",), ("stacks",))
        assert rank.min_match == 3
        compound = build(db, "How did load-balancing and state-of-the-art GPU-hours change? e-mail", policy)
        assert compound.terms == (("load", "balancing"), ("state", "art"), ("gpu", "hours"),
                                  ("change",), ("mail",))
        vietnamese = build(db, "Độ trễ của bộ điều hợp là bao nhiêu?", policy)
        assert vietnamese.terms == (("độ",), ("trễ",), ("bộ",), ("điều",), ("hợp",))
        for common in ("What is the of and to?", "?!.,;: --- ...", "   ", "a b c 1 2 3"):
            assert build(db, common, policy).status == "no_terms"
        long_token = build(db, "adapter " + "y" * 65, policy)
        assert long_token.terms == (("adapter",),)
        # Every term query parses as exactly its lexemes, whatever the input characters.
        for query in HOSTILE:
            built = build(db, query, policy)
            for term, tsquery in zip(built.terms, built.term_queries()):
                assert db.scalar(text("SELECT array_to_tsvector(CAST(:l AS text[])) @@ CAST(:q AS tsquery)"),
                                 {"l": list(term), "q": tsquery})
            if built.terms:
                db.scalar(text("SELECT CAST(:q AS tsquery)"), {"q": built.any_query()})
        db.rollback()


def test_random_hostile_input_never_breaks_the_gated_query(api):
    owner = api.create_user("fuzz@example.org")
    alphabet = "abcXYZ019 '\"\\:*&|!<>()-_.@/?,;#%$~`^[]{}=+\t\nđếư日😀"
    rng = random.Random(4101)
    with Session(api.engine) as db:
        for _ in range(300):
            query = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 80)))
            built = build(db, query, get_settings().search)
            if not built.terms:
                continue
            db.execute(text(KEYWORD), {"owner": owner, "item": None, "collection": None,
                "terms": built.term_queries(), "any": built.any_query(), "min": built.min_match,
                "k": 5}).all()
        db.rollback()
    assert api.scalar("SELECT count(*) FROM library_items") == 0


def _settings(monkeypatch):
    settings = settings_with_fake_key()
    settings.generation.pricing_verified = True
    fake_provider(monkeypatch)
    return settings


def _no_network(monkeypatch):
    calls = []

    def refuse(self, request):
        calls.append(str(request.url))
        raise httpx.ConnectError("network disabled in test", request=request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    return calls


def _generation(sent, claims=True):
    def fake(messages, settings):
        payload = json.loads(messages[1]["content"])
        sent.append(payload)
        first = payload["evidence"][0]
        content = ({"claims": [{"text": "Rank sixteen.", "citations": [
            {"chunk_id": first["chunk_id"], "quote": first["text"][:12]}]}]} if claims else {"claims": []})
        return GenerationResult(json.dumps(content), 100, 20, settings.generation.model_id, "SyntheticProvider")
    return fake


def test_natural_question_matches_in_all_keyword_paths_without_provider_calls(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    settings = _settings(monkeypatch)
    ids = indexed(api, storage, a, b"## Methods\nThe adapters use rank sixteen in every layer.\n\n"
                                   b"## Results\nLatency grows slightly with batch size.\n", settings)
    app.dependency_overrides[get_settings] = lambda: settings
    question = "What rank do the adapters use?"
    # The M3b query ANDs every token, including "what", "do" and "the".
    assert api.scalar("SELECT count(*) FROM index_entries WHERE keywords @@ plainto_tsquery('simple', :q)",
                      q=question) == 0
    network = _no_network(monkeypatch)
    provider = []
    monkeypatch.setattr("app.search.embed", lambda *args, **kwargs: provider.append("embed"))
    monkeypatch.setattr("app.search.rerank", lambda *args, **kwargs: provider.append("rerank"))
    found = a.post("/search", {"query": question, "mode": "keyword", "rerank": False})
    assert found.status_code == 200, found.text
    data = found.json()
    assert data["keyword_query"] == {"status": "ok", "terms": 3, "min_match": 2, "dropped_terms": 0}
    assert data["count"] == 1 and "rank sixteen" in data["hits"][0]["text"]
    assert data["cost"] == {"embed_query": None, "rerank": None}
    sent = []
    monkeypatch.setattr("app.rag.generate", _generation(sent))
    rag = a.post("/rag/ask", {"item_id": ids["item_id"], "question": question, "mode": "keyword"})
    assert rag.status_code == 200 and rag.json()["status"] == "answered", rag.text
    assert len(sent) == 1 and ["rank sixteen" in row["text"] for row in sent[0]["evidence"]] == [True]
    # Keyword retrieval itself adds no provider call; only the RAG generation fake ran.
    assert provider == [] and network == []
    monkeypatch.setattr("app.search.embed", lambda value, cfg, input_type: EmbeddingResult(
        [.1, .1, .1], cfg.embedding.model_id, 1))
    hybrid = a.post("/search", {"query": question, "mode": "hybrid", "rerank": False}).json()
    assert hybrid["keyword_query"] == data["keyword_query"]
    assert hybrid["hits"][0]["chunk_id"] == data["hits"][0]["chunk_id"]


def test_common_words_punctuation_and_hostile_input(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    settings = _settings(monkeypatch)
    ids = indexed(api, storage, a, b"## Methods\nThe routing model was trained on English data.\n", settings)
    app.dependency_overrides[get_settings] = lambda: settings
    sent = []
    monkeypatch.setattr("app.rag.generate", _generation(sent))
    for query in ("What is the of and to?", "?!.,;:", "- - -"):
        response = a.post("/search", {"query": query, "mode": "keyword"})
        assert response.status_code == 200 and response.json()["count"] == 0
        assert response.json()["keyword_query"]["status"] == "no_terms"
        rag = a.post("/rag/ask", {"item_id": ids["item_id"], "question": query, "mode": "keyword"}).json()
        assert rag["status"] == "insufficient_evidence" and rag["reason"] == "keyword_query_without_terms"
    for query in HOSTILE:
        response = a.post("/search", {"query": query, "mode": "keyword"})
        assert response.status_code == 200, (query, response.text)
        rag = a.post("/rag/ask", {"scope": "all", "question": query, "mode": "keyword"})
        assert rag.status_code == 200, (query, rag.text)
    assert sent == []  # nothing matched, so generation was never reached
    nul = a.post("/search", {"query": "routing\u0000model", "mode": "keyword"})
    assert nul.status_code == 422 and nul.json()["error"] == "search_query_invalid"
    nul = a.post("/rag/ask", {"scope": "all", "question": "routing\u0000model", "mode": "keyword"})
    assert nul.status_code == 422 and nul.json()["error"] == "rag_question_invalid"
    too_long = a.post("/search", {"query": "routing " * 200, "mode": "keyword"})
    assert too_long.status_code == 422 and too_long.json()["error"] == "search_query_too_long"
    assert api.scalar("SELECT count(*) FROM library_items") == 1


def test_candidates_are_bounded_and_a_hit_is_not_an_answer(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    settings = _settings(monkeypatch)
    ids = indexed(api, storage, a, b"## Methods\nThe routing model was trained on English data.\n", settings)
    app.dependency_overrides[get_settings] = lambda: settings
    # Shares two of five useful terms (routing, model): below the 3-term threshold, no candidate.
    miss = a.post("/search", {"query": "What BLEU did the routing model reach on French?", "mode": "keyword"})
    assert miss.json()["keyword_query"]["min_match"] == 3 and miss.json()["count"] == 0
    # Four of five terms match although the paper says nothing about Yoruba: a candidate exists,
    # and whether it answers the question stays with generation and the citation checks.
    question = "How was the routing model trained on Yoruba data?"
    assert a.post("/search", {"query": question, "mode": "keyword"}).json()["count"] == 1
    sent = []
    monkeypatch.setattr("app.rag.generate", _generation(sent, claims=False))
    rag = a.post("/rag/ask", {"item_id": ids["item_id"], "question": question, "mode": "keyword"}).json()
    assert len(sent) == 1 and rag["status"] == "insufficient_evidence" and rag["claims"] == []


def test_expanded_query_keeps_acl_membership_and_index_gates_before_top_k(upload_api, monkeypatch):
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    settings = _settings(monkeypatch)
    settings.search.keyword_top_k = 1
    inside = indexed(api, storage, a, b"## Methods\nRouting experts improve translation inside.\n", settings)
    outside = indexed(api, storage, a, b"## Methods\nRouting experts improve translation quality "
                                       b"for Kinyarwanda outside.\n", settings)
    secret = indexed(api, storage, b, b"## Methods\nRouting experts improve translation quality for "
                                      b"Kinyarwanda BSECRET quality Kinyarwanda.\n", settings)
    common = api.add_item(owner=None, title="Routing experts improve translation quality Kinyarwanda")
    collection = a.post("/collections", {"name": "Reading"}).json()["collection"]["id"]
    for item in (inside["item_id"], common):
        assert a.post(f"/collections/{collection}/items", {"item_id": str(item)}).status_code == 201
    app.dependency_overrides[get_settings] = lambda: settings
    sent = []
    monkeypatch.setattr("app.rag.generate", _generation(sent, claims=False))
    question = "Which routing experts improve translation quality for Kinyarwanda?"

    def hits(browser, **scope):
        response = browser.post("/search", {"query": question, "mode": "keyword", **scope})
        assert response.status_code == 200, response.text
        assert "BSECRET" not in response.text or browser is b
        return [hit["item_id"] for hit in response.json()["hits"]]

    # B's chunk matches every term and more often, yet A's single slot goes to A's best chunk.
    assert hits(a) == [outside["item_id"]]
    assert hits(a, collection_id=collection) == [inside["item_id"]]
    assert hits(b) == [secret["item_id"]]
    assert hits(admin) == []
    for target in (secret["item_id"], str(common), str(uuid.uuid4())):
        denied = a.post("/search", {"query": question, "mode": "keyword", "item_id": target})
        assert denied.status_code == 404 and denied.json() == {"error": "not_found"}
    for scope in ({"scope": "all"}, {"collection_id": collection}):
        a.post("/rag/ask", {"question": question, "mode": "keyword", **scope})
    assert len(sent) == 2 and "BSECRET" not in json.dumps(sent)
    assert {row["item_id"] for payload in sent for row in payload["evidence"]} <= {
        inside["item_id"], outside["item_id"]}
    assert {row["item_id"] for row in sent[1]["evidence"]} == {inside["item_id"]}
    stale = a.post("/search", {"query": question, "mode": "keyword"}).json()["hits"][0]["chunk_id"]
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                     {"i": uuid.UUID(outside["item_id"])})
    assert hits(a) == [inside["item_id"]]
    assert a.get(f"/search/chunks/{stale}").status_code == 404
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET content_status='metadata_only' WHERE id=:i"),
                     {"i": uuid.UUID(inside["item_id"])})
    assert hits(a) == [] and hits(a, collection_id=collection) == []
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE library_items SET content_status='indexed' WHERE id=:i"),
                     {"i": uuid.UUID(inside["item_id"])})
        conn.execute(text("UPDATE index_generations SET state='superseded' WHERE item_id=:i"),
                     {"i": uuid.UUID(inside["item_id"])})
    assert hits(a) == [] and hits(b) == [secret["item_id"]]


def test_keyword_rank_is_deterministic_and_bounded(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    settings = _settings(monkeypatch)
    body = (b"## Four\nZircon quartz basalt gneiss.\n\n## Three\nZircon quartz basalt.\n\n"
            b"## Two A\nZircon quartz.\n\n## Two B\nZircon quartz.\n\n## One\nZircon only.\n")
    indexed(api, storage, a, body, settings)
    app.dependency_overrides[get_settings] = lambda: settings
    question = "zircon, quartz, basalt & gneiss?"

    def ranked():
        response = a.post("/search", {"query": question, "mode": "keyword"})
        assert response.status_code == 200
        return [(hit["chunk_id"], hit["text"]) for hit in response.json()["hits"]]

    first = ranked()
    texts = [row[1] for row in first]
    assert "Zircon quartz basalt gneiss" in texts[0] and "Zircon quartz basalt." in texts[1]
    tied = sorted(row[0] for row in first[2:])
    assert [row[0] for row in first[2:]] == tied and len(tied) == 2  # ties: chunk ID
    assert all("only" not in t for t in texts)  # 1 of 4 terms is below the 2-term threshold
    assert ranked() == first == ranked()
    settings.search.keyword_top_k = 2
    assert ranked() == first[:2]
    # Terms beyond the cap are dropped, never silently widened into an OR over everything.
    settings.search.keyword_max_terms = 2
    capped = a.post("/search", {"query": "alpha bravo zircon quartz", "mode": "keyword"}).json()
    assert capped["keyword_query"] == {"status": "ok", "terms": 2, "min_match": 2, "dropped_terms": 2}
    assert capped["count"] == 0


def test_keyword_sql_applies_the_full_gate_inside_the_ranked_subquery():
    inner = KEYWORD.split(") hit WHERE", 1)[0]
    assert BASE in inner and "LIMIT" not in inner
    assert KEYWORD.rstrip().endswith("LIMIT :k")
