"""Celery wakeup; the durable queue and retry clock live in PostgreSQL.

Every task first checks the recovery gate (M6c-1) and does nothing while it is
closed: no ingest, discovery, citation reconcile or cleanup runs against a
database that may be a restored, stale copy. ``recover`` runs cleanup itself.
"""

from __future__ import annotations

import uuid

from celery import Celery

from app import recovery
from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest.worker import due_job_ids, process_job
from app.discovery import run_sync
from app.citations import reconcile_pending
from app.clock import utcnow
from app.db import get_engine
from app.publication import cleanup_revoked
from app.storage import get_storage

settings = get_settings()
celery_app = Celery("ras_ingest", broker=settings.celery_broker_url())
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


def _gate(task: str) -> bool:
    return recovery.gate_open(settings, get_engine(), task)


@celery_app.task(name="ras.ingest.discover")
def discover() -> int:
    if not _gate("ras.ingest.discover"):
        return 0
    with get_sessionmaker()() as db:
        ids = due_job_ids(db, settings)
    for job_id in ids:
        ingest_job.delay(str(job_id))
    return len(ids)


@celery_app.task(name="ras.ingest.process")
def ingest_job(job_id: str) -> str:
    return process_job(uuid.UUID(job_id), settings)


@celery_app.task(name="ras.discovery.sync")
def sync_arxiv() -> dict:
    if not _gate("ras.discovery.sync"):
        return {"skipped": "recovery_gate_closed"}
    return run_sync(get_engine(), settings.discovery)


@celery_app.task(name="ras.citations.reconcile")
def reconcile_citation_links() -> dict:
    if not _gate("ras.citations.reconcile"):
        return {"skipped": "recovery_gate_closed"}
    return reconcile_pending(get_engine(), settings.citations)


@celery_app.task(name="ras.revocation.cleanup")
def cleanup_revoked_copies() -> dict:
    # Tombstones in PostgreSQL are the durable queue; a missed run is retried next time.
    if not _gate("ras.revocation.cleanup"):
        return {"skipped": "recovery_gate_closed"}
    return cleanup_revoked(get_engine(), get_storage(settings), utcnow(), settings.retention.cleanup_batch_size)
