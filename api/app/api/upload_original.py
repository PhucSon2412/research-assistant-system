"""Original acceptance: short proofs, unlocked staging, atomic consume, fresh receipt.

The shielded attempt owns its slot/temp file until every thread has settled.
Cancellation signals it to stop before acceptance; it never closes resources
under a running thread or treats an in-flight commit as a rollback.
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app import auth_safety
from app.api import upload_intents
from app.api.errors import ApiError
from app.audit import record_event
from app.authz import m2a_private_derivatives_clause
from app.db import get_engine
from app.config import get_settings
from app.identity.predicates import lock_users
from app.identity.tokens import hash_token, plausible_token
from app.models import LibraryItem, PaperVersion, SourceFile, IngestionJob
from app.models import UploadIntent as Intent, ProcessingAcknowledgement as Ack
from app.models.enums import AuditActorType, AuditOutcome


class UploadSlots:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0

    def acquire(self, maximum):
        with self.lock:
            if self.active >= maximum:
                raise ApiError(503, "upload_busy")
            self.active += 1

    def release(self):
        with self.lock:
            self.active -= 1


slots = UploadSlots()  # separate from verified downloads; per API process
_attempts: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Accepted:
    item_id: uuid.UUID
    version_id: uuid.UUID
    file_id: uuid.UUID
    job_id: uuid.UUID
    used_at: datetime


def _lock_timeout(db, settings):
    db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))


def _notice(expected):
    # A notice configuration reload during I/O must invalidate the old consent.
    # This does not introduce hot-reload machinery or an auth/consent journal.
    if expected.notice_version != get_settings().upload_notice_version:
        raise ApiError(409, "notice_changed")


def _locked_binding(db, principal, digest, settings, *, writer=False):
    row = db.scalar(select(Intent).where(Intent.token_hash == digest,
        Intent.user_id == principal.user_id, Intent.session_id == principal.session_id,
        Intent.used_at.is_(None), Intent.item_id.is_(None))
        .with_for_update(read=not writer))
    if row is None:
        raise ApiError(404, "invalid_intent")
    db.execute(select(Ack.id).where(Ack.id == row.acknowledgement_id).with_for_update(read=True)).all()
    return upload_intents.Binding(row.id, row.acknowledgement_id, row.user_id, row.session_id,
        row.notice_version, row.file_kind, row.filename, row.declared_size, row.declared_sha256,
        row.token_hash, row.created_at, row.expires_at)


def proof(principal, digest, csrf, clock, settings, expected=None):
    # Own the Session inside this thread. Never share the dependency Session
    # with a thread that may outlive its HTTP request.
    with Session(get_engine()) as db:
        try:
            _lock_timeout(db, settings)
            binding = _locked_binding(db, principal, digest, settings)
            auth_safety.require_final(db)
            upload_intents._check(db, expected or binding, principal, csrf, clock, settings)
            _notice(expected or binding)
            return binding
        except ApiError as exc:
            if expected is None and exc.code in ("upload_intent_expired", "notice_changed"):
                raise ApiError(404, "invalid_intent") from None
            raise
        except Exception:
            raise ApiError(503, "upload_unavailable") from None
        finally:
            db.rollback()


def _consumed(db, expected, accepted, settings):
    # Keep the unused-only C2d-4a helper unchanged. This separate boundary
    # permits only the exact consume made by THIS acceptance transaction.
    row = db.execute(select(
        Intent.id, Intent.acknowledgement_id, Intent.user_id, Intent.session_id,
        Intent.notice_version, Intent.file_kind, Intent.filename, Intent.declared_size,
        Intent.declared_sha256, Intent.token_hash, Intent.created_at, Intent.expires_at,
        Intent.used_at, Intent.item_id, Ack.actor_user_id, Ack.session_id.label("ack_session"),
        Ack.notice_version.label("ack_notice"), Ack.acknowledged_at,
    ).join(Ack, Ack.id == Intent.acknowledgement_id).where(Intent.id == expected.intent_id)).one_or_none()
    if (row is None or tuple(row[:12]) != tuple(vars(expected).values())
            or (row.used_at, row.item_id) != (accepted.used_at, accepted.item_id)
            or (row.actor_user_id, row.ack_session, row.ack_notice, row.acknowledged_at) != (
                expected.user_id, expected.session_id, expected.notice_version, expected.created_at)):
        raise ApiError(409, "upload_intent_changed")
    if row.notice_version != settings.upload_notice_version:
        raise ApiError(409, "notice_changed")
    _notice(expected)


def accept(principal, expected, csrf, clock, settings, key, mime, pages, stopped):
    commit_started = False
    with Session(get_engine()) as db:
        try:
            auth_safety.require_writer(db)
            _lock_timeout(db, settings)
            lock_users(db, [principal.user_id])
            upload_intents._user(db, principal, csrf, clock, settings)
            binding = _locked_binding(db, principal, expected.token_hash, settings, writer=True)
            upload_intents._check(db, expected, principal, csrf, clock, settings)
            if binding != expected:
                raise ApiError(409, "upload_intent_changed")
            if stopped.is_set():
                raise ApiError(503, "upload_unavailable")
            item = LibraryItem(visibility="private", owner_user_id=principal.user_id,
                created_by_user_id=principal.user_id, title=Path(expected.filename).stem)
            db.add(item); db.flush()
            version = PaperVersion(item_id=item.id)
            db.add(version); db.flush()
            job = IngestionJob(origin="upload", actor_user_id=principal.user_id,
                scope_visibility="private", scope_owner_user_id=principal.user_id,
                item_id=item.id, version_id=version.id, idempotency_key=str(expected.intent_id),
                state="received", rights_epoch_at_start=0)
            db.add(job); db.flush()
            source = SourceFile(item_id=item.id, version_id=version.id, job_id=job.id,
                storage_key=key, verified_mime=mime, byte_size=expected.declared_size,
                page_count=pages, checksum_sha256=expected.declared_sha256,
                original_filename=expected.filename, file_source="upload")
            db.add(source)
            used_at = clock()
            db.query(Intent).filter(Intent.id == expected.intent_id).update(
                {Intent.used_at: used_at, Intent.item_id: item.id}, synchronize_session=False)
            record_event(db, action="upload.original.receive", outcome=AuditOutcome.SUCCESS,
                actor_type=AuditActorType.USER, actor_user_id=principal.user_id,
                target_type="library_item", target_id=str(item.id))
            db.flush()  # every DML / FK before exclusive journal
            accepted = Accepted(item.id, version.id, source.id, job.id, used_at)
            auth_safety.require_final(db, writer=True)
            db.expire_all()
            with db.no_autoflush:
                _consumed(db, expected, accepted, settings)
                upload_intents._user(db, principal, csrf, clock, settings)
                _notice(expected)
                if clock() >= expected.expires_at:
                    raise ApiError(409, "upload_intent_expired")
            if stopped.is_set():
                raise ApiError(503, "upload_unavailable")
            commit_started = True
            db.commit()
            return accepted
        except BaseException as exc:
            if commit_started:
                raise ApiError(503, "upload_outcome_uncertain") from None
            if isinstance(exc, ApiError):
                raise
            raise ApiError(503, "upload_unavailable") from None
        finally:
            # Local rollback does not establish the remote commit outcome.
            try:
                db.rollback()
            except Exception:
                db.close()


def receipt(principal, expected, accepted, csrf, clock, settings, key, mime, pages):
    with Session(get_engine()) as db:
        try:
            _lock_timeout(db, settings)
            # Worker order: item FIRST, then job. Other rows before journal.
            item = db.scalar(select(LibraryItem.id).where(LibraryItem.id == accepted.item_id,
                m2a_private_derivatives_clause(principal)).with_for_update(read=True))
            if item is None:
                raise ApiError(409, "upload_intent_changed")
            for model, ident in ((IngestionJob, accepted.job_id), (Intent, expected.intent_id),
                    (Ack, expected.acknowledgement_id), (PaperVersion, accepted.version_id),
                    (SourceFile, accepted.file_id)):
                db.execute(select(model.id).where(model.id == ident).with_for_update(read=True)).all()
            auth_safety.require_final(db)
            _consumed(db, expected, accepted, settings)
            found = db.execute(select(SourceFile.id).join(PaperVersion,
                (SourceFile.version_id == PaperVersion.id) & (SourceFile.item_id == PaperVersion.item_id))
                .join(LibraryItem, LibraryItem.id == PaperVersion.item_id)
                .join(IngestionJob, (IngestionJob.id == SourceFile.job_id) &
                    (IngestionJob.item_id == LibraryItem.id) & (IngestionJob.version_id == PaperVersion.id))
                .where(m2a_private_derivatives_clause(principal), LibraryItem.id == accepted.item_id,
                    PaperVersion.id == accepted.version_id, SourceFile.id == accepted.file_id,
                    SourceFile.storage_key == key, SourceFile.checksum_sha256 == expected.declared_sha256,
                    SourceFile.byte_size == expected.declared_size, SourceFile.verified_mime == mime,
                    SourceFile.page_count == pages, SourceFile.original_filename == expected.filename,
                    SourceFile.file_source == "upload", IngestionJob.id == accepted.job_id,
                    IngestionJob.origin == "upload", IngestionJob.actor_user_id == principal.user_id,
                    IngestionJob.scope_visibility == "private", IngestionJob.scope_owner_user_id == principal.user_id,
                    IngestionJob.idempotency_key == str(expected.intent_id))).one_or_none()
            if found is None:
                raise ApiError(409, "upload_intent_changed")
            upload_intents._user(db, principal, csrf, clock, settings)  # clock AFTER all SQL
            _notice(expected)
            # Intent expiry is a consume boundary, not a second consumption.
            return {"item_id": str(accepted.item_id), "version_id": str(accepted.version_id),
                "file_id": str(accepted.file_id), "job_id": str(accepted.job_id), "state": "received"}
        except ApiError:
            raise
        except Exception:
            raise ApiError(503, "upload_response_unavailable") from None
        finally:
            db.rollback()


def cleanup(storage, key):
    # Only called BEFORE commit or after a known rollback, and once I/O settled.
    # Close the read transaction/connection BEFORE S3 delete.
    try:
        with Session(get_engine()) as db:
            referenced = db.scalar(select(SourceFile.id).where(SourceFile.storage_key == key))
        if referenced is None:
            storage.delete(key)
    except Exception:
        pass  # DB / storage unreachable: keep orphan for operator reconcile


def store(storage, key, path, mime):
    with open(path, "rb") as file:
        storage.put(key, file, mime)


def verify(storage, key, expected):
    body = storage.open(key)
    try:
        size = 0
        digest = hashlib.sha256()
        for chunk in body.iter_chunks(chunk_size=64 * 1024):
            size += len(chunk)
            if size > expected.declared_size:
                raise ApiError(409, "upload_integrity_failed")
            digest.update(chunk)
        if size != expected.declared_size or digest.hexdigest() != expected.declared_sha256:
            raise ApiError(409, "upload_integrity_failed")
    finally:
        body.close()


async def _attempt(request, principal, digest, csrf, clock, settings, storage, validate, stopped):
    slots.acquire(settings.files.upload_max_concurrent)
    key = f"originals/{uuid.uuid4().hex}"
    put_started = keep = accepting = False
    directory = None
    try:
        expected = await _thread(stopped, proof, principal, digest, csrf, clock, settings)
        limit = settings.files.pdf_max_bytes if expected.file_kind == "pdf" else settings.files.markdown_max_bytes
        if request.headers.get("content-type", "").split(";")[0] not in (
                "application/octet-stream", "application/pdf", "text/markdown"):
            raise ApiError(422, "invalid_file_type")
        if request.headers.get("content-length"):
            try:
                length = int(request.headers["content-length"])
            except ValueError:
                raise ApiError(422, "invalid_request") from None
            if length < 0:
                raise ApiError(422, "invalid_request")
            if length > min(limit, expected.declared_size):
                raise ApiError(413, "file_too_large")
        directory = await _thread(stopped, tempfile.TemporaryDirectory, prefix="ras-upload-")
        path = str(Path(directory.name) / "original")
        file = await _thread(stopped, open, path, "wb")
        try:
            size = 0
            digest_bytes = hashlib.sha256()
            async with asyncio.timeout(settings.files.upload_body_timeout_seconds):
                async for chunk in request.stream():
                    if stopped.is_set():
                        raise ApiError(503, "upload_unavailable")
                    size += len(chunk)
                    if size > min(limit, expected.declared_size):
                        raise ApiError(413, "file_too_large")
                    digest_bytes.update(chunk)
                    await _thread(stopped, file.write, chunk)
        finally:
            await _thread(stopped, file.close)
        if size != expected.declared_size or digest_bytes.hexdigest() != expected.declared_sha256:
            raise ApiError(422, "file_mismatch")
        mime, pages = await _thread(stopped, validate, path, expected.file_kind, settings)
        await _thread(stopped, proof, principal, digest, csrf, clock, settings, expected)
        if stopped.is_set() or await request.is_disconnected():
            raise ApiError(503, "upload_unavailable")
        put_started = True  # even a failing put can have left an object
        await _thread(stopped, store, storage, key, path, mime)
        await _thread(stopped, verify, storage, key, expected)
        if stopped.is_set() or await request.is_disconnected():
            raise ApiError(503, "upload_unavailable")
        try:
            accepting = True
            accepted = await _thread(stopped, accept, principal, expected, csrf, clock,
                settings, key, mime, pages, stopped)
        except ApiError as exc:
            if exc.code == "upload_outcome_uncertain":
                keep = True
            raise
        keep = True  # known commit; no postcheck/cancellation can delete it
        return await _thread(stopped, receipt, principal, expected, accepted, csrf,
            clock, settings, key, mime, pages)
    except TimeoutError:
        raise ApiError(408, "upload_body_timeout") from None
    except asyncio.CancelledError:
        # Even direct attempt cancellation must settle threads before cleanup.
        # An acceptance thread may have committed; keep its object conservatively.
        keep = keep or accepting
        raise
    except ApiError:
        raise
    except (Exception, ClientDisconnect):
        raise ApiError(503, "upload_unavailable") from None
    finally:
        try:
            if put_started and not keep:
                await _thread(stopped, cleanup, storage, key)
        finally:
            try:
                if directory is not None:
                    await _thread(stopped, directory.cleanup)
            finally:
                slots.release()


async def receive(request, principal, clock, settings, storage, validate):
    raw = request.headers.get("x-upload-intent")
    if not plausible_token(raw):
        raise ApiError(404, "invalid_intent")
    stopped = threading.Event()
    task = asyncio.create_task(_attempt(request, principal, hash_token(raw),
        request.headers.get("x-csrf-token") or "", clock, settings, storage, validate, stopped))
    _attempts.add(task)
    def settled(done):
        _attempts.discard(done)
        if not done.cancelled():
            done.exception()  # consume detached failures without logging secrets
    task.add_done_callback(settled)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        # Attempt retains slot/temp until its threads settle, with no DTO or
        # mutation retry. An already-started commit is allowed to settle.
        raise


async def _thread(stopped, function, *args, **kwargs):
    """A cancelled coroutine cannot destroy resources still used by its thread."""
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        stopped.set()
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not work.cancelled():
            if work.exception() is None:
                result = work.result()
                # Allocation completed just as its awaiting owner was cancelled.
                close = getattr(result, "cleanup", None) or getattr(result, "close", None)
                if close is not None:
                    await _thread(stopped, close)
        raise
