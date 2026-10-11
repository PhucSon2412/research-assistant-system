"""Celery wakeup; the durable queue and retry clock live in PostgreSQL.

Every task first checks the recovery gate (M6c-1) and does nothing while it is
closed: no ingest, discovery, citation reconcile or cleanup runs against a
database that may be a restored, stale copy. ``recover`` runs its own operator
cleanup; ``ras.revocation.cleanup`` uses the runtime boundary ``app.ingest.cleanup`` and
``ras.citations.reconcile`` the batch boundary ``app.citations.reconcile_pending``.
"""

from __future__ import annotations

import uuid

from celery import Celery
from celery.signals import beat_init, worker_init, worker_process_init

from app import recovery
from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest.worker import due_job_ids, process_job, dispatchable
from app.ingest import runtime
from app.discovery import run_sync
from app.citations import reconcile_pending
from app.db import get_engine
from app.ingest.cleanup import cleanup_revoked
from app.storage import get_storage

try:
    settings = get_settings()
    if settings.db is None:
        raise ValueError('worker connection parts required')
    broker = settings.celery_broker_url()
except Exception:
    raise SystemExit('worker_runtime_configuration_required') from None
celery_app = Celery("ras_ingest", broker=broker)
celery_app.conf.update(task_ignore_result=True, worker_prefetch_multiplier=1,
                       broker_connection_retry_on_startup=True,
                       beat_schedule={"discover-ingest-jobs": {
                           "task": "ras.ingest.discover", "schedule": settings.ingest.poll_seconds},
                                      "arxiv-metadata-discovery": {
                           "task": "ras.discovery.sync", "schedule": min(300, settings.discovery.poll_seconds)},
                                      "citation-link-reconcile": {
                           "task": "ras.citations.reconcile", "schedule": settings.citations.reconcile_poll_seconds},
                                      "revoked-copy-cleanup": {
                           "task": "ras.revocation.cleanup", "schedule": settings.retention.cleanup_poll_seconds}})


@beat_init.connect
@worker_init.connect
@worker_process_init.connect
def require_runtime_startup(**kwargs):
    # Provisioning belongs to setup. Default dev credentials are deliberately
    # NOT changed here; an owner-configured process fails before business tasks.
    if settings.db is None:
        raise SystemExit('worker_runtime_configuration_required')
    try:
        if kwargs.get('signal') is worker_process_init:
            get_engine().dispose(close=False)  # never reuse parent DB sockets after fork
        db = get_sessionmaker()()
        runtime.short(db, lambda _: runtime.require_runtime(db))
    except Exception:
        raise SystemExit('worker_runtime_required') from None


def _gate(task: str) -> bool:
    db = get_sessionmaker()()
    try:
        runtime.short(db, lambda _: runtime.require_runtime(db))
        return recovery.gate_open(settings, get_engine(), task)
    except Exception:
        return False


@celery_app.task(name="ras.ingest.discover")
def discover() -> int:
    if not _gate("ras.ingest.discover"):
        return 0
    try:
        ids = due_job_ids(get_sessionmaker()(), settings)
    except Exception:
        return 0
    sent = 0
    for job_id in ids:
        if not dispatchable(job_id, settings):
            break
        try:
            ingest_job.delay(str(job_id))
        except Exception:
            break  # durable job unchanged; next tick retries after broker recovery
        sent += 1
    return sent


@celery_app.task(name="ras.ingest.process")
def ingest_job(job_id: str) -> str:
    try:
        identifier = uuid.UUID(job_id)
    except (ValueError, TypeError, AttributeError):
        return 'invalid_job_id'
    return process_job(identifier, settings)


@celery_app.task(name="ras.discovery.sync")
def sync_arxiv() -> dict:
    # Boundary checks identity/grants/gate at every checkpoint; disabled does no DB work.
    return run_sync(get_engine(), settings.discovery)


@celery_app.task(name="ras.citations.reconcile")
def reconcile_citation_links() -> dict:
    if not _gate("ras.citations.reconcile"):
        return {"skipped": "recovery_gate_closed"}
    # Runtime boundary: every batch rechecks login/grants/gate and ends in the journal lock.
    return reconcile_pending(get_engine(), settings.citations)


@celery_app.task(name="ras.revocation.cleanup")
def cleanup_revoked_copies() -> dict:
    # Tombstones in PostgreSQL are the durable queue; a missed run is retried next time.
    if not _gate("ras.revocation.cleanup"):
        return {"skipped": "recovery_gate_closed"}
    # Runtime boundary: each checkpoint rechecks login/gate; S3 runs outside DB transactions.
    return cleanup_revoked(get_engine(), get_storage(settings), settings)
