"""Celery wakeup; the durable queue and retry clock live in PostgreSQL."""

from __future__ import annotations

import uuid

from celery import Celery

from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest.worker import due_job_ids, process_job
from app.discovery import run_sync
from app.citations import reconcile_pending
from app.db import get_engine

settings = get_settings()
celery_app = Celery("ras_ingest", broker=settings.celery_broker_url())
celery_app.conf.update(task_ignore_result=True, worker_prefetch_multiplier=1,
                       broker_connection_retry_on_startup=True,
                       beat_schedule={"discover-ingest-jobs": {
                           "task": "ras.ingest.discover", "schedule": settings.ingest.poll_seconds},
                                      "arxiv-metadata-discovery": {
                           "task": "ras.discovery.sync", "schedule": min(300, settings.discovery.poll_seconds)},
                                      "citation-link-reconcile": {
                           "task": "ras.citations.reconcile", "schedule": settings.citations.reconcile_poll_seconds}})


@celery_app.task(name="ras.ingest.discover")
def discover() -> int:
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
    return run_sync(get_engine(), settings.discovery)


@celery_app.task(name="ras.citations.reconcile")
def reconcile_citation_links() -> dict:
    return reconcile_pending(get_engine(), settings.citations)
