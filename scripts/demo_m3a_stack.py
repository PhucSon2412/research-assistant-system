"""Synthetic HTTP upload + real ingest worker function with a fake provider.

Uses the Compose API, PostgreSQL, MinIO and Celery extraction. The indexing
step is invoked through the same process_job entry point as Celery, with only
the outbound embedding transport replaced. No paper or secret is printed.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from app.config import get_settings
from app.identity.passwords import hash_password
from app.ingest import indexer
from app.ingest.embedding import EmbeddingResult
from app.ingest.worker import process_job
from app.storage import OriginalStorage

root = Path(__file__).resolve().parents[1]
env = dotenv_values(root / ".env")
for name, value in {
    "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": env.get("POSTGRES_HOST_PORT") or "5432",
    "RAS_DB__USER": env["POSTGRES_USER"], "RAS_DB__PASSWORD": env["POSTGRES_PASSWORD"],
    "RAS_DB__NAME": env["POSTGRES_DB"],
    "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{env.get('MINIO_API_HOST_PORT') or '9000'}",
    "RAS_STORAGE__ACCESS_KEY": env["MINIO_ROOT_USER"],
    "RAS_STORAGE__SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
    "RAS_STORAGE__BUCKET": "ras-originals",
}.items():
    os.environ[name] = str(value)
get_settings.cache_clear()
settings = get_settings().model_copy(deep=True)
settings.embedding.api_key = SecretStr("synthetic-fake-key")
db_url = URL.create("postgresql+psycopg", username=env["POSTGRES_USER"],
    password=env["POSTGRES_PASSWORD"], host="127.0.0.1",
    port=int(env.get("POSTGRES_HOST_PORT") or "5432"), database=env["POSTGRES_DB"])
engine = create_engine(db_url, hide_parameters=True)
email = f"m3a-demo-{uuid.uuid4().hex[:10]}@example.org"
password = secrets.token_urlsafe(24)
with engine.begin() as conn:
    conn.execute(text("INSERT INTO users(email,password_verifier) VALUES(:e,:p)"),
                 {"e": email, "p": hash_password(password)})
api = f"http://127.0.0.1:{env.get('API_HOST_PORT') or '8000'}"
web = f"http://127.0.0.1:{env.get('WEB_HOST_PORT') or '3000'}"
body = ("# Synthetic result\n\n## Abstract\nA synthetic abstract.\n\n## Methods\n"
        + "A synthetic observation for indexing. " * 12
        + "\n\n## References\n[1] Synthetic bibliography.\n").encode()
with httpx.Client(base_url=api, timeout=30) as client:
    assert client.get("/auth/csrf").status_code == 200
    csrf = client.cookies["ras_csrf"]
    assert client.post("/auth/login", json={"email": email, "password": password},
                       headers={"origin": web, "x-csrf-token": csrf}).status_code == 200
    headers = {"origin": web, "x-csrf-token": client.cookies["ras_csrf"]}
    notice = client.get("/uploads/notice").json()["version"]
    intent = client.post("/uploads/intents", json={"acknowledged": True, "notice_version": notice,
        "file_kind": "markdown", "filename": "m3a-synthetic.md", "byte_size": len(body),
        "sha256": hashlib.sha256(body).hexdigest()}, headers=headers)
    assert intent.status_code == 201, intent.status_code
    sent = client.put("/uploads/original", content=body, headers={**headers,
        "x-upload-intent": intent.json()["intent_token"], "content-type": "application/octet-stream"})
    assert sent.status_code == 201, sent.status_code
    ids = sent.json()
    for _ in range(40):
        detail = client.get(f"/library/items/{ids['item_id']}").json()["item"]
        if detail["jobs"][0]["state"] == "awaiting_index":
            break
        time.sleep(1)
    else:
        raise AssertionError("Celery extraction did not reach awaiting_index")

calls = []
def fake_embed(value: str, selected):
    assert value in body.decode() and "References" not in value
    calls.append(value)
    return EmbeddingResult([0.1, 0.2, 0.3], selected.embedding.model_id, len(value.split()))
indexer.embed = fake_embed
started = time.monotonic()
state = process_job(uuid.UUID(ids["job_id"]), settings, OriginalStorage(settings))
elapsed_ms = round((time.monotonic() - started) * 1000)
assert state == "ready", state
with engine.connect() as conn:
    metrics = conn.execute(text("SELECT j.created_at, j.started_at, j.extraction_finished_at, j.finished_at,"
        " sum(c.input_tokens), sum(c.reserved_usd) FROM ingestion_jobs j"
        " JOIN source_files f ON f.job_id=j.id JOIN embedding_calls c ON c.source_file_id=f.id"
        " WHERE j.id=:j GROUP BY j.id"), {"j": uuid.UUID(ids["job_id"])}).one()
queue_ms = round((metrics.started_at - metrics.created_at).total_seconds() * 1000)
print({"state": state, "queue_wait_ms": queue_ms, "indexing_worker_ms": elapsed_ms,
       "embedding_requests": len(calls), "input_tokens_fake": int(metrics[4]),
       "cost": "unknown (fake provider)", "reserved_ceiling_usd": str(metrics[5])})
