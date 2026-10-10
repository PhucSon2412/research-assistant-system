"""Document content checkpoints. Detached proofs are the only values crossing IO.

Content: User (private) -> item -> job -> resources -> capacity -> generation/
entries -> budgets/call -> flush -> journal exclusive -> current predicates/clock.
Financial maintenance lives separately in document_accounting; it never locks
an item or writes content. Neither path checks out a second connection.
"""
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_, select, text, update

from app import auth_safety
from app.ingest import checkpoints as cp, runtime
from app.ingest import indexer as ix
from app.ingest import worker as w
from app.models import (AccessRights, CommonPublication, IngestionJob, IngestStepAttempt,
    IndexGeneration, IndexEntry, Chunk, Section, PdfPage, UploadIntent, ProcessingAcknowledgement,
    EmbeddingCall, EmbeddingBudgetDay, EmbeddingUserBudgetDay, SearchUsage)
from app.provider_capacity import LOCK


def index_state(job):
    return job.state in ('awaiting_index', 'indexing') or (
        job.state == 'retryable_failed' and job.resume_state == 'indexing')


def configured(settings):
    if settings.embedding.api_key is None or not settings.embedding.api_key.get_secret_value():
        raise runtime.Halt('embedding_key_missing')
    if not settings.embedding.pricing_verified:
        raise runtime.Halt('embedding_pricing_unverified')


def binding(*rows):
    return cp.binding(*rows) + (getattr(rows[1], '_document_consent_binding', None),)


def consent(db, job, item, source):
    if job.origin != 'upload':
        return
    acknowledged = db.execute(select(UploadIntent, ProcessingAcknowledgement).join(ProcessingAcknowledgement,
        UploadIntent.acknowledgement_id == ProcessingAcknowledgement.id).where(
        UploadIntent.item_id == item.id, UploadIntent.user_id == item.owner_user_id,
        UploadIntent.used_at.is_not(None), UploadIntent.session_id == ProcessingAcknowledgement.session_id,
        UploadIntent.declared_size == source.byte_size,
        UploadIntent.declared_sha256 == source.checksum_sha256,
        UploadIntent.file_kind == ('pdf' if source.verified_mime == 'application/pdf' else 'markdown'),
        ProcessingAcknowledgement.actor_user_id == item.owner_user_id,
        ProcessingAcknowledgement.notice_version == UploadIntent.notice_version)).all()
    if len(acknowledged) != 1:
        raise runtime.Halt('upload_acknowledgement_missing')
    item._document_consent_binding = tuple(cp.frozen(row) for row in acknowledged[0])


def context(db, job, item, *, published=False):
    for row in (job, item):
        db.refresh(row)
    item, version, source = w._context(db, job, item)
    if version.coverage not in ('complete', 'partial'):
        raise w.StepError('index_rights_or_coverage_invalid')
    if published:
        if (job.state != 'ready' or job.finished_at is None or job.lease_token is not None
                or job.lease_until is not None or item.content_status != 'indexed'
                or item.active_index_generation_id is None or item.active_version_id != version.id):
            raise runtime.Halt('worker_binding_changed')
    elif item.content_status != 'metadata_only' or item.active_index_generation_id is not None:
        raise runtime.Halt('index_already_active')
    if job.origin == 'upload':
        if source.job_id != job.id or source.item_id != item.id or item.owner_user_id is None:
            raise runtime.Halt('worker_binding_changed')
        consent(db, job, item, source)
        if not db.scalar(text('SELECT ras_auth_status_live(:uid)'), {'uid': item.owner_user_id}):
            raise runtime.Halt('owner_status_paused')
    else:
        if source.id != job.source_file_id or item.owner_user_id is not None or job.scope_owner_user_id is not None:
            raise runtime.Halt('worker_binding_changed')
        rights = db.scalar(select(AccessRights).where(AccessRights.item_id == item.id)
            .execution_options(populate_existing=True))
        publication = db.scalar(select(CommonPublication).where(CommonPublication.item_id == item.id)
            .execution_options(populate_existing=True))
        item._worker_common_binding = (tuple(cp.frozen(rights).values.items()), tuple(cp.frozen(publication).values.items()))
    w._guard(job, item)
    return job, item, version, source


def resources(db, rows):
    """Acquire all resource/FK locks before the journal, in a stable order."""
    job, item, _, source = rows
    if job.origin == 'upload':
        intents = list(db.scalars(select(UploadIntent).where(UploadIntent.item_id == item.id)
            .order_by(UploadIntent.id).with_for_update(read=True)))
        ids = [r.acknowledgement_id for r in intents]
        list(db.scalars(select(ProcessingAcknowledgement).where(ProcessingAcknowledgement.id.in_(ids))
            .order_by(ProcessingAcknowledgement.id).with_for_update(read=True)))
    for model in (Section, PdfPage, Chunk):
        list(db.scalars(select(model).where(model.source_file_id == source.id).order_by(model.id)
            .with_for_update(read=True).execution_options(populate_existing=True)))


def leased(db, job_id, token):
    job, item = cp.lock_item_then_job(db, job_id)
    if job is None or job.lease_token != token or job.lease_until is None:
        raise runtime.Halt('lease_lost')
    rows = context(db, job, item)
    if job.state != 'indexing' or job.finished_at is not None or job.lease_until <= w.utcnow():
        raise runtime.Halt('lease_lost')
    return rows


def final(db, rows, expected, deadline, *, claimed=False, published=False, check=None):
    db.flush()
    db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
    job_state = cp.frozen(rows[0])
    item_state = cp.frozen(rows[1])
    try:
        auth_safety.require_final(db, writer=True)
    except Exception:
        raise runtime.Halt('recovery_gate_closed') from None
    with db.no_autoflush:
        fresh = context(db, rows[0], rows[1], published=published)
        if binding(*fresh) != expected or cp.frozen(fresh[0]) != job_state or cp.frozen(fresh[1]) != item_state:
            raise runtime.Halt('worker_binding_changed')
        if check:
            check()
        at = w.utcnow()  # after every predicate SQL, including caller-provided checks
        if deadline is not None and (at < deadline if claimed else at >= deadline):
            raise runtime.Halt('lease_lost')
        if fresh[0].lease_until is not None and fresh[0].lease_until <= at:
            raise runtime.Halt('lease_lost')


def claim(db, job_id, settings):
    configured(settings)
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'index_claim'
        job, item = cp.lock_item_then_job(db, job_id, skip=True)
        if job is None or job.finished_at is not None or not index_state(job):
            return None
        rows = context(db, job, item)
        resources(db, rows)
        old, at = job.lease_until, w.utcnow()
        if (old is not None and old > at) or (job.next_attempt_at is not None and job.next_attempt_at > at):
            return None
        expected = binding(*rows)
        if job.attempt_count >= settings.ingest.max_attempts:
            job.state, job.finished_at, job.last_error_code = 'terminal_failed', at, 'attempts_exhausted'
            job.lease_token = job.lease_until = None
            final(db, rows, expected, old, claimed=True)
            runtime.commit(db, outcome)
            return None
        db.execute(update(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
            IngestStepAttempt.outcome == 'running').values(outcome='retry', error_code='worker_lost', finished_at=at))
        job.state, job.resume_state = 'indexing', None
        job.lease_token = token = uuid.uuid4()
        job.lease_until = at + timedelta(seconds=settings.ingest.lease_seconds)
        job.attempt_count += 1
        job.started_at = job.started_at or at
        job.next_attempt_at, job.updated_at = None, at
        db.add(IngestStepAttempt(job_id=job.id, step='index', attempt_no=job.attempt_count, outcome='running'))
        final(db, rows, expected, old, claimed=True)
        runtime.commit(db, outcome)
        return token
    return runtime.short(db, write)


def due(db, settings):
    configured(settings)
    cp.entry(db)
    def read(_):
        ids = list(db.scalars(select(IngestionJob.id).where(IngestionJob.finished_at.is_(None),
            or_(IngestionJob.state.in_(('awaiting_index', 'indexing')),
                (IngestionJob.state == 'retryable_failed') & (IngestionJob.resume_state == 'indexing')))
            .order_by(IngestionJob.created_at, IngestionJob.id)))
        db.rollback()
        result = []
        for identifier in ids:
            job, item = cp.lock_item_then_job(db, identifier, skip=True)
            if job is not None:
                try:
                    context(db, job, item)
                    auth_safety.require_final(db)
                    context(db, job, item)
                    at = w.utcnow()
                    if (index_state(job) and job.finished_at is None
                            and (job.lease_until is None or job.lease_until <= at)
                            and (job.next_attempt_at is None or job.next_attempt_at <= at)):
                        result.append(identifier)
                except (w.StepError, runtime.Halt):
                    pass
            db.rollback()
            if len(result) >= settings.ingest.dispatch_batch:
                break
        return result
    return runtime.short(db, read)


def proof(db, job_id, token, settings, previous=None):
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'index_proof'
        rows = leased(db, job_id, token)
        expected, deadline = binding(*rows), rows[0].lease_until
        if previous is not None and expected != previous.binding:
            raise runtime.Halt('worker_binding_changed')
        rows[0].lease_until = w.utcnow() + timedelta(seconds=settings.ingest.lease_seconds)
        final(db, rows, expected, deadline)
        result = cp.SourceProof(cp.frozen(rows[3]), expected)
        runtime.commit(db, outcome)
        return result
    return runtime.short(db, write)


@dataclass(frozen=True)
class Admission:
    call: object = field(repr=False)
    chunk: object = field(repr=False)
    source: object = field(repr=False)
    generation_id: uuid.UUID
    send: bool = False


def _reserve(db, rows, chunk, settings):
    from decimal import Decimal
    job, item, _, source = rows
    # Caller owns item/job/resources and capacity. No reentrant maintenance here.
    day = w.utcnow().date()
    db.execute(text('INSERT INTO embedding_budget_days(day) VALUES (:d) ON CONFLICT DO NOTHING'), {'d': day})
    budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == day).with_for_update())
    if item.owner_user_id is not None:
        db.execute(text('INSERT INTO embedding_user_budget_days(day,user_id) VALUES (:d,:u) ON CONFLICT DO NOTHING'),
            {'d': day, 'u': item.owner_user_id})
    user = ix._user_budget(db, day, item.owner_user_id)
    call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.source_file_id == source.id,
        EmbeddingCall.model_id == settings.embedding.model_id, EmbeddingCall.pipeline_version == chunk.pipeline_version,
        EmbeddingCall.chunk_hash == chunk.chunk_hash).with_for_update().execution_options(populate_existing=True))
    if call is not None:
        if call.status == 'completed':
            if call.vector is None or call.content_removed_at is not None:
                raise w.StepError('embedding_content_unavailable')
            return call, False
        if call.status != 'rejected':
            raise w.StepError('embedding_outcome_uncertain')
    active = db.scalar(select(func.count()).select_from(EmbeddingCall).where(EmbeddingCall.status == 'reserved'))
    queries = db.scalar(select(func.count()).select_from(SearchUsage).where(SearchUsage.task_type == 'embed_query', SearchUsage.status == 'reserved'))
    if active + queries >= settings.embedding.max_concurrent_requests:
        raise w.StepError('embedding_concurrency_limit', retryable=True)
    amount = ix._amount(settings.embedding.max_request_bytes, settings)
    def spend(task=None, uid=None):
        q = select(func.coalesce(func.sum(func.coalesce(SearchUsage.cost_usd, SearchUsage.reserved_usd)), 0)).where(
            SearchUsage.day == day, SearchUsage.status.in_(('reserved', 'completed', 'uncertain')))
        if task: q = q.where(SearchUsage.task_type == task)
        if uid: q = q.where(SearchUsage.user_id == uid)
        return db.scalar(q)
    app_cap, cap, user_cap = (Decimal(str(v)) for v in (settings.budget.app_daily_usd,
        settings.budget.embedding_daily_usd, settings.budget.user_embedding_daily_usd))
    doc = budget.committed_usd + budget.reserved_usd
    query, personal = spend('embed_query'), spend('embed_query', item.owner_user_id) if user else Decimal(0)
    if doc + spend() + amount > app_cap or doc + query + amount > cap:
        raise w.StepError('embedding_budget_exhausted', retryable=True)
    if user and user.committed_usd + user.reserved_usd + personal + amount > user_cap:
        raise w.StepError('embedding_user_quota_exhausted', retryable=True)
    budget.reserved_usd += amount
    if user: user.reserved_usd += amount
    warnings = list(job.warnings)
    if doc + query + amount >= cap * Decimal(str(settings.budget.embedding_warning_fraction)):
        warnings.append('embedding_bucket_near_limit')
    if user and user.committed_usd + user.reserved_usd + personal >= user_cap * Decimal(str(settings.budget.user_embedding_warning_fraction)):
        warnings.append('embedding_user_quota_near_limit')
    job.warnings = list(dict.fromkeys(warnings))
    if call is None:
        call = EmbeddingCall(source_file_id=source.id, owner_user_id=item.owner_user_id,
            model_id=settings.embedding.model_id, provider=settings.embedding.provider,
            pipeline_version=chunk.pipeline_version, chunk_hash=chunk.chunk_hash)
        db.add(call)
    call.status, call.reserved_usd, call.reserved_day = 'reserved', amount, day
    call.cost_usd, call.cost_status = None, 'unknown'
    call.response_model_id = call.response_provider = call.finished_at = None
    call.reservation_token, call.reservation_job_id, call.reservation_job_token = uuid.uuid4(), job.id, job.lease_token
    call.lease_until = w.utcnow() + timedelta(seconds=settings.embedding.reservation_lease_seconds)
    db.flush()
    return call, True


def step(db, job_id, token, settings, source_proof, verified, *, publish=False):
    configured(settings)
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'publish' if publish else 'reservation'
        rows = leased(db, job_id, token)
        expected, deadline = binding(*rows), rows[0].lease_until
        if expected != source_proof.binding:
            raise runtime.Halt('worker_binding_changed')
        resources(db, rows)
        db.execute(text(LOCK))
        job, item, version, source = rows
        pipeline, chunks = ix._eligible_chunks(db, source, version, verified, settings)
        generation = db.scalar(select(IndexGeneration).where(IndexGeneration.job_id == job_id,
            IndexGeneration.model_id == settings.embedding.model_id, IndexGeneration.index_version == ix.index_version(job))
            .with_for_update().execution_options(populate_existing=True))
        if generation is None:
            generation = IndexGeneration(item_id=item.id, version_id=version.id, source_file_id=source.id,
                job_id=job.id, model_id=settings.embedding.model_id, provider=settings.embedding.provider,
                pipeline_version=pipeline, index_version=ix.index_version(job), expected_chunks=len(chunks),
                rights_epoch=item.rights_epoch, state='building')
            db.add(generation)
            db.flush()
        elif (generation.state != 'building' or generation.expected_chunks != len(chunks)
                or generation.pipeline_version != pipeline or generation.rights_epoch != item.rights_epoch
                or generation.item_id != item.id or generation.version_id != version.id or generation.source_file_id != source.id):
            raise w.StepError('index_generation_changed')
        entries = list(db.scalars(select(IndexEntry).where(IndexEntry.generation_id == generation.id)
            .order_by(IndexEntry.id).with_for_update()))
        done = {e.chunk_id for e in entries}
        pending = next((c for c in chunks if c.id not in done), None)
        result = None
        if publish:
            if pending is not None:
                raise w.StepError('index_incomplete')
            ix._publish(db, job, item, generation, chunks, settings)
        elif pending is not None:
            if len(pending.text.encode('utf-8')) > settings.embedding.max_request_bytes:
                raise w.StepError('embedding_request_too_large')
            call, send = _reserve(db, rows, pending, settings)
            if not send:
                ix._insert_entry(db, generation, pending, call)
            result = Admission(cp.frozen(call), cp.frozen(pending), source_proof, generation.id, send)
        generation_snapshot = cp.frozen(generation)
        chunks_snapshot = tuple(cp.frozen(c) for c in chunks)
        def check():
            db.refresh(generation)
            if cp.frozen(generation) != generation_snapshot:
                raise runtime.Halt('worker_binding_changed')
            if tuple(cp.frozen(c) for c in ix._eligible_chunks(db, source, version, verified, settings)[1]) != chunks_snapshot:
                raise runtime.Halt('worker_binding_changed')
            if result is not None and result.send:
                db.refresh(call)
                if cp.frozen(call) != result.call or call.lease_until <= w.utcnow():
                    raise runtime.Halt('reservation_lost')
        final(db, rows, expected, deadline, published=publish, check=check)
        runtime.commit(db, outcome)
        return result
    return runtime.short(db, write)


def pre_send(db, job_id, token, settings, admission, verified):
    """Reprove the exact durable reservation immediately before transport."""
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'pre_send'
        rows = leased(db, job_id, token)
        expected, deadline = binding(*rows), rows[0].lease_until
        if expected != admission.source.binding:
            raise runtime.Halt('worker_binding_changed')
        resources(db, rows)
        db.execute(text(LOCK))
        call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == admission.call.id).with_for_update())
        from app.ingest.document_accounting import matches
        if not matches(call, admission.call) or call.status != 'reserved':
            raise runtime.Halt('reservation_lost')
        chunks = ix._eligible_chunks(db, rows[3], rows[2], verified, settings)[1]
        if not any(cp.frozen(c) == admission.chunk for c in chunks):
            raise runtime.Halt('worker_binding_changed')
        old_call_deadline = call.lease_until
        rows[0].lease_until = w.utcnow() + timedelta(seconds=settings.ingest.lease_seconds)
        def check():
            db.refresh(call)
            if not matches(call, admission.call) or call.status != 'reserved' or old_call_deadline is None or old_call_deadline <= w.utcnow():
                raise runtime.Halt('reservation_lost')
        final(db, rows, expected, deadline, check=check)
        runtime.commit(db, outcome)
    runtime.short(db, write)


def store_result(db, job_id, token, settings, admission, result, verified):
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'entry'
        rows = leased(db, job_id, token)
        expected, deadline = binding(*rows), rows[0].lease_until
        if expected != admission.source.binding:
            raise runtime.Halt('worker_binding_changed')
        resources(db, rows)
        db.execute(text(LOCK))
        call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == admission.call.id).with_for_update())
        from app.ingest.document_accounting import matches
        if (not matches(call, admission.call) or call.status != 'completed' or call.content_removed_at is not None
                or call.chunk_hash != admission.chunk.chunk_hash):
            raise runtime.Halt('reservation_lost')
        chunks = ix._eligible_chunks(db, rows[3], rows[2], verified, settings)[1]
        chunk = next((c for c in chunks if cp.frozen(c) == admission.chunk), None)
        generation = db.scalar(select(IndexGeneration).where(IndexGeneration.id == admission.generation_id).with_for_update())
        if chunk is None or generation is None or generation.state != 'building' or generation.job_id != job_id:
            raise runtime.Halt('worker_binding_changed')
        if generation.dimensions is not None and generation.dimensions != len(result.vector):
            raise w.StepError('embedding_dimensions_mismatch')
        call.vector, call.dimensions = list(result.vector), len(result.vector)
        ix._insert_entry(db, generation, chunk, call)
        final(db, rows, expected, deadline)
        runtime.commit(db, outcome)
    runtime.short(db, write)


def fail(db, job_id, token, settings, error):
    cp.entry(db)
    def write(outcome):
        db.info['document_checkpoint'] = 'index_failure'
        rows = leased(db, job_id, token)
        job = rows[0]
        expected, deadline = binding(*rows), job.lease_until
        attempt = db.scalar(select(IngestStepAttempt).where(IngestStepAttempt.job_id == job_id,
            IngestStepAttempt.step == 'index', IngestStepAttempt.attempt_no == job.attempt_count).with_for_update())
        at = w.utcnow()
        attempt.outcome, attempt.finished_at, attempt.error_code = 'retry' if error.retryable else 'failed', at, error.code
        job.last_error_code, job.resume_state, job.updated_at = error.code, 'indexing', at
        job.lease_token = job.lease_until = None
        if error.retryable and job.attempt_count < settings.ingest.max_attempts:
            job.state = 'retryable_failed'
            job.next_attempt_at = at + timedelta(seconds=min(settings.ingest.retry_base_seconds * 2 ** (job.attempt_count-1), settings.ingest.retry_max_seconds))
        else:
            job.state, job.finished_at = 'terminal_failed', at
        final(db, rows, expected, deadline)
        runtime.commit(db, outcome)
    runtime.short(db, write)


def process_index(db, job_id, token, storage, settings):
    from app.ingest import document_accounting as money
    configured(settings)
    # Direct entry is guarded as well; no caller can resurrect the legacy path.
    source = proof(db, job_id, token, settings)
    try:
        verified = ix._verify_object(source, storage, settings)
        hashed_after_entry = True
        while True:
            admission = step(db, job_id, token, settings, source, verified)
            if admission is None:
                if not hashed_after_entry:
                    source = proof(db, job_id, token, settings, source)
                    verified = ix._verify_object(source, storage, settings)  # ALWAYS after the last entry, including reuse
                step(db, job_id, token, settings, source, verified, publish=True)
                return 'ready'
            if not admission.send:
                hashed_after_entry = False
                continue
            # This original was hashed since the preceding call. Reservation and
            # pre-send contain only DB work; no other IO occurs before transport.
            pre_send(db, job_id, token, settings, admission, verified)
            error = result = None
            try:
                with money.heartbeat(db.get_bind(), admission.call, settings):
                    result = ix.embed(admission.chunk.text, settings)
            except ix.EmbeddingError as exc:
                error = exc
            except BaseException:
                # Unknown transport/cancellation: preserve the durable ceiling.
                # No inference that the provider did not receive the request.
                raise runtime.Halt('provider_outcome_uncertain') from None
            if result is not None:
                error = money.invalid_result(result, settings)
                if error is not None:
                    result = None
            money.settle(db, admission.call, settings, result=result, error=error)
            if error is not None:
                raise w.StepError(error.code, retryable=error.definitely_rejected)
            ix._check_unchanged(source, storage, verified)  # early reject only
            store_result(db, job_id, token, settings, admission, result, verified)
            source = proof(db, job_id, token, settings, source)
            verified = ix._verify_object(source, storage, settings)
            hashed_after_entry = True
    except runtime.Halt:
        raise
    except w.StepError as exc:
        if exc.code in ('cancelled', 'lease_lost', 'rights_gate_missing', 'invalid_job_file_relation') or exc.code.startswith('common_'):
            raise
        fail(db, job_id, token, settings, exc)
        return exc.code
