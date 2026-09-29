"""Dev-only HTTP demo through the live Next.js proxy and Compose stack.

Creates disposable fixture accounts and one common metadata item directly in
the development DB. It does not use existing accounts or print credentials.
Run with the Conda ras Python from repo root after Compose is healthy.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values
from pypdf import PdfWriter
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from app.identity.passwords import hash_password

ROOT = Path(__file__).resolve().parents[1]
env = dotenv_values(ROOT / ".env")
db_url = URL.create(
    "postgresql+psycopg", username=env["POSTGRES_USER"], password=env["POSTGRES_PASSWORD"],
    host="127.0.0.1", port=int(env.get("POSTGRES_HOST_PORT") or "5432"), database=env["POSTGRES_DB"],
)
web = f"http://127.0.0.1:{env.get('WEB_HOST_PORT') or '3000'}"
marker = uuid.uuid4().hex[:10]
passwords = {role: secrets.token_urlsafe(24) for role in ("admin", "a", "b")}
emails = {role: f"m1c-{role}-{marker}@example.org" for role in passwords}

engine = create_engine(db_url, hide_parameters=True)
with engine.begin() as conn:
    for role in passwords:
        conn.execute(text("INSERT INTO users (email, role, password_verifier) VALUES (:e, :r, :p)"),
                     {"e": emails[role], "r": "admin" if role == "admin" else "user",
                      "p": hash_password(passwords[role])})
    common = conn.scalar(text("INSERT INTO library_items (visibility, publication_state, title) VALUES ('common', 'listed', 'demo common') RETURNING id"))


def browser(role: str) -> httpx.Client:
    client = httpx.Client(base_url=web, timeout=180)
    assert client.get("/api/auth/csrf").status_code == 200
    response = client.post("/api/auth/login", json={"email": emails[role], "password": passwords[role]},
                           headers={"origin": web, "x-csrf-token": client.cookies["ras_csrf"]})
    assert response.status_code == 200, response.text
    return client


admin, a, b = (browser(role) for role in ("admin", "a", "b"))
updated = admin.patch(f"/api/library/items/{common}", json={"title": "Common metadata managed by Admin"},
                      headers={"origin": web, "x-csrf-token": admin.cookies["ras_csrf"]})
assert updated.status_code == 200, updated.text

writer = PdfWriter()
writer.add_blank_page(width=612, height=792)
writer.add_metadata({"/Padding": "x" * (29 * 1024 * 1024)})
out = io.BytesIO()
writer.write(out)
pdf = out.getvalue()
assert 28 * 1024 * 1024 < len(pdf) < 30 * 1024 * 1024
digest = hashlib.sha256(pdf).hexdigest()
notice = a.get("/api/uploads/notice").json()
created = a.post("/api/uploads/intents", json={
    "acknowledged": True, "notice_version": notice["version"], "file_kind": "pdf",
    "filename": "m1c-demo.pdf", "byte_size": len(pdf), "sha256": digest,
}, headers={"origin": web, "x-csrf-token": a.cookies["ras_csrf"]})
assert created.status_code == 201, created.text
token = created.json()["intent_token"]


def blocks():
    for start in range(0, len(pdf), 64 * 1024):
        yield pdf[start:start + 64 * 1024]


uploaded = a.put("/api/uploads/original", content=blocks(), headers={
    "origin": web, "x-csrf-token": a.cookies["ras_csrf"],
    "x-upload-intent": token, "content-type": "application/octet-stream",
})
assert uploaded.status_code == 201, uploaded.text
result = uploaded.json()
item_id, file_id = result["item_id"], result["file_id"]
assert a.get(f"/api/library/items/{item_id}").status_code == 200
with a.stream("GET", f"/api/files/{file_id}/original") as response:
    assert response.status_code == 200
    received = hashlib.sha256()
    size = 0
    for chunk in response.iter_bytes():
        received.update(chunk)
        size += len(chunk)
assert size == len(pdf) and received.hexdigest() == digest
for other in (b, admin):
    assert item_id not in str(other.get("/api/library/items").json())
    assert other.get(f"/api/library/items/{item_id}").status_code == 404
    assert other.get(f"/api/files/{file_id}/original").status_code == 404
assert b.get(f"/api/files/{uuid.uuid4()}/original").status_code == 404
send_again = a.put("/api/uploads/original", content=b"x", headers={
    "origin": web, "x-csrf-token": a.cookies["ras_csrf"],
    "x-upload-intent": token, "content-type": "application/octet-stream",
})
assert send_again.status_code == 404
print(f"M1c stack demo passed: PDF {len(pdf)} bytes, private item {item_id}; A download checksum matched; B/Admin denied; Admin edited common metadata")
