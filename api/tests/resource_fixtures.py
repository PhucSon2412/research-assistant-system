"""Resource contract fixtures with real runtime accounts/receipts, no raw auth seed."""
import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.authz import Principal
from app.identity import invitations
from app.models import (Chunk, LibraryItem, MetadataCandidate, PaperVersion, PdfPage,
                        ReferenceEntry, ReviewRequest, Section, SourceFile)
from tests.helpers import (Harness, Browser, FakeClock, USER_PASSWORD, WEB_ORIGIN, DEV_ENV)
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as resource_world, issue
from tests.test_auth_safety_foundation import NOW
from app.config import get_settings
from app.main import app
from app.api import deps


class ResourceBrowser(Browser):
    def __init__(self):
        super().__init__(WEB_ORIGIN)

    def _unsafe(self, method, path, json=None, headers=None, csrf=True):
        return super()._unsafe(method, path, json, {"Origin": WEB_ORIGIN} | (headers or {}), csrf)


class RuntimeHarness(Harness):
    def bootstrap_admin(self):
        return self.world.uid

    def browser(self):
        return ResourceBrowser()

    def admin_browser(self):
        b = self.browser()
        b.client.cookies.set("ras_session", self.world.actor.token)
        return b

    def create_user(self, email, password=USER_PASSWORD):
        created = issue(self.world, email=email)
        with Session(self.world.runtime) as db:
            user = invitations.accept_invitation(db, raw_token=created.token, password=password,
                display_name=None, now=self.clock.now, policy=self.world.settings.password,
                clock=lambda: self.clock.now)
            return user.id


@pytest.fixture
def resource_api(resource_world, monkeypatch):
    for key, value in DEV_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    clock = FakeClock()
    clock.now = NOW
    h = RuntimeHarness(resource_world.engine, clock)
    h.world = resource_world
    app.dependency_overrides[deps.get_now] = lambda: clock.now
    app.dependency_overrides[deps.get_clock] = lambda: (lambda: clock.now)
    try:
        yield h
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


BODY = b"Synthetic identical file bytes for two independent owners."
SHA = hashlib.sha256(BODY).hexdigest()


def source(w, owner, marker, *, data=BODY):
    with Session(w.engine, expire_on_commit=False) as db:
        item = LibraryItem(visibility="private", owner_user_id=owner, title=marker)
        db.add(item); db.flush()
        version = PaperVersion(item_id=item.id, version_label="v1", coverage="partial")
        db.add(version); db.flush()
        file = SourceFile(item_id=item.id, version_id=version.id, verified_mime="text/markdown",
                          storage_key=f"synthetic/{item.id}", byte_size=len(data),
                          checksum_sha256=hashlib.sha256(data).hexdigest())
        db.add(file); db.flush()
        item.active_version_id = version.id
        db.commit()
        return SimpleNamespace(item=item.id, version=version.id, file=file.id, key=file.storage_key)


def derived(w, ids, marker):
    identity = dict(item_id=ids.item, version_id=ids.version, source_file_id=ids.file)
    locator = dict(locator_kind="markdown", start_offset=0, end_offset=20, start_line=1, end_line=1)
    with Session(w.engine) as db:
        section = Section(**identity, **locator, kind="body", ordinal=0, text=marker, pipeline_version="synthetic")
        db.add(section); db.flush()
        db.add(Chunk(**identity, **locator, section_id=section.id, ordinal=0, text=marker,
                     chunk_hash=SHA, pipeline_version="synthetic"))
        db.add(MetadataCandidate(**identity, field_name="title", value=marker, locator_kind="markdown",
                                 start_offset=0, end_offset=20, pipeline_version="synthetic"))
        db.add(PdfPage(**identity, page_number=1, text=marker, width=600, height=800, role="body",
                       body_chars=20, body_page=True, qualifies=False, error_code="insufficient_text",
                       layout_mode="plain", pipeline_version="synthetic"))
        reference = ReferenceEntry(**identity, ordinal=0, raw_citation="arXiv:2609.00011", fields={},
            confidence="low", provenance="synthetic", locator_kind="markdown", start_offset=0,
            end_offset=20, pipeline_version="synthetic")
        db.add(reference); db.flush()
        rid = reference.id
        db.commit()
    return rid


class MemoryOriginals:
    def __init__(self, ids):
        self.objects = {ids.key: BODY}
    def open(self, key):
        from botocore.response import StreamingBody
        return StreamingBody(io.BytesIO(self.objects[key]), len(self.objects[key]))
    def put(self, key, file, mime):
        file.seek(0); self.objects[key] = file.read()
    def delete(self, key):
        self.objects.pop(key, None)


def runtime_approve(w, request_id, storage, *, title="Common readable", source="synthetic test author",
                    evidence="synthetic permission", download=False, search=True, provider=False, clock=None):
    """Approve through the C2d-5b runtime boundary: real Admin session/CSRF, proofs,
    copy, routines and journal. Only original storage is supplied by the caller."""
    import asyncio
    import threading
    from app.api import review_approval
    from app.api.routes.reviews import ApproveInput
    admin = Principal(w.uid, "admin", w.actor.session_id, "admin@example.test", None)
    data = ApproveInput(confirm_verified=True, file_checksum_sha256=SHA, title=title, verified_source=source,
        evidence_note=evidence, checked_on=NOW.date(), scope=dict(audience="instance_users",
        original_download=download, fulltext_search=search, external_processing=provider, conditions=None))
    return asyncio.run(review_approval.attempt(None, admin, str(request_id), data, w.actor.csrf_token,
        clock or (lambda: NOW), get_settings(), storage, threading.Event()))


def common_copy(w, *, download=False, search=True, provider=False, indexed=True, pending=False):
    """Valid rights via the runtime approval/closure journal; only original storage is in memory."""
    ids = source(w, w.uid, "review source")
    with Session(w.engine, expire_on_commit=False) as db:
        rr = ReviewRequest(item_id=ids.item, version_id=ids.version, source_file_id=ids.file,
            requester_user_id=w.uid, status="open", file_checksum_sha256=SHA, rights_epoch_at_submit=0,
            notice_version="synthetic", declared_source="test author", declared_rights="synthetic permission",
            submitted_at=NOW)
        db.add(rr); db.commit(); rid=rr.id
    admin = Principal(w.uid, "admin", w.actor.session_id, "admin@example.test", None)
    storage = MemoryOriginals(ids)
    done = runtime_approve(w, rid, storage, download=download, search=search,
                           provider=provider or (indexed and not pending))
    item = uuid.UUID(done["request"]["pending_publication_id"])
    with Session(w.engine) as db:
        version, file, key = db.execute(text("SELECT version_id, id, storage_key FROM source_files WHERE item_id=:i"),
                                        {"i": item}).one()
    result = SimpleNamespace(item=item, version=version, file=file, key=key, admin=admin, storage=storage)
    if not pending:
        runtime_publish(w, result.item, storage)
    derived(w, result, "COMMON-TEXT")
    if indexed and not pending:
        from app.models import IngestionJob, IndexGeneration
        from app.authz import COMMON_INDEX_VERSION
        with Session(w.engine) as db:
            job = IngestionJob(origin="promotion", scope_visibility="common", item_id=result.item,
                version_id=result.version, source_file_id=result.file, rights_epoch_at_start=0,
                idempotency_key=str(result.item), state="ready", finished_at=NOW)
            db.add(job); db.flush()
            generation = IndexGeneration(item_id=result.item, version_id=result.version, source_file_id=result.file,
                job_id=job.id, model_id="synthetic-model", provider="synthetic", dimensions=3,
                pipeline_version="synthetic", index_version=COMMON_INDEX_VERSION, expected_chunks=1,
                rights_epoch=0, state="active")
            db.add(generation); db.flush()
            item=db.get(LibraryItem,result.item)
            item.active_version_id=result.version; item.active_index_generation_id=generation.id
            item.content_status="indexed"
            db.commit()
        if not provider:
            # Privileged test-only corruption models a searchable indexed copy
            # whose current rights do not permit provider payloads. Production
            # issuance needs both permissions; no runtime grant/fence is changed.
            with w.engine.begin() as conn:
                conn.execute(text("SET LOCAL session_replication_role='replica'"))
                conn.execute(text("UPDATE access_rights SET permits_external_processing=false WHERE item_id=:i"),
                             {"i":result.item})
    return result


def runtime_publish(w, item_id, storage, *, checksum=SHA, clock=None, principal=None, csrf=None):
    """Real runtime publish proof/hash/acceptance/response; no privileged publish setup."""
    import asyncio
    import threading
    from app.api import common_publish
    admin = principal or Principal(w.uid, "admin", w.actor.session_id, "admin@example.test", None)
    return asyncio.run(common_publish.attempt(None, admin, str(item_id), checksum,
        csrf if csrf is not None else w.actor.csrf_token, clock or (lambda: NOW),
        get_settings(), storage, threading.Event()))


def runtime_revoke(w, item_id, *, clock=None, principal=None, csrf=None, retain=(), reason='rights_withdrawn'):
    """Real C2d-5c2 runtime transaction/mirror/append/confirm/fresh response."""
    import asyncio
    import threading
    from app.api import common_revoke
    from app.api.routes.publications import RevokeInput
    admin = principal or Principal(w.uid, 'admin', w.actor.session_id, 'admin@example.test', None)
    body = RevokeInput(confirm_revoke=True, reason_code=reason, retain=list(retain), confirm_retention_basis=bool(retain))
    return asyncio.run(common_revoke.attempt(None, admin, str(item_id), body,
        csrf if csrf is not None else w.actor.csrf_token, clock or (lambda: NOW), get_settings(), threading.Event()))
