"""Dev-only HTTP/Celery/Redis/MinIO PDF demo. Uses synthetic text only.

Run with Conda ras after `docker compose up -d --build --wait`.
The disposable users/items stay in the dev DB; no credentials are printed.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from app.identity.passwords import hash_password

ROOT = Path(__file__).resolve().parents[1]
env = dotenv_values(ROOT / ".env")
db_url = URL.create("postgresql+psycopg", username=env["POSTGRES_USER"],
    password=env["POSTGRES_PASSWORD"], host="127.0.0.1",
    port=int(env.get("POSTGRES_HOST_PORT") or "5432"), database=env["POSTGRES_DB"])
web = f"http://127.0.0.1:{env.get('WEB_HOST_PORT') or '3000'}"
engine = create_engine(db_url, hide_parameters=True)
marker = uuid.uuid4().hex[:10]
credentials = {role: (f"m2b-{role}-{marker}@example.org", secrets.token_urlsafe(24))
               for role in ("a", "b", "admin")}
with engine.begin() as conn:
    for role, (email, password) in credentials.items():
        conn.execute(text("INSERT INTO users (email, role, password_verifier) VALUES (:e, :r, :p)"),
                     {"e": email, "r": "admin" if role == "admin" else "user",
                      "p": hash_password(password)})


def browser(role: str) -> httpx.Client:
    email, password = credentials[role]
    client = httpx.Client(base_url=web, timeout=90)
    assert client.get("/api/auth/csrf").status_code == 200
    login = client.post("/api/auth/login", json={"email": email, "password": password},
        headers={"origin": web, "x-csrf-token": client.cookies["ras_csrf"]})
    assert login.status_code == 200
    return client


def fixture(body: bool) -> bytes:
    out = io.BytesIO()
    document = canvas.Canvas(out, pagesize=(612, 792), pageCompression=0)
    document.drawString(35, 750, "Synthetic fixture")
    document.drawString(35, 730, "Abstract")
    document.drawString(35, 710, "Short abstract for parser demonstration.")
    document.showPage()
    if body:
        for heading in ("Introduction", "Methods"):
            document.drawString(35, 750, heading)
            for row in range(25):
                document.drawString(35, 730 - row * 24,
                    "A measured observation with sufficient text for page coverage.")
            document.showPage()
    document.drawString(35, 750, "References")
    document.drawString(35, 730, "[1] Example Author. Example Work. 2020.")
    document.showPage()
    document.save()
    return out.getvalue()


def upload(client: httpx.Client, raw: bytes) -> dict:
    notice = client.get("/api/uploads/notice").json()
    intent = client.post("/api/uploads/intents", json={"acknowledged": True,
        "notice_version": notice["version"], "file_kind": "pdf", "filename": "m2b-synthetic.pdf",
        "byte_size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
        headers={"origin": web, "x-csrf-token": client.cookies["ras_csrf"]})
    assert intent.status_code == 201, intent.status_code
    sent = client.put("/api/uploads/original", content=raw, headers={"origin": web,
        "x-csrf-token": client.cookies["ras_csrf"],
        "x-upload-intent": intent.json()["intent_token"], "content-type": "application/octet-stream"})
    assert sent.status_code == 201, sent.status_code
    return sent.json()


def wait(client: httpx.Client, ids: dict) -> dict:
    for _ in range(40):
        detail = client.get(f"/api/library/items/{ids['item_id']}").json()["item"]
        if detail["jobs"][0]["state"] in ("awaiting_index", "needs_review", "fulltext_rejected", "terminal_failed"):
            return detail
        time.sleep(1)
    raise AssertionError("worker did not finish within 40 seconds")


a, b, admin = browser("a"), browser("b"), browser("admin")
accepted, rejected = upload(a, fixture(True)), upload(a, fixture(False))
good, bad = wait(a, accepted), wait(a, rejected)
assert good["jobs"][0]["state"] == "awaiting_index"
assert good["content_status"] == "metadata_only"
assert good["jobs"][0]["finished_at"] is None
assert bad["jobs"][0]["state"] == "needs_review"
assert a.get(f"/api/files/{rejected['file_id']}/original").status_code == 200
coverage = a.get(f"/api/library/items/{accepted['item_id']}/coverage").json()
assert coverage["summary"][0]["body_pages"] == 2
refs = a.get(f"/api/library/items/{accepted['item_id']}/references").json()["references"]
assert len(refs) == 1
chunks = a.get(f"/api/library/items/{accepted['item_id']}/chunks?limit=100").json()["chunks"]
assert any(chunk["section_kind"] == "references" for chunk in chunks)
for other in (b, admin):
    for suffix in ("", "/chunks", "/coverage", "/references"):
        assert other.get(f"/api/library/items/{accepted['item_id']}{suffix}").status_code == 404

with engine.connect() as conn:
    attempts = conn.execute(text("SELECT step, round(extract(epoch FROM finished_at-started_at)*1000)::int"
        " FROM ingest_step_attempts WHERE job_id=:id ORDER BY started_at"),
        {"id": uuid.UUID(accepted["job_id"])}).all()
print({"accepted": good["jobs"][0]["state"], "rejected": bad["jobs"][0]["state"],
       "queue_wait_ms": good["jobs"][0]["queue_wait_ms"],
       "steps_ms": dict(attempts), "body_pages": coverage["summary"][0]["body_pages"],
       "references": len(refs), "acl": "A allowed, B/Admin 404"})
