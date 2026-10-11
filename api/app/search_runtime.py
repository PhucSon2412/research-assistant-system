"""Shared runtime retrieval for HTTP search, RAG and the citation viewer.

Every phase returns detached scalars and releases its Session. No SQL/locks
survive into provider IO, heartbeat join, response serialization or another phase.
"""
import math
import time
import uuid
from types import MappingProxyType
from collections.abc import Mapping
from decimal import Decimal

from sqlalchemy import select, text

from app import auth_safety, search as s, search_accounting as accounting
from app import search_boundary as boundary
from app.api.errors import ApiError
from app.api.resource_writes import _actor
from app.authz import Principal, visible_items_clause
from app.identity.final_caller import final_caller
from app.identity.predicates import lock_users
from app.ingest.checkpoints import frozen
from app.models import Collection, LibraryItem, SearchRequestDay, SearchUsage


def immutable(value):
    """Detached content proofs cannot be edited by evaluation/capture consumers."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: immutable(v) for key, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(immutable(v) for v in value)
    if isinstance(value, set):
        return frozenset(value)
    return value


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(v) for key, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def caller(db, principal, settings, clock, csrf=None):
    if csrf is not None:
        current = _actor(db, principal, csrf, clock, settings.auth.session_idle)
    else:
        proof = final_caller(db, session_id=principal.session_id, user_id=principal.user_id,
            clock=clock, idle=settings.auth.session_idle)
        current = Principal(proof.user_id, proof.role, proof.session_id, proof.email, proof.display_name)
    if current.role != principal.role:
        raise ApiError(409, 'search_scope_changed')
    return current


def snapshot(db, principal, item, collection):
    models = s._scope(db, principal, item, collection, include_common=True)
    # Detect exact tuple/rights changes even if both scopes use the same model/dimension.
    rows = db.execute(text('''SELECT i.id,i.rights_epoch,i.visibility,i.owner_user_id,
        g.id,g.version_id,g.source_file_id,g.provider,g.pipeline_version,g.index_version,
        f.checksum_sha256,f.byte_size,g.model_id,g.dimensions,v.coverage,
        (i.visibility='private' OR (i.id,g.version_id,f.id,g.id) IN ('''
        + s.common_fulltext_sql(external_processing=True) + ''')) provider_ok,
        (SELECT jsonb_agg(jsonb_build_array(r.id,r.version_id,r.source_file_id,r.file_checksum_sha256,
            r.rights_epoch_at_grant,r.permits_original_download,r.permits_fulltext_search,
            r.permits_external_processing,r.scope_audience,r.scope_conditions,p.item_id,p.verified_byte_size)
            ORDER BY r.id,p.item_id)
         FROM access_rights r JOIN common_publications p ON p.access_rights_id=r.id
         WHERE r.item_id=i.id AND r.revoked_at IS NULL AND p.revoked_at IS NULL) rights_binding
        FROM library_items i JOIN index_generations g ON g.id=i.active_index_generation_id
        JOIN paper_versions v ON v.id=g.version_id AND v.item_id=i.id
        JOIN source_files f ON f.id=g.source_file_id
        WHERE ''' + s._audience(True) + ''' AND i.content_status='indexed'
        AND i.active_version_id=g.version_id AND i.rights_epoch=g.rights_epoch AND g.state='active'
        AND (CAST(:item AS uuid) IS NULL OR i.id=:item)
        AND (CAST(:collection AS uuid) IS NULL OR EXISTS
            (SELECT 1 FROM collection_items ci WHERE ci.collection_id=:collection AND ci.item_id=i.id))
        ORDER BY i.id'''), {'owner': principal.user_id, 'item': item, 'collection': collection}).all()
    members = tuple(db.scalars(text('SELECT item_id FROM collection_items WHERE collection_id=:c ORDER BY item_id'),
        {'c': collection})) if collection else ()
    return immutable((frozenset(models), tuple(tuple(r) for r in rows), members))


def locks(db, principal, settings, rows, item, collection):
    db.execute(text(f'SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}'))
    ids = {row['item_id'] for row in rows.values()}
    if item:
        ids.add(item)
    if ids:
        db.execute(select(LibraryItem.id).where(LibraryItem.id.in_(ids), visible_items_clause(principal))
            .order_by(LibraryItem.id).with_for_update(read=True)).all()
    if collection:
        db.execute(select(Collection.id).where(Collection.id == collection, Collection.owner_user_id == principal.user_id)
            .with_for_update(read=True)).all()


def observe(db, principal, settings, clock, item, collection, expected=None, before=None, csrf=None, writer=False):
    auth_safety.require_final(db, writer=writer)
    current = caller(db, principal, settings, clock, csrf)
    try:
        scope = snapshot(db, current, item, collection)
    except ApiError as exc:
        caller(db, principal, settings, clock, csrf)
        if expected is not None and exc.status_code == 404:
            raise ApiError(409, 'search_scope_changed') from None
        raise
    fresh = s._hydrate_scope(db, current, item, list(before or {}), collection, True)
    stable = snapshot(db, current, item, collection)
    if scope != stable or (expected is not None and scope != expected) or (before is not None and immutable(fresh) != immutable(before)):
        caller(db, principal, settings, clock, csrf)
        raise ApiError(409, 'search_scope_changed')
    caller(db, principal, settings, clock, csrf)  # clock after every SQL and journal wait
    return scope, fresh


def request_limit(engine, principal, settings, clock, csrf, item, collection, expected=None):
    def write(db, outcome):
        lock_users(db, [principal.user_id])
        caller(db, principal, settings, clock, csrf)
        locks(db, principal, settings, {}, item, collection)
        day = clock().date()
        db.execute(text('INSERT INTO search_request_days(day,user_id) VALUES (:d,:u) ON CONFLICT DO NOTHING'),
            {'d': day, 'u': principal.user_id})
        row = db.scalar(select(SearchRequestDay).where(SearchRequestDay.day == day,
            SearchRequestDay.user_id == principal.user_id).with_for_update())
        if row.request_count >= settings.search.max_requests_per_user_day:
            observe(db, principal, settings, clock, item, collection, csrf=csrf, writer=True)
            raise ApiError(429, 'search_request_limit')
        row.request_count += 1
        db.flush()
        scope, _ = observe(db, principal, settings, clock, item, collection, expected=expected, csrf=csrf, writer=True)
        if day != clock().date():
            raise ApiError(503, 'search_counter_day_changed')
        boundary.commit(db, outcome)
        return scope
    return boundary.checkpoint(engine, 'counter', write)


def admission(engine, principal, settings, clock, csrf, item, collection, scope, before, task, ceiling, operation):
    accounting.reap(engine, settings, clock=clock)
    def write(db, outcome):
        lock_users(db, [principal.user_id])
        caller(db, principal, settings, clock, csrf)
        locks(db, principal, settings, before or {}, item, collection)
        row = accounting.admit(db, principal, settings, task, ceiling, operation, clock)
        proof = frozen(row)
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        observe(db, principal, settings, clock, item, collection, scope, before, csrf, writer=True)
        db.refresh(row)
        at = clock()
        if (not accounting.matches(row, proof) or row.status != 'reserved'
                or at >= row.lease_until or at.date() != row.day):
            raise ApiError(503, 'search_reservation_lost')
        caller(db, principal, settings, clock, csrf)
        boundary.commit(db, outcome)
        return proof
    proof = boundary.checkpoint(engine, 'reservation', write)
    # A fresh transaction is mandatory after admission commit, immediately before dispatch.
    def presend(db, _):
        locks(db, principal, settings, before or {}, item, collection)
        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update(read=True))
        observe(db, principal, settings, clock, item, collection, scope, before, csrf)
        if not accounting.matches(row, proof) or row.status != 'reserved' or row.lease_until <= clock():
            raise ApiError(503, 'search_reservation_lost')
        caller(db, principal, settings, clock, csrf)
        if row.lease_until <= clock():
            raise ApiError(503, 'search_reservation_lost')
    boundary.checkpoint(engine, 'presend', presend)
    return proof


def hit(identifier, row, downloads):
    return {'chunk_id': str(identifier), 'item_id': str(row['item_id']), 'version_id': str(row['version_id']),
        'source_file_id': str(row['file_id']), **{k: row[k] for k in ('title', 'text', 'coverage', 'locator_kind',
            'start_page', 'end_page', 'start_line', 'end_line', 'start_offset', 'end_offset', 'locator_detail',
            'section_kind', 'visibility')}, 'original_url': downloads.get(row['file_id'])}


def search(engine, principal, settings, query, mode, item, use_rerank, collection, *, clock, csrf,
           expected_scope=None, return_context=False):
    boundary.entry(engine)
    if len(query.encode('utf-8')) > settings.search.max_query_bytes:
        raise ApiError(422, 'search_query_too_long')
    if '\x00' in query:
        raise ApiError(422, 'search_query_invalid')
    scope = request_limit(engine, principal, settings, clock, csrf, item, collection, expected_scope)
    operation = uuid.uuid4()
    vector = None
    costs = {'embed_query': None, 'rerank': None}
    timings = {'keyword_ms': 0, 'vector_ms': 0, 'provider_ms': 0, 'rerank_ms': 0}
    if mode == 'hybrid' and scope[0]:
        if len(scope[0]) != 1 or next(iter(scope[0]))[0] != settings.embedding.model_id:
            raise ApiError(409, 'search_index_model_mismatch')
        dimension = next(iter(scope[0]))[1]
        if not settings.embedding.pricing_verified or settings.embedding.api_key is None:
            raise ApiError(503, 'search_embedding_unavailable')
        ceiling = s.money(settings.search.max_query_bytes, settings.embedding.max_usd_per_million_tokens)
        if ceiling > Decimal(str(settings.search.max_query_request_usd)):
            raise ApiError(503, 'search_embedding_ceiling_unverified')
        proof = admission(engine, principal, settings, clock, csrf, item, collection, scope, None, 'embed_query', ceiling, operation)
        error, result = None, None
        start = time.perf_counter()
        with accounting.heartbeat(engine, proof, settings, clock=clock) as failed:
            try:
                result = s.embed(query, settings, input_type='search_query')
            except s.EmbeddingError as exc:
                error = exc
        timings['provider_ms'] = int((time.perf_counter() - start) * 1000)
        if result is not None:
            if result.model_id != settings.embedding.model_id or len(result.vector) != dimension or not all(type(v) in (int, float) and math.isfinite(v) for v in result.vector):
                error = s.EmbeddingError('search_embedding_dimensions_mismatch')
            elif result.input_tokens is not None and (type(result.input_tokens) is not int or result.input_tokens < 0 or result.input_tokens > settings.search.max_query_bytes):
                error = s.EmbeddingError('search_embedding_usage_exceeds_reservation')
        costs['embed_query'] = accounting.settle(engine, proof, settings, tokens=result.input_tokens if result and not error else None,
            rejected=error.definitely_rejected if error else False,
            response_model_id=getattr(result or error, 'response_model_id', None),
            response_provider=getattr(result or error, 'response_provider', None), clock=clock)
        if failed.is_set():
            raise ApiError(503, 'search_heartbeat_unavailable')
        if error:
            raise ApiError(503, error.code)
        vector = result.vector
    def retrieve(db, _):
        ranking = s._rank(db, principal, item, settings, query, vector, collection, include_common=True)
        ids = s.fuse(ranking.keyword, ranking.semantic)[:settings.search.max_results]
        rows = s._hydrate_scope(db, principal, item, ids, collection, True)
        if set(ids) != set(rows):
            raise ApiError(409, 'search_scope_changed')
        locks(db, principal, settings, rows, item, collection)
        observe(db, principal, settings, clock, item, collection, scope, rows)
        return ranking, ids, rows
    ranking, ids, rows = boundary.checkpoint(engine, 'retrieval', retrieve)
    timings['keyword_ms'], timings['vector_ms'] = ranking.keyword_ms, ranking.vector_ms
    permitted = [i for i in ids if rows[i]['provider_ok']]
    withheld = len(ids) - len(permitted) if use_rerank else 0
    status = 'not_requested'
    if use_rerank and ids:
        status = 'degraded_not_permitted' if not permitted else 'degraded_unavailable'
        policy = settings.rerank
        if permitted and policy.pricing_verified and settings.embedding.api_key is not None:
            sent = permitted[:policy.max_candidates]
            documents = [rows[i]['text'].encode('utf-8')[:policy.max_text_bytes].decode('utf-8', 'ignore') for i in sent]
            bound = sum(len(d.encode('utf-8')) for d in documents) + len(sent)*len(query.encode('utf-8')) + 1024
            ceiling = s.money(bound, policy.max_usd_per_million_tokens)
            status = 'degraded_ceiling'
            if ceiling <= Decimal(str(policy.max_request_usd)):
                proof = None
                try:
                    proof = admission(engine, principal, settings, clock, csrf, item, collection, scope, rows, 'rerank', ceiling, operation)
                except ApiError as exc:
                    if exc.status_code != 429:
                        raise
                    status = 'degraded_concurrency' if exc.code == 'search_rerank_concurrency_limit' else 'degraded_budget'
                if proof:
                    error, ranked = None, None
                    start = time.perf_counter()
                    with accounting.heartbeat(engine, proof, settings, clock=clock) as failed:
                        try:
                            ranked = s.rerank(query, documents, settings)
                            results, tokens, units = ranked
                            ordered = s.rerank_order(sent, results)
                            if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= bound):
                                raise s.RerankError('rerank_usage_exceeds_reservation')
                            if units is not None and (type(units) is not int or units < 0):
                                raise s.RerankError('rerank_response_invalid')
                        except (ValueError, OverflowError):
                            error = s.RerankError('rerank_response_invalid')
                        except s.RerankError as exc:
                            error = exc
                    timings['rerank_ms'] = int((time.perf_counter() - start)*1000)
                    timings['provider_ms'] += timings['rerank_ms']
                    costs['rerank'] = accounting.settle(engine, proof, settings, tokens=tokens if not error else None,
                        units=units if not error else None, rejected=error.rejected if error else False,
                        response_model_id=getattr(ranked or error, 'response_model_id', None),
                        response_provider=getattr(ranked or error, 'response_provider', None), clock=clock)
                    if failed.is_set():
                        raise ApiError(503, 'search_heartbeat_unavailable')
                    if error:
                        status = 'degraded_' + error.code
                    else:
                        ids, status = ordered + [i for i in ids if i not in set(sent)], 'applied'
    def response(db, _):
        locks(db, principal, settings, rows, item, collection)
        _, fresh = observe(db, principal, settings, clock, item, collection, scope, rows)
        downloads = s.original_urls(db, principal, [fresh[i]['file_id'] for i in ids])
        caller(db, principal, settings, clock)
        return {'mode': mode, 'rerank_status': status, 'rerank_withheld': withheld, 'count': len(ids),
            'hits': [hit(i, fresh[i], downloads) for i in ids], 'candidate_count': len(ranking.keyword)+len(ranking.semantic),
            'keyword_query': ranking.keyword_query, 'timings': timings, 'cost': costs}
    found = boundary.checkpoint(engine, 'response', response)
    return (found, immutable(scope), immutable(rows)) if return_context else found


def open_chunk(engine, principal, settings, chunk, *, clock):
    boundary.entry(engine)
    def read(db, _):
        before = s.hydrate(db, principal, None, [chunk], include_common=True)
        locks(db, principal, settings, before, None, None)
        auth_safety.require_final(db)
        current = caller(db, principal, settings, clock)
        fresh = s.hydrate(db, current, None, [chunk], include_common=True)
        downloads = s.original_urls(db, current, [r['file_id'] for r in fresh.values()])
        caller(db, principal, settings, clock)
        if not fresh or fresh != before:
            raise ApiError(404, 'not_found')
        return hit(chunk, fresh[chunk], downloads)
    return boundary.checkpoint(engine, 'viewer', read)
