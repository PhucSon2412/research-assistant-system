"""Run the question set through the existing API on a throw-away database.

The runner uploads the corpus with real upload intents, runs the real worker
(extraction, chunking, indexing) and asks every question through ``POST
/search`` and ``POST /rag/ask`` as User A, User B or Admin. Provider calls are
replaced in-process: embeddings by ``local_embedding`` and generation by a
capture that records the exact payload and returns no claims. Outbound HTTP is
blocked for the whole run, so no text leaves the machine and nothing is billed.
Answer quality is therefore not measured here.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app import config as app_config
from app import db as app_db
from app.authz import Principal
from app.config import EmbeddingPolicy, GenerationPolicy, PasswordPolicy, RerankPolicy, Settings, get_settings
from app.evaluation import local_embedding
from app.evaluation.corpus import sha256_bytes
from app.evaluation.locator import DIAGNOSTIC_OVERLAP, FULL_SPAN, Hit, covers
from app.evaluation.metrics import K, Row, failure, side_ranks
from app.evaluation.report import build
from app.evaluation.schema import Manifest, Question, QuestionSet, allowed_items, load
from app.evaluation.validate import validate
from app.identity.bootstrap import bootstrap_admin
from app.identity.passwords import hash_password
from app.ingest.worker import process_job
from app.main import app
from app.rag import GenerationResult, is_comparison
from app.search import _rank, hydrate
from app.storage import OriginalStorage

API_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = API_DIR.parent
ORIGIN = "http://eval.local"
MODES = ("keyword", "hybrid")
TERMINAL = {"ready", "needs_review", "fulltext_rejected", "failed", "duplicate"}
CAPTURE_MODEL = "local/capture-no-generation"
EMAILS = {"user_a": "eval-user-a@example.org", "user_b": "eval-user-b@example.org",
          "admin": "eval-admin@example.org"}


# --- isolation -----------------------------------------------------------------------------------

@dataclass
class Capture:
    """Everything the RAG pipeline tried to hand to a provider during one request."""
    payloads: list[dict] = field(default_factory=list)
    rag_hits: list[list[dict]] = field(default_factory=list)
    blocked_hosts: list[str] = field(default_factory=list)
    embedding_calls: int = 0
    prompt_bytes: list[int] = field(default_factory=list)

    def reset(self) -> None:
        self.payloads.clear()
        self.rag_hits.clear()
        self.prompt_bytes.clear()


def eval_settings(base: Settings) -> Settings:
    """In-process copy for the local fakes only; never persisted or used for a provider."""
    settings = base.model_copy(deep=True)
    settings.embedding.model_id = local_embedding.MODEL_ID
    settings.embedding.api_key = SecretStr("local-evaluation-no-network")
    settings.embedding.pricing_verified = True
    settings.generation.model_id = CAPTURE_MODEL
    settings.generation.pricing_verified = True
    settings.rerank.pricing_verified = False
    settings.search.max_requests_per_user_day = 100_000
    settings.generation.max_requests_per_user_day = 100_000
    return settings


@contextmanager
def instrumented(capture: Capture) -> Iterator[None]:
    """Replace provider adapters and refuse any real outbound HTTP connection."""
    import app.rag as rag
    import app.rag_runtime as runtime

    original_search = runtime.retrieve

    def recorded_search(*args, **kwargs):
        result = original_search(*args, **kwargs)
        capture.rag_hits.append((result[0] if isinstance(result, tuple) else result)["hits"])
        return result

    def local_embed(value, settings, **kwargs):
        capture.embedding_calls += 1
        return local_embedding.embed(value, settings, **kwargs)

    def capture_generation(messages, settings):
        capture.payloads.append({"system": messages[0]["content"], "user": json.loads(messages[1]["content"])})
        capture.prompt_bytes.append(len(json.dumps(messages, ensure_ascii=False).encode("utf-8")))
        return GenerationResult('{"claims":[]}', 0, 0, settings.generation.model_id, "local-capture")

    def refuse(self, request):
        capture.blocked_hosts.append(request.url.host)
        raise httpx.ConnectError("outbound HTTP is disabled during evaluation", request=request)

    with ExitStack() as stack:
        stack.enter_context(mock.patch("app.ingest.indexer.embed", local_embed))
        stack.enter_context(mock.patch("app.search.embed", local_embed))
        stack.enter_context(mock.patch("app.rag.generate", capture_generation))
        stack.enter_context(mock.patch("app.rag_runtime.retrieve", recorded_search))
        stack.enter_context(mock.patch.object(httpx.HTTPTransport, "handle_request", refuse))
        yield


@dataclass
class Environment:
    engine: Engine
    storage: OriginalStorage
    settings: Settings
    configured_pricing: dict[str, bool]


@contextmanager
def environment(repo_dir: Path = REPO_DIR, keep: bool = False) -> Iterator[Environment]:
    """Fresh migrated database and bucket on the Compose PostgreSQL/MinIO, removed afterwards."""
    values = {k: (v or "") for k, v in dotenv_values(repo_dir / ".env").items()}
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        if not values.get(key):
            raise SystemExit(f"{key} missing in .env; see docs/dev-setup.md")
    admin_url = URL.create("postgresql+psycopg", username=values["POSTGRES_USER"],
                           password=values["POSTGRES_PASSWORD"], host="127.0.0.1",
                           port=int(values.get("POSTGRES_HOST_PORT") or 5432), database=values["POSTGRES_DB"])
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    name = f"ras_eval_{uuid.uuid4().hex[:12]}"
    url = admin_url.set(database=name)
    env = {"RAS_DB__HOST": url.host, "RAS_DB__PORT": str(url.port), "RAS_DB__USER": url.username,
           "RAS_DB__PASSWORD": url.password, "RAS_DB__NAME": name,
           "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{values.get('MINIO_API_HOST_PORT') or '9000'}",
           "RAS_STORAGE__ACCESS_KEY": values["MINIO_ROOT_USER"],
           "RAS_STORAGE__SECRET_KEY": values["MINIO_ROOT_PASSWORD"],
           "RAS_STORAGE__BUCKET": f"ras-eval-{uuid.uuid4().hex[:16]}",
           "RAS_ENVIRONMENT": "development", "RAS_HTTP__COOKIE_SECURE": "false",
           "RAS_HTTP__ALLOWED_ORIGINS": json.dumps([ORIGIN]), "RAS_DELIVERY__MODE": "disabled"}
    saved = {key: os.environ.get(key) for key in (*env, "RAS_DATABASE_URL")}
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    storage = None
    try:
        os.environ.pop("RAS_DATABASE_URL", None)
        os.environ.update(env)
        app_config.get_settings.cache_clear()
        app_db.get_engine.cache_clear()
        cfg = Config(str(API_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(API_DIR / "migrations"))
        cfg.attributes["database_url"] = url
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")
        base = get_settings()
        configured = {"embedding": base.embedding.pricing_verified, "rerank": base.rerank.pricing_verified,
                      "generation": base.generation.pricing_verified}
        settings = eval_settings(base)
        storage = OriginalStorage(settings)
        storage.ensure_bucket()
        app.dependency_overrides[get_settings] = lambda: settings
        yield Environment(app_db.get_engine(), storage, settings, configured)
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app_db.get_engine().dispose()
        if storage is not None and not keep:
            for key, _ in storage.orphan_keys(""):
                storage.delete(key)
            storage.client.delete_bucket(Bucket=storage.bucket)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        app_config.get_settings.cache_clear()
        app_db.get_engine.cache_clear()
        if not keep:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --- seeding through the product API ------------------------------------------------------------

@dataclass
class Identity:
    key: str
    user_id: uuid.UUID
    role: str
    client: TestClient

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies["ras_csrf"], "origin": ORIGIN}

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers=self.headers())

    def principal(self) -> Principal:
        return Principal(user_id=self.user_id, role=self.role, session_id=uuid.uuid4(),
                         email=EMAILS[self.key], display_name=None)


@dataclass
class Seeded:
    identities: dict[str, Identity]
    items: dict[str, uuid.UUID]
    collections: dict[str, uuid.UUID]
    ingest: dict[str, dict]
    owners: dict[str, str]

    def item_key(self, item_id) -> str:
        wanted = str(item_id)
        return next((key for key, value in self.items.items() if str(value) == wanted), f"unknown:{wanted}")


def _login(email: str, password: str, key: str, user_id: uuid.UUID, role: str) -> Identity:
    client = TestClient(app, base_url="http://testserver")
    client.get("/auth/csrf")
    response = client.post("/auth/login", json={"email": email, "password": password},
                           headers={"X-CSRF-Token": client.cookies["ras_csrf"], "origin": ORIGIN})
    if response.status_code != 200:
        raise RuntimeError(f"login failed for {key}: {response.status_code}")
    return Identity(key, user_id, role, client)


def seed(env: Environment, manifest: Manifest, root: Path) -> Seeded:
    passwords = {key: secrets.token_urlsafe(24) for key in EMAILS}  # never printed or stored
    admin_id = bootstrap_admin(env.engine, email=EMAILS["admin"], password=passwords["admin"],
                               policy=PasswordPolicy()).user_id
    ids = {"admin": admin_id}
    with env.engine.begin() as conn:
        for key in ("user_a", "user_b"):
            ids[key] = conn.scalar(text("INSERT INTO users (email, password_verifier) VALUES (:e, :v) RETURNING id"),
                                   {"e": EMAILS[key], "v": hash_password(passwords[key])})
    identities = {key: _login(EMAILS[key], passwords[key], key, ids[key], "admin" if key == "admin" else "user")
                  for key in EMAILS}
    papers = {paper.id: paper for paper in manifest.papers}
    items: dict[str, uuid.UUID] = {}
    ingest: dict[str, dict] = {}
    for entry in manifest.items:
        if entry.owner == "common":
            with env.engine.begin() as conn:
                item_id = conn.scalar(text("INSERT INTO library_items (visibility, publication_state, title) VALUES ('common', 'listed', :t)"
                                           " RETURNING id"), {"t": entry.title})
                conn.execute(text("INSERT INTO paper_versions (item_id, version_label) VALUES (:i, 'v1')"),
                             {"i": item_id})
            items[entry.key] = item_id
            continue
        paper = papers[entry.paper]
        owner = identities[entry.owner]
        data = (root / "corpus" / paper.file).read_bytes()
        notice = owner.client.get("/uploads/notice").json()["version"]
        intent = owner.post("/uploads/intents", {
            "acknowledged": True, "notice_version": notice, "file_kind": paper.format,
            "filename": Path(paper.file).name, "byte_size": len(data), "sha256": sha256_bytes(data)})
        if intent.status_code != 201:
            raise RuntimeError(f"upload intent for {entry.key} failed: {intent.status_code}")
        sent = owner.client.put("/uploads/original", content=data, headers={
            **owner.headers(), "x-upload-intent": intent.json()["intent_token"],
            "content-type": "application/octet-stream"})
        if sent.status_code != 201:
            raise RuntimeError(f"upload of {entry.key} failed: {sent.status_code}")
        uploaded = sent.json()
        items[entry.key] = uuid.UUID(uploaded["item_id"])
        state = uploaded["state"]
        for _ in range(8):
            state = process_job(uuid.UUID(uploaded["job_id"]), env.settings, env.storage)
            if state in TERMINAL:
                break
        with env.engine.connect() as conn:
            row = conn.execute(text("""SELECT i.content_status, v.coverage,
                (SELECT count(*) FROM index_entries e WHERE e.generation_id=i.active_index_generation_id) chunks
                FROM library_items i JOIN paper_versions v ON v.item_id=i.id WHERE i.id=:i"""),
                {"i": items[entry.key]}).one()
        ingest[entry.key] = {"job_state": state, "content_status": row.content_status,
                             "coverage": row.coverage, "indexed_chunks": row.chunks,
                             "expected_coverage": paper.expected_coverage}
    collections = {}
    for collection in manifest.collections:
        owner = identities[collection.owner]
        created = owner.post("/collections", {"name": collection.key})
        if created.status_code != 201:
            raise RuntimeError(f"collection {collection.key} failed: {created.status_code}")
        collections[collection.key] = uuid.UUID(created.json()["collection"]["id"])
        for key in collection.items:
            added = owner.post(f"/collections/{collections[collection.key]}/items", {"item_id": str(items[key])})
            if added.status_code != 201:
                raise RuntimeError(f"adding {key} to {collection.key} failed: {added.status_code}")
    return Seeded(identities, items, collections, ingest, {item.key: item.owner for item in manifest.items})


# --- measurement ---------------------------------------------------------------------------------

def _hit(row: dict, seeded: Seeded) -> Hit:
    locator = row.get("locator") or row
    detail = locator.get("locator_detail") or {}
    if locator["locator_kind"] == "pdf":
        start, end = detail.get("start_offset"), detail.get("end_offset")
    else:
        start, end = locator.get("start_offset"), locator.get("end_offset")
    return Hit(seeded.item_key(row["item_id"]), locator["locator_kind"], locator.get("start_page"),
               start if start is not None else -1, end if end is not None else -1)


def indexed_hits(env: Environment, seeded: Seeded, item_key: str) -> list[Hit]:
    """Every chunk of the item's active generation, as the index holds it."""
    with env.engine.connect() as conn:
        rows = conn.execute(text("""SELECT c.item_id, c.locator_kind, c.start_page, c.start_offset,
                c.end_offset, c.locator_detail FROM library_items i
            JOIN index_entries e ON e.generation_id=i.active_index_generation_id
            JOIN chunks c ON c.id=e.chunk_id WHERE i.id=:i"""), {"i": seeded.items[item_key]}).mappings().all()
    return [_hit(dict(row), seeded) for row in rows]


def _scope_body(question: Question, seeded: Seeded, *, rag: bool) -> dict:
    scope = question.scope
    if scope.kind == "item":
        return {"item_id": str(seeded.items[scope.item])}
    if scope.kind == "collection":
        return {"collection_id": str(seeded.collections[scope.collection])}
    return {"scope": "all"} if rag else {}


def _acl(question: Question, manifest: Manifest, seeded: Seeded, status: int, body: str,
         item_ids: list[str], sent: str = "") -> list[str]:
    """Scope 404, item ownership and foreign ACL markers in the response and provider payload."""
    violations = []
    if question.expected.status == "scope_not_found":
        if status != 404 or json.loads(body) != {"error": "not_found"}:
            violations.append(f"expected_404_got_{status}")
    allowed = {str(seeded.items[key]) for key in allowed_items(manifest, question.identity, question.scope)}
    violations += [f"item_outside_scope:{seeded.item_key(i)}" for i in sorted(set(item_ids)) if i not in allowed]
    markers = set(question.forbidden_markers) | {m for item in manifest.items if item.owner != question.identity
                                                  for m in item.acl_markers}
    violations += [f"marker:{m}" for m in sorted(markers) if m in body or m in sent]
    return violations


def _record(question: Question, stage: str, mode: str | None, status: int, hits: list[Hit],
            acl: list[str], **extra) -> dict:
    return {"question_id": question.id, "stage": stage, "mode": mode, "status_code": status,
            "hits": [[h.item, h.locator_kind, h.page, h.start_offset, h.end_offset] for h in hits],
            "acl_violations": acl, **extra}


def evaluate(env: Environment, manifest: Manifest, qset: QuestionSet, seeded: Seeded,
             capture: Capture, modes: tuple[str, ...] = MODES) -> list[dict]:
    records = []
    for question in qset.questions:
        who = seeded.identities[question.identity]
        for mode in modes:
            response = who.post("/search", {"query": question.question, "mode": mode, "rerank": False,
                                            **_scope_body(question, seeded, rag=False)})
            found = response.json() if response.status_code == 200 else {}
            rows = found.get("hits", [])
            hits = [_hit(row, seeded) for row in rows]
            records.append(_record(question, "search", mode, response.status_code, hits,
                                   _acl(question, manifest, seeded, response.status_code, response.text,
                                        [row["item_id"] for row in rows]),
                                   chunk_ids=[row["chunk_id"] for row in rows],
                                   keyword_query=found.get("keyword_query")))
            capture.reset()
            response = who.post("/rag/ask", {"question": question.question, "mode": mode,
                                             **_scope_body(question, seeded, rag=True)})
            payload = capture.payloads[0]["user"] if capture.payloads else None
            evidence = payload["evidence"] if payload else []
            pool = [_hit(row, seeded) for row in (capture.rag_hits[0] if capture.rag_hits else [])]
            sent = json.dumps(capture.payloads, ensure_ascii=False)
            # A supported_claim may quote the paper verbatim, so only label-only text is a leak signal.
            gold_leak = [label for label in (question.rationale, question.label_status, question.id)
                         if payload is not None and label in sent]
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            records.append(_record(
                question, "rag_pack", mode, response.status_code, [_hit(row, seeded) for row in evidence],
                _acl(question, manifest, seeded, response.status_code, response.text,
                     [row["item_id"] for row in evidence] + [c["item_id"] for claim in body.get("claims", [])
                                                             for c in claim.get("citations", [])], sent),
                chunk_ids=[row["chunk_id"] for row in evidence], generation_called=payload is not None,
                pack_chunks=len(evidence), pack_papers=len({row["item_id"] for row in evidence}),
                pack_text_bytes=sum(len(row["text"].encode("utf-8")) for row in evidence),
                prompt_byte_token_ceiling=capture.prompt_bytes[0] if capture.prompt_bytes else None,
                rag_status=body.get("status"), rag_reason=body.get("reason"), coverage=body.get("coverage"),
                search_pool=[[h.item, h.locator_kind, h.page, h.start_offset, h.end_offset] for h in pool],
                gold_label_in_payload=gold_leak, payload_request_keys=sorted(payload) if payload else []))
        if question.expected.status != "scope_not_found":
            records.append(_vector_record(env, manifest, question, seeded))
        else:
            records.append(_record(question, "vector", None, 404, [], []))
    return records


def _vector_record(env: Environment, manifest: Manifest, question: Question, seeded: Seeded) -> dict:
    """Vector-only ranking through the same SQL gate the API uses (no fusion, no rerank)."""
    who = seeded.identities[question.identity]
    scope = question.scope
    item_id = seeded.items[scope.item] if scope.kind == "item" else None
    collection_id = seeded.collections[scope.collection] if scope.kind == "collection" else None
    with Session(env.engine) as db:
        principal = who.principal()
        ids = _rank(db, principal, item_id, env.settings, question.question,
                    local_embedding.vector(question.question), collection_id).semantic
        rows = hydrate(db, principal, item_id, ids, collection_id)
        db.rollback()
    ordered = [dict(rows[i]) | {"chunk_id": str(i)} for i in ids if i in rows]
    hits = [_hit(row, seeded) for row in ordered]
    body = json.dumps([row["text"] for row in ordered], ensure_ascii=False)
    return _record(question, "vector", "local_vector", 200, hits,
                   _acl(question, manifest, seeded, 200, body, [str(row["item_id"]) for row in ordered])
                   if question.expected.status != "scope_not_found" else [],
                   chunk_ids=[row["chunk_id"] for row in ordered])


def check_viewer(seeded: Seeded, records: list[dict]) -> dict:
    """Open every retrieved chunk as its owner (200) and as everyone else (404)."""
    chunks: dict[str, str] = {}
    for record in records:
        for chunk_id, hit in zip(record.get("chunk_ids", []), record["hits"]):
            chunks[chunk_id] = seeded.owners.get(hit[0], "unknown")
    checks, violations = 0, []
    for chunk_id, owner in sorted(chunks.items()):
        for key, identity in seeded.identities.items():
            status = identity.client.get(f"/search/chunks/{chunk_id}").status_code
            checks += 1
            if status != (200 if key == owner else 404):
                violations.append({"chunk_id": chunk_id, "identity": key, "status": status})
    return {"chunks": len(chunks), "requests": checks, "violations": violations}


# --- scoring ---------------------------------------------------------------------------------------

def score(manifest: Manifest, qset: QuestionSet, records: list[dict], index: dict[str, list[Hit]],
          minimum: float = FULL_SPAN) -> list[Row]:
    """One Row per answered question and stage; ranks are 1-based positions in the returned list.

    ``minimum`` selects the span-coverage rule; the headline uses the whole span.
    """
    rows = []
    by_id = {q.id: q for q in qset.questions}
    for record in records:
        question = by_id[record["question_id"]]
        if question.expected.status != "answered":
            continue
        hits = [Hit(*h) for h in record["hits"]]
        ranks = side_ranks(question, hits, minimum)
        indexed = [any(covers(span, hit, minimum) for span in side.spans for hit in index.get(span.item, []))
                   for side in question.evidence]
        acl = bool(record["acl_violations"])
        if record["stage"] == "rag_pack":
            pool = [Hit(*h) for h in record["search_pool"]]
            pool_ranks = side_ranks(question, pool, minimum)
            outcome = failure(ranks, indexed, acl_violation=acl, k=len(hits) or 1, pool="retrieval")
            if outcome == "retrieval" and all(rank is not None for rank in pool_ranks):
                outcome = "evidence_pack"
            if record["status_code"] != 200:
                outcome = f"http_{record['status_code']}"
        else:
            outcome = failure(ranks, indexed, acl_violation=acl, k=K)
        stage = record["stage"] if record["stage"] == "vector" else f"{record['stage']}:{record['mode']}"
        rows.append(Row(question.id, question.group, question.split, question.scope.kind, stage,
                        tuple(ranks), outcome))
    return rows


def git_state(repo_dir: Path = REPO_DIR) -> dict:
    def run(*args):
        return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, check=False).stdout.strip()
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def run(root: Path, *, modes: tuple[str, ...] = MODES, keep: bool = False) -> dict:
    errors = validate(root)
    if errors:
        raise SystemExit("dataset is invalid; run `python -m app.evaluation validate`:\n" + "\n".join(errors))
    manifest, qset = load(root)
    capture = Capture()
    started = datetime.now(timezone.utc)
    with environment(keep=keep) as env, instrumented(capture):
        seeded = seed(env, manifest, root)
        index = {key: indexed_hits(env, seeded, key) for key in seeded.ingest}
        records = evaluate(env, manifest, qset, seeded, capture, modes)
        viewer = check_viewer(seeded, records)
        settings = env.settings
        configured = env.configured_pricing
    rows = score(manifest, qset, records, index)
    diagnostic = score(manifest, qset, records, index, DIAGNOSTIC_OVERLAP)
    return build(manifest=manifest, qset=qset, records=records, rows=rows, diagnostic_rows=diagnostic,
                 index=index, seeded=seeded,
                 viewer=viewer, capture=capture, settings=settings, configured_pricing=configured,
                 defaults={"embedding": EmbeddingPolicy().pricing_verified,
                           "rerank": RerankPolicy().pricing_verified,
                           "generation": GenerationPolicy().pricing_verified},
                 comparison_detected={q.id: is_comparison(q.question) for q in qset.questions},
                 started=started, git=git_state(), modes=modes)
