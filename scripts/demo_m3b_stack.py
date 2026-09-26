"""Two synthetic users: real Compose web/API, Celery extraction, fake host indexer."""
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


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    env = dotenv_values(root / ".env")
    for key, value in {
        "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": env.get("POSTGRES_HOST_PORT") or "5432",
        "RAS_DB__USER": env["POSTGRES_USER"], "RAS_DB__PASSWORD": env["POSTGRES_PASSWORD"],
        "RAS_DB__NAME": env["POSTGRES_DB"],
        "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{env.get('MINIO_API_HOST_PORT') or '9000'}",
        "RAS_STORAGE__ACCESS_KEY": env["MINIO_ROOT_USER"],
        "RAS_STORAGE__SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
        "RAS_STORAGE__BUCKET": "ras-originals",
    }.items():
        os.environ[key] = str(value)
    get_settings.cache_clear()
    settings = get_settings().model_copy(deep=True)
    settings.embedding.api_key = SecretStr("synthetic-fake-key")
    settings.embedding.pricing_verified = True
    engine = create_engine(URL.create("postgresql+psycopg", username=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"], host="127.0.0.1",
        port=int(env.get("POSTGRES_HOST_PORT") or "5432"), database=env["POSTGRES_DB"]), hide_parameters=True)
    api = f"http://127.0.0.1:{env.get('API_HOST_PORT') or '8000'}"
    web = f"http://127.0.0.1:{env.get('WEB_HOST_PORT') or '3000'}"
    with httpx.Client(timeout=30) as probe:
        assert probe.get(web + "/search").status_code == 200

    def fake_embed(value: str, selected):
        assert "Bibliography" not in value
        return EmbeddingResult([.1, .2, .3], selected.embedding.model_id, len(value.split()))
    indexer.embed = fake_embed
    clients = []
    ids = []
    for label in ("A", "B"):
        email = f"m3b-demo-{label.lower()}-{uuid.uuid4().hex[:8]}@example.org"
        password = secrets.token_urlsafe(24)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(email,password_verifier) VALUES(:e,:p)"),
                         {"e": email, "p": hash_password(password)})
        client = httpx.Client(base_url=api, timeout=30)
        assert client.get("/auth/csrf").status_code == 200
        csrf = client.cookies["ras_csrf"]
        assert client.post("/auth/login", json={"email": email, "password": password},
                           headers={"origin": web, "x-csrf-token": csrf}).status_code == 200
        headers = {"origin": web, "x-csrf-token": client.cookies["ras_csrf"]}
        body = (f"## Methods\nSynthetic {label} private observation.\n"
                "## References\n[1] Bibliography only.\n").encode()
        notice = client.get("/uploads/notice").json()["version"]
        intent = client.post("/uploads/intents", json={"acknowledged": True,
            "notice_version": notice, "file_kind": "markdown", "filename": f"synthetic-{label}.md",
            "byte_size": len(body), "sha256": hashlib.sha256(body).hexdigest()}, headers=headers)
        assert intent.status_code == 201, intent.text
        sent = client.put("/uploads/original", content=body, headers={**headers,
            "x-upload-intent": intent.json()["intent_token"], "content-type": "application/octet-stream"})
        assert sent.status_code == 201, sent.text
        item = sent.json()
        for _ in range(40):
            state = client.get(f"/library/items/{item['item_id']}").json()["item"]["jobs"][0]["state"]
            if state == "awaiting_index":
                break
            time.sleep(1)
        else:
            raise AssertionError("Celery extraction did not finish")
        assert process_job(uuid.UUID(item["job_id"]), settings, OriginalStorage(settings)) == "ready"
        clients.append(client)
        ids.append(item)
    for own, other, label in ((0, 1, "A"), (1, 0, "B")):
        client = clients[own]
        response = client.post("/search", json={"query": "observation", "mode": "keyword",
            "rerank": False}, headers={"origin": web, "x-csrf-token": client.cookies["ras_csrf"]})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["mode"] == "keyword" and result["count"] == 1
        assert result["hits"][0]["item_id"] == ids[own]["item_id"]
        chunk_id = result["hits"][0]["chunk_id"]
        assert client.get(f"/search/chunks/{chunk_id}").status_code == 200
        assert clients[other].get(f"/search/chunks/{chunk_id}").status_code == 404
        assert ids[other]["item_id"] not in response.text
        assert client.get(f"/library/items/{ids[other]['item_id']}").status_code == 404
        print({"user": label, "mode": result["mode"], "hits": result["count"],
               "owner_only": True, "rerank": result["rerank_status"]})
    for client in clients:
        client.close()
    engine.dispose()
    print("web_search_page=200; external_provider_calls=0 (fake index embedding)")


if __name__ == "__main__":
    main()
