"""RAG content authority. Financial proofs never authorize evidence or response."""
import json
import uuid
from decimal import Decimal

from sqlalchemy import select, text

from app import rag, search_accounting as accounting, search_boundary as boundary
from app import search_runtime as retrieval
from app.api.errors import ApiError
from app.identity.predicates import lock_users
from app.ingest.checkpoints import frozen
from app.models import RagRequestDay, SearchUsage


def checkpoint(engine, phase, operation):
    return boundary.checkpoint(engine, phase, operation, domain='rag')


def release(db):
    try:
        boundary.release(db)
    except ApiError:
        raise ApiError(503, 'rag_release_unavailable') from None


def settle(engine, proof, settings, **usage):
    if proof.task_type != 'generation':
        raise ApiError(503, 'rag_reservation_lost')
    return accounting.settle(engine, proof, settings, **usage)


def observe(db, principal, settings, clock, item, collection, expected=None, before=None, csrf=None, writer=False):
    return retrieval.observe(db, principal, settings, clock, item, collection, expected, before, csrf, writer)


def retrieve(engine, principal, settings, query, mode, item, collection, *, clock, csrf, scope):
    """Internal retrieval interface: one search counter, no HTTP nesting/rerank."""
    return retrieval.search(engine, principal, settings, query, mode, item, False, collection,
        clock=clock, csrf=csrf, expected_scope=scope, return_context=True)


def content(engine, phase, principal, settings, clock, csrf, item, collection, scope=None, rows=None):
    def read(db, _):
        retrieval.locks(db, principal, settings, rows or {}, item, collection)
        return observe(db, principal, settings, clock, item, collection, scope, rows, csrf)
    return checkpoint(engine, phase, read)


def counter(engine, principal, settings, clock, csrf, item, collection, scope):
    def write(db, outcome):
        lock_users(db, [principal.user_id])
        retrieval.caller(db, principal, settings, clock, csrf)
        retrieval.locks(db, principal, settings, {}, item, collection)
        day = clock().date()
        db.execute(text('INSERT INTO rag_request_days(day,user_id) VALUES (:d,:u) ON CONFLICT DO NOTHING'),
            {'d': day, 'u': principal.user_id})
        row = db.scalar(select(RagRequestDay).where(RagRequestDay.day == day,
            RagRequestDay.user_id == principal.user_id).with_for_update())
        if row.request_count >= settings.generation.max_requests_per_user_day:
            observe(db, principal, settings, clock, item, collection, scope, csrf=csrf, writer=True)
            raise ApiError(429, 'rag_request_limit')
        row.request_count += 1
        db.flush()
        observe(db, principal, settings, clock, item, collection, scope, csrf=csrf, writer=True)
        if clock().date() != day:
            raise ApiError(503, 'rag_counter_day_changed')
        boundary.commit(db, outcome)
    checkpoint(engine, 'counter', write)


def admission(engine, principal, settings, clock, csrf, item, collection, scope, rows):
    policy = settings.generation
    ceiling = rag.money(policy.max_input_tokens_estimate, policy.input_usd_per_million_ceiling) + rag.money(
        policy.max_output_tokens, policy.max_output_usd_per_million_tokens)
    if ceiling > Decimal(str(policy.max_request_usd)):
        content(engine, 'unavailable', principal, settings, clock, csrf, item, collection, scope, rows)
        raise ApiError(503, 'rag_generation_ceiling_unverified')
    accounting.reap(engine, settings, clock=clock)
    def write(db, outcome):
        lock_users(db, [principal.user_id])
        retrieval.caller(db, principal, settings, clock, csrf)
        retrieval.locks(db, principal, settings, rows, item, collection)
        row = accounting.admit(db, principal, settings, 'generation', ceiling, uuid.uuid4(), clock)
        proof = frozen(row)
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        observe(db, principal, settings, clock, item, collection, scope, rows, csrf, writer=True)
        db.refresh(row)
        at = clock()
        if (not accounting.matches(row, proof) or row.status != 'reserved'
                or at >= row.lease_until or at.date() != row.day):
            raise ApiError(503, 'rag_reservation_lost')
        retrieval.caller(db, principal, settings, clock, csrf)
        boundary.commit(db, outcome)
        return proof
    proof = checkpoint(engine, 'reservation', write)
    # Separate transaction after known commit AND clean release. No admission
    # proof is reused as content authority, including for unobserved revocations.
    def presend(db, _):
        retrieval.locks(db, principal, settings, rows, item, collection)
        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update(read=True))
        observe(db, principal, settings, clock, item, collection, scope, rows, csrf)
        if (proof.task_type != 'generation' or proof.model_id != settings.generation.model_id
                or proof.provider != 'openrouter' or not accounting.matches(row, proof) or row.status != 'reserved'
                or row.lease_until is None or row.lease_until <= clock()):
            raise ApiError(503, 'rag_reservation_lost')
        retrieval.caller(db, principal, settings, clock, csrf)
        if row.lease_until <= clock():
            raise ApiError(503, 'rag_reservation_lost')
    checkpoint(engine, 'presend', presend)
    return proof


def answer(engine, principal, settings, item, question, mode, *, collection_id=None, all_items=False, clock, csrf=None):
    """Translate shared checkpoint failures once, without catching provider errors as success."""
    try:
        return _answer(engine, principal, settings, item, question, mode, collection_id, all_items, clock, csrf)
    except ApiError as exc:
        if exc.code in ('search_scope_changed', 'search_commit_uncertain', 'search_release_unavailable',
                'search_checkpoint_unavailable', 'search_reservation_lost', 'search_heartbeat_unavailable',
                'search_runtime_grants_missing', 'search_runtime_required', 'search_runtime_unavailable'):
            raise ApiError(exc.status_code, exc.code.replace('search_', 'rag_', 1)) from None
        raise
    except Exception:
        raise ApiError(503, 'rag_checkpoint_unavailable') from None


def _answer(engine, principal, settings, item, question, mode, collection, all_items, clock, csrf):
    boundary.entry(engine, domain='rag')
    policy = settings.generation
    if len(question.encode('utf-8')) > policy.max_question_bytes:
        raise ApiError(422, 'rag_question_too_long')
    if '\x00' in question:
        raise ApiError(422, 'rag_question_invalid')
    if sum((item is not None, collection is not None, all_items)) != 1:
        raise ApiError(422, 'rag_invalid_scope')
    scope, _ = content(engine, 'scope', principal, settings, clock, csrf, item, collection)
    scope = retrieval.immutable(scope)
    if not policy.pricing_verified or settings.embedding.api_key is None:
        content(engine, 'unavailable', principal, settings, clock, csrf, item, collection, scope)
        raise ApiError(503, 'rag_generation_unavailable_unverified_pricing')
    counter(engine, principal, settings, clock, csrf, item, collection, scope)
    comparing = rag.is_comparison(question) and item is None
    query = rag.COMPARISON_CUE.sub(' ', question).strip() if comparing and mode == 'keyword' else question
    found, _, rows = retrieve(engine, principal, settings, query or question, mode, item,
        collection, clock=clock, csrf=csrf, scope=scope)
    # Evaluation captures precisely this real internal retrieval interface.
    content(engine, 'pack', principal, settings, clock, csrf, item, collection, scope, rows)
    if (set(rows) != {uuid.UUID(hit['chunk_id']) for hit in found['hits']}
            or any(not rag._matches_hit(hit, retrieval.plain(rows[uuid.UUID(hit['chunk_id'])])) for hit in found['hits'])):
        raise ApiError(409, 'rag_scope_changed')
    permitted = [hit for hit in found['hits'] if rows[uuid.UUID(hit['chunk_id'])]['provider_ok']]
    withheld = len(found['hits']) - len(permitted)
    pack = rag._pack(permitted, settings, comparing=comparing, question=question)
    coverages = {row['coverage'] for row in pack.values()}
    coverage = 'partial' if 'partial' in coverages else next(iter(coverages), None)
    if coverage is None and item is not None and scope[1]:
        coverage = scope[1][0][14]
    label = ({'kind': 'item', 'item_id': str(item)} if item else
        {'kind': 'collection', 'collection_id': str(collection)} if collection else {'kind': 'all'})
    base = {'status': 'insufficient_evidence', 'scope': label, 'coverage': coverage, 'claims': [],
        'cost': None, 'evidence_withheld': withheld}

    def response(value, claims=None):
        def read(db, _):
            retrieval.locks(db, principal, settings, rows, item, collection)
            observe(db, principal, settings, clock, item, collection, scope, rows, csrf)
            if claims:
                downloads = rag.original_urls(db, principal, [r['file_id'] for r in rows.values()])
                for claim in claims:
                    for cite in claim['citations']:
                        cite['original_url'] = downloads.get(uuid.UUID(cite['source_file_id']))
            retrieval.caller(db, principal, settings, clock, csrf)
            return value
        return checkpoint(engine, 'response', read)

    if not pack:
        if mode == 'keyword' and found['keyword_query']['status'] == 'no_terms':
            base['reason'] = 'keyword_query_without_terms'
        elif comparing:
            base['reason'] = 'comparison_requires_multiple_papers'
        elif withheld and not permitted:
            base['reason'] = 'evidence_not_permitted_for_provider'
        return response(base)
    if comparing and len({r['item_id'] for r in pack.values()}) < 2:
        return response({**base, 'coverage': None, 'reason': 'comparison_requires_multiple_papers'})
    evidence = {key: rows[key] for key in pack}
    messages = rag._messages(question, evidence)
    if len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) > policy.max_input_tokens_estimate:
        raise ApiError(422, 'rag_evidence_limit')
    proof = admission(engine, principal, settings, clock, csrf, item, collection, scope, rows)
    result = error = None
    with accounting.heartbeat(engine, proof, settings, clock=clock) as failed:
        try:
            result = rag.generate(messages, settings)
        except rag.GenerationError as exc:
            error = exc
    cost = settle(engine, proof, settings,
        tokens=result.input_tokens if result else None, output_tokens=result.output_tokens if result else None,
        rejected=error.rejected if error else False,
        response_model_id=getattr(result or error, 'response_model_id', None),
        response_provider=getattr(result or error, 'response_provider', None), clock=clock)
    if failed.is_set():
        raise ApiError(503, 'rag_heartbeat_unavailable')
    if error:
        raise ApiError(503, error.code)
    if cost['status'] != 'usage_price_estimate':
        raise ApiError(503, 'rag_provider_usage_invalid')
    claims = rag._validate_answer(result.content, evidence, settings)
    incomplete = bool(comparing and claims and not any(c['comparison'] and rag.COMPARATIVE_ASSERTION.search(c['text'])
        and {v['item_id'] for v in c['citations']} == {r['item_id'] for r in pack.values()} for c in claims))
    if incomplete:
        claims = None
    value = ({'status': 'answered', 'scope': label, 'coverage': coverage, 'claims': claims, 'cost': cost,
        'evidence_withheld': withheld} if claims else {**base, 'cost': cost,
        **({'reason': 'comparison_requires_multiple_papers'} if incomplete else {})})
    return response(value, claims)
