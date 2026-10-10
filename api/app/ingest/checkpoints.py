"""C2d-5d2a extraction transactions; IO receives only detached immutable values."""
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType

from sqlalchemy import select, text, update

from app import auth_safety
from app.identity.predicates import lock_users
from app.ingest import runtime
from app.models import (AccessRights, CommonPublication, IngestionJob, IngestStepAttempt,
                        LibraryItem, PaperVersion, SourceFile, Section, PdfPage)


@dataclass(frozen=True)
class Record:
    values: object = field(repr=False)

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None


def frozen(row):
    def freeze(value):
        if isinstance(value, dict):
            return MappingProxyType({k: freeze(v) for k, v in value.items()})
        if isinstance(value, list):
            return tuple(freeze(v) for v in value)
        if type(value).__module__ == 'numpy' and hasattr(value, 'tolist'):
            return freeze(value.tolist())
        return value
    return Record(MappingProxyType({c.key: freeze(getattr(row, c.key)) for c in row.__table__.columns}))


@dataclass(frozen=True)
class SourceProof:
    source: Record = field(repr=False)
    binding: tuple = field(repr=False)
    sections: tuple = field(default=(), repr=False)
    pages: tuple = field(default=(), repr=False)

    def __getattr__(self, name):
        return getattr(self.source, name)


def _w():
    from app.ingest import worker
    return worker


def entry(db):
    def check(_):
        runtime.require_runtime(db)
        try:
            auth_safety.require_writer(db)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
    return runtime.short(db, check)


def lock_item_then_job(db, job_id, *, skip=False):
    w = _w()
    db.execute(text(f'SET LOCAL lock_timeout = {int(w.get_settings().recovery.gate_lock_timeout_ms)}'))
    candidate = db.execute(select(IngestionJob.item_id, IngestionJob.scope_owner_user_id,
        IngestionJob.origin).where(IngestionJob.id == job_id)).first()
    if candidate is None:
        return None, None
    if candidate.origin == 'upload' and candidate.scope_owner_user_id is not None:
        lock_users(db, [candidate.scope_owner_user_id])
    item = db.scalar(select(LibraryItem).where(LibraryItem.id == candidate.item_id)
        .with_for_update(skip_locked=skip).execution_options(populate_existing=True))
    if item is None:
        return None, None
    job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id)
        .with_for_update(skip_locked=skip).execution_options(populate_existing=True))
    if job is None:
        return None, item
    if (job.item_id, job.scope_owner_user_id, job.origin) != tuple(candidate):
        raise w.StepError('invalid_job_file_relation')
    if job.origin == 'promotion':
        for model in (AccessRights, CommonPublication):
            order = model.id if model is AccessRights else model.item_id
            list(db.scalars(select(model).where(model.item_id == item.id).order_by(order)
                .with_for_update(read=True).execution_options(populate_existing=True)))
    list(db.scalars(select(PaperVersion).where(PaperVersion.id == job.version_id)
        .with_for_update(read=True).execution_options(populate_existing=True)))
    list(db.scalars(select(SourceFile).where(
        SourceFile.id == job.source_file_id if job.origin == 'promotion' else SourceFile.job_id == job.id)
        .order_by(SourceFile.id).with_for_update(read=True).execution_options(populate_existing=True)))
    return job, item


def binding(job, item, version, source):
    return (job.id, job.origin, job.scope_visibility, job.scope_owner_user_id, job.item_id,
            job.version_id, job.source_file_id, job.rights_epoch_at_start, item.id,
            item.owner_user_id, item.visibility, item.rights_epoch, version.id, version.item_id,
            source.id, source.item_id, source.version_id, source.job_id, source.storage_key,
            source.checksum_sha256, source.byte_size, source.verified_mime, source.page_count,
            getattr(item, '_worker_common_binding', None))


def context(db, job, item):
    w = _w()
    # Refresh every scalar, including identity-map hits. No locks acquired here:
    # the caller already owns the full resource set before the journal boundary.
    for row in (job, item):
        if row is not None:
            db.refresh(row)
    item, version, source = w._context(db, job, item)
    if item.content_status != 'metadata_only' or item.active_index_generation_id is not None:
        raise runtime.Halt('index_pending')
    if job.origin == 'upload':
        live = db.scalar(text('SELECT ras_auth_status_live(:uid)'), {'uid': item.owner_user_id})
        if not live:
            raise runtime.Halt('owner_status_paused')
    else:
        rights = db.scalar(select(AccessRights).where(AccessRights.item_id == item.id)
            .execution_options(populate_existing=True))
        publication = db.scalar(select(CommonPublication).where(CommonPublication.item_id == item.id)
            .execution_options(populate_existing=True))
        item._worker_common_binding = (tuple(frozen(rights).values.items()), tuple(frozen(publication).values.items()))
    w._guard(job, item)
    return job, item, version, source


def leased(db, job_id, token):
    w = _w()
    job, item = lock_item_then_job(db, job_id)
    if job is None or job.lease_token != token or job.lease_until is None:
        raise runtime.Halt('lease_lost')
    rows = context(db, job, item)
    if job.finished_at is not None or job.state not in w.CLAIMABLE or job.state == 'retryable_failed':
        raise runtime.Halt('lease_lost')
    if job.lease_until <= w.utcnow():
        raise runtime.Halt('lease_lost')
    return rows


def _final(db, rows, expected, deadline, *, token=None, claimed=False):
    w = _w()
    db.flush()
    db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
    # These snapshots describe THIS transaction's completed mutation, not a
    # previous stage. Old binding/deadline remain separate from its new lease.
    state = tuple(getattr(rows[0], c.key) for c in IngestionJob.__table__.columns)
    try:
        auth_safety.require_final(db, writer=True)
    except Exception:
        raise runtime.Halt('recovery_gate_closed') from None
    with db.no_autoflush:
        fresh = context(db, rows[0], rows[1])
        current = tuple(getattr(fresh[0], c.key) for c in IngestionJob.__table__.columns)
        if binding(*fresh) != expected or state != current:
            raise runtime.Halt('worker_binding_changed')
        now = w.utcnow()  # after the LAST SQL predicate and any journal wait
        if deadline is not None and (now < deadline if claimed else now >= deadline):
            raise runtime.Halt('lease_lost')
        if token is not None and fresh[0].lease_token not in (None, token):
            raise runtime.Halt('lease_lost')
        if fresh[0].lease_until is not None and fresh[0].lease_until <= now:
            raise runtime.Halt('lease_lost')
    return now


def claim_job(db, job_id, settings, now=None):
    w = _w()
    db.info['worker_settings'] = settings
    entry(db)
    def write(outcome):
        job, item = lock_item_then_job(db, job_id, skip=True)
        if job is None or job.finished_at is not None or not w.extraction_state(job):
            return None
        rows = context(db, job, item)
        at = w.utcnow()
        if job.lease_until is not None and job.lease_until > at:
            return None
        if job.next_attempt_at is not None and job.next_attempt_at > at:
            return None
        old_deadline = job.lease_until
        expected = binding(*rows)
        if job.attempt_count >= settings.ingest.max_attempts:
            job.state = 'terminal_failed'
            job.last_error_code = 'attempts_exhausted'
            job.finished_at = at
            job.lease_token = job.lease_until = None
            _final(db, rows, expected, old_deadline, claimed=True)
            runtime.commit(db, outcome)
            return None
        db.execute(update(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
            IngestStepAttempt.outcome == 'running').values(outcome='retry', error_code='worker_lost', finished_at=at))
        if job.state == 'retryable_failed':
            job.state = job.resume_state or 'validating'
        if job.state == 'received':
            job.state = 'validating'
        job.attempt_count += 1
        job.started_at = job.started_at or at
        job.lease_token = token = uuid.uuid4()
        job.lease_until = at + timedelta(seconds=settings.ingest.lease_seconds)
        job.next_attempt_at = None
        job.updated_at = at
        _final(db, rows, expected, old_deadline, token=token, claimed=True)
        runtime.commit(db, outcome)
        return token
    return runtime.short(db, write)


def begin(db, job_id, token, step):
    w = _w()
    entry(db)
    def write(outcome):
        rows = leased(db, job_id, token)
        job, _, _, source = rows
        if step not in ('validate', 'extract', 'chunk') or w.stage(job.state) != step:
            raise runtime.Halt('index_pending')
        expected, deadline = binding(*rows), job.lease_until
        sections = pages = ()
        if step == 'chunk':
            pipeline = w.pdf.PIPELINE_VERSION if source.verified_mime == 'application/pdf' else w.PIPELINE_VERSION
            sections = tuple(frozen(s) for s in w._rows(db, Section, source, pipeline, Section.ordinal))
            pages = tuple(frozen(p) for p in w._rows(db, PdfPage, source, pipeline, PdfPage.page_number))
        job.state = w.STEPS[step]
        job.updated_at = w.utcnow()
        job.lease_until = job.updated_at + timedelta(seconds=db.info.get('worker_settings', w.get_settings()).ingest.lease_seconds)
        db.add(IngestStepAttempt(job_id=job.id, step=step, attempt_no=job.attempt_count, outcome='running'))
        _final(db, rows, expected, deadline, token=token)
        proof = SourceProof(frozen(source), expected, sections, pages)
        runtime.commit(db, outcome)
        return proof
    return runtime.short(db, write)


def io_check(db, job_id, token, proof):
    """A fresh read boundary between storage and parsing/chunk computation."""
    entry(db)
    def read(_):
        rows = leased(db, job_id, token)
        try:
            auth_safety.require_final(db)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
        rows = context(db, rows[0], rows[1])
        if binding(*rows) != proof.binding:
            raise runtime.Halt('worker_binding_changed')
        if rows[0].lease_token != token or rows[0].lease_until <= _w().utcnow():
            raise runtime.Halt('lease_lost')
    return runtime.short(db, read)


def finish(db, job_id, token, step, save):
    w = _w()
    entry(db)
    def write(outcome):
        rows = leased(db, job_id, token)
        job, item, version, source = rows
        if job.state != w.STEPS[step]:
            raise runtime.Halt('worker_binding_changed')
        expected = binding(*rows)
        proof = db.info.get('worker_source_proof')
        if proof is None or proof.binding != expected:
            raise runtime.Halt('worker_binding_changed')
        deadline = job.lease_until
        new_state = save(db, job, item, version, source)
        attempt = db.scalar(select(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
            IngestStepAttempt.step == step, IngestStepAttempt.attempt_no == job.attempt_count))
        if attempt is None or attempt.outcome != 'running':
            raise runtime.Halt('worker_binding_changed')
        at = w.utcnow()
        attempt.outcome, attempt.finished_at = 'success', at
        job.state, job.resume_state, job.updated_at = new_state, None, at
        if new_state not in ('fulltext_rejected', 'needs_review'):
            job.last_error_code = None
        if new_state == 'awaiting_index':
            job.extraction_finished_at = at
        if new_state in w.WORKER_STOP:
            if new_state in w.TERMINAL:
                job.finished_at = at
            job.lease_token = job.lease_until = None
        else:
            job.lease_until = at + timedelta(seconds=db.info.get('worker_settings', w.get_settings()).ingest.lease_seconds)
        _final(db, rows, expected, deadline, token=token)
        runtime.commit(db, outcome)
        return new_state
    return runtime.short(db, write)


def fail(db, job_id, token, step, exc, settings):
    w = _w()
    entry(db)
    def write(outcome):
        rows = leased(db, job_id, token)
        job, _, version, _ = rows
        expected, deadline = binding(*rows), job.lease_until
        proof = db.info.get('worker_source_proof')
        if proof is None or proof.binding != expected or job.state != w.STEPS[step]:
            raise runtime.Halt('worker_binding_changed')
        at = w.utcnow()
        attempt = db.scalar(select(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
            IngestStepAttempt.step == step, IngestStepAttempt.attempt_no == job.attempt_count))
        if attempt is not None and attempt.outcome == 'running':
            attempt.outcome = 'retry' if exc.retryable else 'failed'
            attempt.error_code, attempt.finished_at = exc.code, at
        job.last_error_code, job.resume_state = exc.code, w.STEPS[step]
        job.lease_token = job.lease_until = None
        job.updated_at = at
        if exc.code == 'no_body_text':
            job.state, job.finished_at, version.coverage = 'fulltext_rejected', at, 'none'
        elif exc.retryable and job.attempt_count < settings.ingest.max_attempts:
            job.state = 'retryable_failed'
            delay = min(settings.ingest.retry_base_seconds * 2 ** (job.attempt_count - 1), settings.ingest.retry_max_seconds)
            job.next_attempt_at = at + timedelta(seconds=delay)
        else:
            job.state, job.finished_at = 'terminal_failed', at
        _final(db, rows, expected, deadline, token=token)
        runtime.commit(db, outcome)
    return runtime.short(db, write)
