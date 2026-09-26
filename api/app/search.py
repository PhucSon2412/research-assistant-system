"""Private, active-generation retrieval and cost-accounted provider calls."""
from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP

import httpx
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.authz import Principal
from app.config import Settings
from app.ingest.embedding import EmbeddingError, embed
from app.models import EmbeddingBudgetDay, EmbeddingCall, EmbeddingUserBudgetDay, LibraryItem, SearchRequestDay, SearchUsage
from app.provider_capacity import LOCK, lease_deadline, reap_expired, reservation_heartbeat
from app.provider_identity import VERIFIED_RERANK_IDENTITIES, audit_identity, verified_identity

log = logging.getLogger(__name__)
MONEY = Decimal("0.00000001")


def money(units: int, price: float) -> Decimal:
    return (Decimal(units) * Decimal(str(price)) / Decimal(1_000_000)).quantize(MONEY, rounding=ROUND_UP)


def fuse(keyword: list[uuid.UUID], vector: list[uuid.UUID], offset: int = 60) -> list[uuid.UUID]:
    """Stable RRF: ties resolve by first keyword/vector rank and then UUID."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in (keyword, vector):
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (offset + rank)
    return sorted(scores, key=lambda c: (-scores[c], keyword.index(c) if c in keyword else 10**9,
                                          vector.index(c) if c in vector else 10**9, str(c)))


def rerank_order(ids: list[uuid.UUID], results: list[dict]) -> list[uuid.UUID]:
    """Map provider indices to the exact sent candidate order, rejecting malformed mappings."""
    seen: set[int] = set()
    scored = []
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("invalid rerank result")
        index, score = row.get("index"), row.get("relevance_score")
        if (type(index) is not int or index < 0 or index >= len(ids) or index in seen
                or type(score) not in (float, int) or not math.isfinite(score)):
            raise ValueError("invalid rerank mapping")
        seen.add(index)
        scored.append((ids[index], float(score), index))
    if len(seen) != len(ids):
        raise ValueError("incomplete rerank mapping")
    return [row[0] for row in sorted(scored, key=lambda row: (-row[1], row[2]))]


def _day() -> datetime:
    return datetime.now(timezone.utc)


def limit_request(db: Session, principal: Principal, settings: Settings) -> None:
    day = _day().date()
    db.execute(text("INSERT INTO search_request_days(day,user_id) VALUES (:d,:u) ON CONFLICT DO NOTHING"),
               {"d": day, "u": principal.user_id})
    counter = db.scalar(select(SearchRequestDay).where(SearchRequestDay.day == day,
        SearchRequestDay.user_id == principal.user_id).with_for_update())
    if counter.request_count >= settings.search.max_requests_per_user_day:
        db.rollback()
        raise ApiError(429, "search_request_limit")
    counter.request_count += 1
    db.commit()


def _total(db: Session, day, task: str | None = None, user_id: uuid.UUID | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(func.coalesce(SearchUsage.cost_usd, SearchUsage.reserved_usd)), 0)).where(
        SearchUsage.day == day, SearchUsage.status.in_(("reserved", "completed", "uncertain")))
    if task:
        q = q.where(SearchUsage.task_type == task)
    if user_id:
        q = q.where(SearchUsage.user_id == user_id)
    return Decimal(db.scalar(q))


def reserve(db: Session, principal: Principal, settings: Settings, task: str, amount: Decimal) -> uuid.UUID:
    """One advisory lock serializes search and document embedding reservations."""
    day = _day().date()
    reap_expired(db.get_bind())
    db.execute(text(LOCK))
    if task == "embed_query":
        active_document = db.scalar(select(func.count()).select_from(EmbeddingCall).where(
            EmbeddingCall.status == "reserved"))
        active_query = db.scalar(select(func.count()).select_from(SearchUsage).where(
            SearchUsage.task_type == "embed_query", SearchUsage.status == "reserved"))
        if active_document + active_query >= settings.embedding.max_concurrent_requests:
            db.rollback()
            raise ApiError(429, "search_embedding_concurrency_limit")
    elif task == "rerank":
        active_rerank = db.scalar(select(func.count()).select_from(SearchUsage).where(
            SearchUsage.task_type == "rerank", SearchUsage.status == "reserved"))
        if active_rerank >= settings.rerank.max_concurrent_requests:
            db.rollback()
            raise ApiError(429, "search_rerank_concurrency_limit")
    else:
        db.rollback()
        raise ValueError("invalid search task type")
    used = _total(db, day)
    doc = db.get(EmbeddingBudgetDay, day)
    doc_used = (doc.committed_usd + doc.reserved_usd) if doc else Decimal(0)
    app_cap = Decimal(str(settings.budget.app_daily_usd))
    if used + doc_used + amount > app_cap:
        db.rollback()
        raise ApiError(429, "search_app_budget_exhausted")
    warnings = []
    if task == "embed_query":
        cap = Decimal(str(settings.budget.embedding_daily_usd))
        user_cap = Decimal(str(settings.budget.user_embedding_daily_usd))
        doc_user = db.get(EmbeddingUserBudgetDay, (day, principal.user_id))
        doc_user_used = (doc_user.committed_usd + doc_user.reserved_usd) if doc_user else Decimal(0)
        query_spend = _total(db, day, "embed_query")
        if query_spend + doc_used + amount > cap:
            db.rollback()
            raise ApiError(429, "search_embedding_budget_exhausted")
        if _total(db, day, "embed_query", principal.user_id) + doc_user_used + amount > user_cap:
            db.rollback()
            raise ApiError(429, "search_user_budget_exhausted")
        if query_spend + doc_used + amount >= cap * Decimal(str(settings.budget.embedding_warning_fraction)):
            warnings.append("embedding_bucket_near_limit")
        if (_total(db, day, "embed_query", principal.user_id) + doc_user_used + amount >=
                user_cap * Decimal(str(settings.budget.user_embedding_warning_fraction))):
            warnings.append("embedding_user_quota_near_limit")
        model = settings.embedding.model_id
        lease_seconds = settings.embedding.reservation_lease_seconds
    else:
        rerank_spend = _total(db, day, "rerank")
        rerank_cap = Decimal(str(settings.budget.rerank_daily_usd))
        if rerank_spend + amount > rerank_cap:
            db.rollback()
            raise ApiError(429, "search_rerank_budget_exhausted")
        if _total(db, day, "rerank", principal.user_id) + amount > Decimal(str(settings.budget.user_rerank_daily_usd)):
            db.rollback()
            raise ApiError(429, "search_user_rerank_budget_exhausted")
        if rerank_spend + amount >= rerank_cap * Decimal(str(settings.budget.rerank_warning_fraction)):
            warnings.append("rerank_bucket_near_limit")
        model = settings.rerank.model_id
        lease_seconds = settings.rerank.reservation_lease_seconds
    usage = SearchUsage(user_id=principal.user_id, day=day, task_type=task, model_id=model,
        provider="openrouter", status="reserved", reserved_usd=amount, cost_status="unknown",
        warnings=warnings, lease_until=lease_deadline(lease_seconds))
    db.add(usage)
    db.flush()
    usage_id = usage.id
    db.commit()
    return usage_id


def settle(db: Session, usage_id: uuid.UUID, *, tokens: int | None = None,
           units: int | None = None, price: float | None = None, rejected: bool = False,
           response_model_id: str | None = None, response_provider: str | None = None) -> str:
    usage = db.get(SearchUsage, usage_id, with_for_update=True)
    if usage.status in ("completed", "rejected"):
        status = usage.cost_status
        db.commit()
        return status
    usage.response_model_id = response_model_id
    usage.response_provider = response_provider
    if rejected:
        usage.status, usage.cost_usd, usage.cost_status = "rejected", Decimal(0), "rejected_no_charge"
    elif tokens is not None and price is not None:
        usage.status = "completed"
        usage.input_tokens = tokens
        usage.search_units = units
        usage.cost_usd = money(tokens, price)
        usage.cost_status = "usage_price_estimate"
    else:
        usage.status = "uncertain"
        usage.cost_status = "unknown_reserved_ceiling"
    usage.finished_at = _day()
    usage.lease_until = None
    status = usage.cost_status
    db.commit()
    return status


def _scope(db: Session, principal: Principal, item_id: uuid.UUID | None):
    if item_id is not None:
        item = db.scalar(select(LibraryItem).where(LibraryItem.id == item_id,
            LibraryItem.visibility == "private", LibraryItem.owner_user_id == principal.user_id))
        if item is None:
            raise ApiError(404, "not_found")
    rows = db.execute(text("""SELECT g.model_id, g.dimensions FROM library_items i
        JOIN index_generations g ON g.id=i.active_index_generation_id AND g.item_id=i.id
        JOIN paper_versions v ON v.id=g.version_id AND v.item_id=i.id
        JOIN source_files f ON f.id=g.source_file_id AND f.version_id=v.id AND f.item_id=i.id
        WHERE i.visibility='private' AND i.owner_user_id=:owner AND i.content_status='indexed'
          AND i.active_version_id=g.version_id AND i.rights_epoch=g.rights_epoch
          AND g.state='active' AND g.dimensions IS NOT NULL
          AND (CAST(:item AS uuid) IS NULL OR i.id=CAST(:item AS uuid))"""),
        {"owner": principal.user_id, "item": item_id}).all()
    return {(r.model_id, r.dimensions) for r in rows}


BASE = """FROM index_entries e
 JOIN index_generations g ON g.id=e.generation_id
 JOIN library_items i ON i.id=g.item_id AND i.active_index_generation_id=g.id
 JOIN paper_versions v ON v.id=g.version_id AND v.item_id=i.id AND i.active_version_id=v.id
 JOIN source_files f ON f.id=g.source_file_id AND f.version_id=v.id AND f.item_id=i.id
 JOIN chunks c ON c.id=e.chunk_id AND c.item_id=i.id AND c.version_id=v.id AND c.source_file_id=f.id
 JOIN sections s ON s.id=c.section_id AND s.item_id=i.id AND s.version_id=v.id AND s.source_file_id=f.id
 JOIN embedding_calls ec ON ec.id=e.embedding_call_id AND ec.source_file_id=f.id
 WHERE i.visibility='private' AND i.owner_user_id=:owner AND i.content_status='indexed'
   AND i.rights_epoch=g.rights_epoch AND g.state='active' AND g.dimensions IS NOT NULL
   AND s.kind IN ('body','abstract') AND c.pipeline_version=g.pipeline_version
   AND c.chunk_hash=e.chunk_hash AND c.chunk_hash=ec.chunk_hash AND c.text=e.text
   AND ec.status='completed' AND ec.model_id=g.model_id AND ec.dimensions=g.dimensions
   AND vector_dims(ec.vector)=g.dimensions
   AND (CAST(:item AS uuid) IS NULL OR i.id=CAST(:item AS uuid))"""


def _rank(db: Session, principal: Principal, item_id, settings: Settings, query: str,
          vector: list[float] | None):
    params = {"owner": principal.user_id, "item": item_id, "q": query}
    start = time.perf_counter()
    keyword = db.execute(text("SELECT e.chunk_id " + BASE + " AND e.keywords @@ plainto_tsquery('simple',:q)"
        " ORDER BY ts_rank_cd(e.keywords,plainto_tsquery('simple',:q)) DESC,e.chunk_id LIMIT :k"),
        {**params, "k": settings.search.keyword_top_k}).scalars().all()
    keyword_ms = int((time.perf_counter() - start) * 1000)
    semantic, vector_ms = [], 0
    if vector is not None:
        start = time.perf_counter()
        semantic = db.execute(text("SELECT e.chunk_id " + BASE +
            " AND g.model_id=:model AND g.dimensions=:dim"
            " ORDER BY ec.vector <=> CAST(:vector AS vector),e.chunk_id LIMIT :k"),
            {**params, "model": settings.embedding.model_id, "dim": len(vector),
             "vector": "[" + ",".join(str(x) for x in vector) + "]", "k": settings.search.vector_top_k}).scalars().all()
        vector_ms = int((time.perf_counter() - start) * 1000)
    return keyword, semantic, keyword_ms, vector_ms


def hydrate(db: Session, principal: Principal, item_id, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    if not ids:
        return {}
    stmt = text("""SELECT e.chunk_id, i.id item_id, v.id version_id, f.id file_id, i.title,
        v.coverage, i.rights_epoch, g.id generation_id, c.text, c.locator_kind,
        c.start_page, c.end_page, c.start_line, c.end_line, c.start_offset, c.end_offset,
        c.locator_detail, s.kind section_kind
        """ + BASE + " AND e.chunk_id IN :ids").bindparams(bindparam("ids", expanding=True))
    rows = db.execute(stmt, {"owner": principal.user_id, "item": item_id, "ids": ids}).mappings().all()
    return {r["chunk_id"]: dict(r) for r in rows}


def open_chunk(db: Session, principal: Principal, chunk_id: uuid.UUID) -> dict:
    """Source-viewer read: reapply the full gate to this exact active chunk."""
    row = hydrate(db, principal, None, [chunk_id]).get(chunk_id)
    if row is None:
        raise ApiError(404, "not_found")
    if db.execute(text("SELECT id FROM library_items WHERE id=:i FOR SHARE"),
                  {"i": row["item_id"]}).first() is None:
        raise ApiError(404, "not_found")
    fresh = hydrate(db, principal, None, [chunk_id]).get(chunk_id)
    if fresh is None or fresh["rights_epoch"] != row["rights_epoch"] or fresh["generation_id"] != row["generation_id"]:
        raise ApiError(404, "not_found")
    response = {"chunk_id": str(chunk_id), "item_id": str(fresh["item_id"]),
        "version_id": str(fresh["version_id"]), "source_file_id": str(fresh["file_id"]),
        "title": fresh["title"], "coverage": fresh["coverage"], "text": fresh["text"],
        "locator_kind": fresh["locator_kind"], "start_page": fresh["start_page"],
        "end_page": fresh["end_page"], "start_line": fresh["start_line"],
        "end_line": fresh["end_line"], "start_offset": fresh["start_offset"],
        "end_offset": fresh["end_offset"], "locator_detail": fresh["locator_detail"],
        "section_kind": fresh["section_kind"]}
    db.commit()
    return response


class RerankError(Exception):
    def __init__(self, code: str, rejected: bool = False,
                 response_model_id: str | None = None, response_provider: str | None = None):
        self.code, self.rejected = code, rejected
        self.response_model_id, self.response_provider = response_model_id, response_provider


@dataclass(frozen=True)
class RerankResult:
    results: list[dict]
    tokens: int | None
    units: int | None
    response_model_id: str
    response_provider: str

    def __iter__(self):
        # Existing callers can still unpack the ranking, tokens and units.
        return iter((self.results, self.tokens, self.units))


def rerank(query: str, docs: list[str], settings: Settings) -> RerankResult:
    policy = settings.rerank
    try:
        with httpx.Client(timeout=policy.request_timeout_seconds) as client:
            response = client.post(policy.endpoint, headers={
                "Authorization": f"Bearer {settings.embedding.api_key.get_secret_value()}",
                "Content-Type": "application/json"}, json={"model": policy.model_id,
                "query": query, "documents": docs, "top_n": len(docs)})
    except httpx.RequestError:
        raise RerankError("rerank_outcome_uncertain") from None
    if response.status_code == 429:
        raise RerankError("rerank_rate_limited", True)
    if response.status_code >= 500:
        raise RerankError("rerank_outcome_uncertain")
    if response.status_code != 200:
        raise RerankError("rerank_rejected", True)
    response_model = response_provider = None
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        response_model = audit_identity(data.get("model"))
        response_provider = audit_identity(data.get("provider"))
        if not verified_identity(policy.model_id, response_model, response_provider,
                                 VERIFIED_RERANK_IDENTITIES):
            raise RerankError("rerank_response_invalid", response_model_id=response_model,
                              response_provider=response_provider)
        if (not isinstance(data.get("results"), list)
                or any(not isinstance(row, dict) for row in data["results"])):
            raise ValueError()
        usage = data.get("usage")
        if usage is None:
            usage = {}
        elif not isinstance(usage, dict):
            raise ValueError()
        tokens, units = usage.get("total_tokens"), usage.get("search_units")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError()
        if units is not None and (type(units) is not int or units < 0):
            raise ValueError()
        return RerankResult(data["results"], tokens, units, response_model, response_provider)
    except (KeyError, TypeError, ValueError):
        raise RerankError("rerank_response_invalid", response_model_id=response_model,
                          response_provider=response_provider) from None


def search(db: Session, principal: Principal, settings: Settings, query: str,
           mode: str, item_id: uuid.UUID | None, use_rerank: bool) -> dict:
    if len(query.encode("utf-8")) > settings.search.max_query_bytes:
        raise ApiError(422, "search_query_too_long")
    scopes = _scope(db, principal, item_id)  # before any paid call
    limit_request(db, principal, settings)
    timings = {"keyword_ms": 0, "vector_ms": 0, "provider_ms": 0, "rerank_ms": 0}
    vector, embed_usage, rerank_usage = None, None, None
    if mode == "hybrid" and scopes:
        if len(scopes) != 1 or next(iter(scopes))[0] != settings.embedding.model_id:
            raise ApiError(409, "search_index_model_mismatch")
        dimension = next(iter(scopes))[1]
        if not settings.embedding.pricing_verified or settings.embedding.api_key is None:
            raise ApiError(503, "search_embedding_unavailable")
        ceiling = money(settings.search.max_query_bytes, settings.embedding.max_usd_per_million_tokens)
        if ceiling > Decimal(str(settings.search.max_query_request_usd)):
            raise ApiError(503, "search_embedding_ceiling_unverified")
        embed_usage = reserve(db, principal, settings, "embed_query", ceiling)
        try:
            if _scope(db, principal, item_id) != scopes:
                settle(db, embed_usage, rejected=True)
                raise ApiError(409, "search_scope_changed")
            start = time.perf_counter()
            with reservation_heartbeat(db.get_bind(), SearchUsage, embed_usage,
                                       settings.embedding.reservation_lease_seconds):
                result = embed(query, settings, input_type="search_query")
            timings["provider_ms"] += int((time.perf_counter() - start) * 1000)
        except EmbeddingError as exc:
            settle(db, embed_usage, rejected=exc.definitely_rejected,
                   response_model_id=exc.response_model_id,
                   response_provider=exc.response_provider)
            raise ApiError(503, exc.code) from None
        if result.model_id != settings.embedding.model_id or len(result.vector) != dimension:
            settle(db, embed_usage, response_model_id=result.response_model_id,
                   response_provider=result.response_provider)
            raise ApiError(503, "search_embedding_dimensions_mismatch")
        if result.input_tokens is not None and result.input_tokens > settings.search.max_query_bytes:
            settle(db, embed_usage, response_model_id=result.response_model_id,
                   response_provider=result.response_provider)
            raise ApiError(503, "search_embedding_usage_exceeds_reservation")
        settle(db, embed_usage, tokens=result.input_tokens,
               price=settings.embedding.max_usd_per_million_tokens,
               response_model_id=result.response_model_id,
               response_provider=result.response_provider)
        vector = result.vector
    keyword, semantic, timings["keyword_ms"], timings["vector_ms"] = _rank(
        db, principal, item_id, settings, query, vector)
    ids = fuse(keyword, semantic)[:settings.search.max_results]
    hydrated = hydrate(db, principal, item_id, ids)
    ids = [i for i in ids if i in hydrated]
    rerank_status = "not_requested"
    if use_rerank and ids:
        rerank_status = "degraded_unavailable"
        policy = settings.rerank
        if policy.pricing_verified and settings.embedding.api_key is not None:
            sent_ids = ids[:policy.max_candidates]
            documents = [hydrated[i]["text"].encode("utf-8")[:policy.max_text_bytes].decode("utf-8", "ignore") for i in sent_ids]
            # Every byte is an upper bound on UTF-8 token count; include repeated query and envelope.
            ceiling = money(sum(len(d.encode("utf-8")) for d in documents) +
                            len(sent_ids) * len(query.encode("utf-8")) + 1024,
                            policy.max_usd_per_million_tokens)
            if ceiling <= Decimal(str(policy.max_request_usd)):
                try:
                    rerank_usage = reserve(db, principal, settings, "rerank", ceiling)
                    fresh = hydrate(db, principal, item_id, sent_ids)
                    if set(fresh) != set(sent_ids) or any(fresh[i]["rights_epoch"] != hydrated[i]["rights_epoch"]
                                                         or fresh[i]["generation_id"] != hydrated[i]["generation_id"]
                                                         or fresh[i]["text"] != hydrated[i]["text"] for i in sent_ids):
                        settle(db, rerank_usage, rejected=True)
                        raise ApiError(409, "search_scope_changed")
                    start = time.perf_counter()
                    with reservation_heartbeat(db.get_bind(), SearchUsage, rerank_usage,
                                               policy.reservation_lease_seconds):
                        ranked = rerank(query, documents, settings)
                    results, tokens, units = ranked
                    response_model = getattr(ranked, "response_model_id", None)
                    response_provider = getattr(ranked, "response_provider", None)
                    timings["rerank_ms"] = int((time.perf_counter() - start) * 1000)
                    timings["provider_ms"] += timings["rerank_ms"]
                    try:
                        ordered = rerank_order(sent_ids, results)
                    except ValueError:
                        raise RerankError("rerank_response_invalid",
                            response_model_id=response_model, response_provider=response_provider) from None
                    if tokens is not None and tokens > sum(len(d.encode("utf-8")) for d in documents) + len(sent_ids) * len(query.encode("utf-8")) + 1024:
                        raise RerankError("rerank_usage_exceeds_reservation",
                            response_model_id=response_model, response_provider=response_provider)
                    settle(db, rerank_usage, tokens=tokens, units=units, price=policy.max_usd_per_million_tokens,
                           response_model_id=response_model, response_provider=response_provider)
                    ids = ordered + [i for i in ids if i not in set(sent_ids)]
                    rerank_status = "applied"
                except RerankError as exc:
                    if rerank_usage is not None:
                        settle(db, rerank_usage, rejected=exc.rejected,
                               response_model_id=exc.response_model_id,
                               response_provider=exc.response_provider)
                    rerank_status = "degraded_" + exc.code
                except ApiError as exc:
                    if exc.code == "search_scope_changed":
                        raise
                    rerank_status = ("degraded_concurrency" if exc.code == "search_rerank_concurrency_limit"
                                     else "degraded_budget")
            else:
                rerank_status = "degraded_ceiling"
    final = hydrate(db, principal, item_id, ids)
    if set(final) != set(ids) or any(final[i]["rights_epoch"] != hydrated[i]["rights_epoch"]
                                    or final[i]["generation_id"] != hydrated[i]["generation_id"]
                                    or final[i]["text"] != hydrated[i]["text"] for i in ids):
        raise ApiError(409, "search_scope_changed")
    # Hold a share lock through response materialization so a concurrent revoke cannot
    # commit between this last check and assembling the outgoing body.
    if ids:
        db.execute(text("SELECT id FROM library_items WHERE id = ANY(:ids) FOR SHARE"),
                   {"ids": list({final[i]["item_id"] for i in ids})}).all()
        locked = hydrate(db, principal, item_id, ids)
        if set(locked) != set(ids) or any(locked[i]["rights_epoch"] != final[i]["rights_epoch"]
                                         or locked[i]["generation_id"] != final[i]["generation_id"]
                                         or locked[i]["text"] != final[i]["text"] for i in ids):
            raise ApiError(409, "search_scope_changed")
        final = locked
    hits = [{"chunk_id": str(i), "item_id": str(final[i]["item_id"]),
        "version_id": str(final[i]["version_id"]), "source_file_id": str(final[i]["file_id"]),
        "title": final[i]["title"], "text": final[i]["text"],
        "coverage": final[i]["coverage"], "locator_kind": final[i]["locator_kind"],
        "start_page": final[i]["start_page"], "end_page": final[i]["end_page"],
        "start_line": final[i]["start_line"], "end_line": final[i]["end_line"],
        "start_offset": final[i]["start_offset"], "end_offset": final[i]["end_offset"],
        "locator_detail": final[i]["locator_detail"], "section_kind": final[i]["section_kind"]}
        for i in ids]
    db.commit()
    usage = {task: db.get(SearchUsage, identifier) if identifier else None for task, identifier in
             (("embed_query", embed_usage), ("rerank", rerank_usage))}
    costs = {task: {"status": row.cost_status, "estimated_usd": str(row.cost_usd) if row.cost_usd is not None else None,
                    "reserved_usd": str(row.reserved_usd), "input_tokens": row.input_tokens,
                    "search_units": row.search_units, "warnings": row.warnings} if row else None for task, row in usage.items()}
    log.info("search mode=%s rerank=%s candidates=%d hits=%d keyword_ms=%d vector_ms=%d rerank_ms=%d provider_ms=%d embed_cost=%s rerank_cost=%s embed_warnings=%s rerank_warnings=%s",
             mode, rerank_status, len(keyword) + len(semantic), len(hits),
             timings["keyword_ms"], timings["vector_ms"], timings["rerank_ms"], timings["provider_ms"],
             costs["embed_query"]["estimated_usd"] if costs["embed_query"] else None,
             costs["rerank"]["estimated_usd"] if costs["rerank"] else None,
             costs["embed_query"]["warnings"] if costs["embed_query"] else [],
             costs["rerank"]["warnings"] if costs["rerank"] else [])
    return {"mode": mode, "rerank_status": rerank_status, "count": len(hits), "hits": hits,
            "candidate_count": len(keyword) + len(semantic), "timings": timings,
            "cost": costs}
