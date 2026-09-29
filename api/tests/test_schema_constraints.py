"""Database-level invariants of the M1a schema.

Each test runs inside a transaction that is rolled back; expected failures
run inside a SAVEPOINT so the outer transaction stays usable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.identity.passwords import hash_password

VERIFIER = hash_password("correct horse battery staple")
CHECKSUM = "a" * 64


@pytest.fixture
def conn(shared_migrated_engine: Engine) -> Iterator[Connection]:
    with shared_migrated_engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()


def expect_error(conn: Connection, sql: str, params: dict | None = None, exc=IntegrityError) -> str:
    with pytest.raises(exc) as info:
        with conn.begin_nested():
            conn.execute(text(sql), params or {})
    return str(info.value.orig)


def add_user(conn: Connection, email: str | None = None, role: str = "user") -> uuid.UUID:
    return conn.scalar(
        text("INSERT INTO users (email, role, password_verifier) VALUES (:e, :r, :v) RETURNING id"),
        {"e": email or f"u{uuid.uuid4().hex[:8]}@example.org", "r": role, "v": VERIFIER},
    )


def add_item(conn: Connection, owner: uuid.UUID | None) -> uuid.UUID:
    visibility = "private" if owner else "common"
    return conn.scalar(
        text("INSERT INTO library_items (visibility, owner_user_id, publication_state) VALUES (:v, :o, :p)"
             " RETURNING id"),
        {"v": visibility, "o": owner, "p": None if owner else "listed"},
    )


def add_version(conn: Connection, item: uuid.UUID) -> uuid.UUID:
    return conn.scalar(text("INSERT INTO paper_versions (item_id) VALUES (:i) RETURNING id"), {"i": item})


def add_job(conn: Connection, *, owner: uuid.UUID | None, item=None, version=None, key=None, origin=None):
    return conn.scalar(
        text(
            "INSERT INTO ingestion_jobs (origin, scope_visibility, scope_owner_user_id, item_id, version_id,"
            " idempotency_key) VALUES (:origin, :vis, :owner, :item, :version, :key) RETURNING id"
        ),
        {
            "origin": origin or ("upload" if owner else "discovery"),
            "vis": "private" if owner else "common",
            "owner": owner,
            "item": item,
            "version": version,
            "key": key or uuid.uuid4().hex,
        },
    )


# --- users -------------------------------------------------------------------


def test_plaintext_password_cannot_be_stored(conn: Connection) -> None:
    msg = expect_error(
        conn, "INSERT INTO users (email, password_verifier) VALUES ('p@example.org', 'hunter2hunter2')"
    )
    assert "ck_users_password_verifier_argon2id" in msg


def test_email_is_unique_case_insensitively(conn: Connection) -> None:
    add_user(conn, "alice@example.org")
    assert "uq_users_email" in expect_error(
        conn, "INSERT INTO users (email, password_verifier) VALUES ('alice@example.org', :v)", {"v": VERIFIER}
    )
    # Non-normalised spelling is rejected outright, so it cannot sidestep the unique key.
    assert "ck_users_email_normalised" in expect_error(
        conn, "INSERT INTO users (email, password_verifier) VALUES ('Alice@Example.org', :v)", {"v": VERIFIER}
    )


def test_role_and_status_are_constrained(conn: Connection) -> None:
    assert "ck_users_role_valid" in expect_error(
        conn, "INSERT INTO users (email, role, password_verifier) VALUES ('r@example.org', 'root', :v)",
        {"v": VERIFIER},
    )
    assert "ck_users_status_valid" in expect_error(
        conn, "INSERT INTO users (email, status, password_verifier) VALUES ('s@example.org', 'x', :v)",
        {"v": VERIFIER},
    )


def test_new_user_defaults_to_non_admin(conn: Connection) -> None:
    uid = conn.scalar(
        text("INSERT INTO users (email, password_verifier) VALUES ('d@example.org', :v) RETURNING id"),
        {"v": VERIFIER},
    )
    assert conn.scalar(text("SELECT role FROM users WHERE id = :i"), {"i": uid}) == "user"


# --- bootstrap marker --------------------------------------------------------


def test_bootstrap_state_is_a_singleton(conn: Connection) -> None:
    a, b = add_user(conn), add_user(conn)
    conn.execute(text("INSERT INTO bootstrap_state (admin_user_id) VALUES (:a)"), {"a": a})
    assert "pk_bootstrap_state" in expect_error(
        conn, "INSERT INTO bootstrap_state (admin_user_id) VALUES (:b)", {"b": b}
    )
    assert "ck_bootstrap_state_singleton" in expect_error(
        conn, "INSERT INTO bootstrap_state (id, admin_user_id) VALUES (false, :b)", {"b": b}
    )


# --- library items / versions ------------------------------------------------


def test_private_item_requires_owner_and_common_forbids_one(conn: Connection) -> None:
    owner = add_user(conn)
    assert "ck_library_items_owner_matches_visibility" in expect_error(
        conn, "INSERT INTO library_items (visibility) VALUES ('private')"
    )
    assert "ck_library_items_owner_matches_visibility" in expect_error(
        conn, "INSERT INTO library_items (visibility, owner_user_id, publication_state) VALUES ('common', :o,"
        " 'listed')", {"o": owner}
    )


def test_item_defaults_to_metadata_only(conn: Connection) -> None:
    item = add_item(conn, add_user(conn))
    assert conn.scalar(text("SELECT content_status FROM library_items WHERE id = :i"), {"i": item}) == (
        "metadata_only"
    )


def test_item_visibility_and_owner_are_immutable(conn: Connection) -> None:
    alice, bob = add_user(conn), add_user(conn)
    item = add_item(conn, alice)
    for sql in (
        "UPDATE library_items SET owner_user_id = :b WHERE id = :i",
        "UPDATE library_items SET visibility = 'common', owner_user_id = NULL WHERE id = :i",
    ):
        msg = expect_error(conn, sql, {"b": bob, "i": item})
        assert "visibility/owner is immutable" in msg
    # Other columns stay editable.
    conn.execute(text("UPDATE library_items SET title = 't' WHERE id = :i"), {"i": item})


def test_indexed_requires_active_version(conn: Connection) -> None:
    item = add_item(conn, None)
    assert "ck_library_items_indexed_requires_active_version" in expect_error(
        conn, "UPDATE library_items SET content_status = 'indexed' WHERE id = :i", {"i": item}
    )
    version = add_version(conn, item)
    conn.execute(
        text("UPDATE library_items SET active_version_id = :v, content_status = 'indexed' WHERE id = :i"),
        {"v": version, "i": item},
    )


def test_active_version_must_belong_to_item(conn: Connection) -> None:
    item_a, item_b = add_item(conn, None), add_item(conn, None)
    version_b = add_version(conn, item_b)
    assert "fk_library_items_active_version_paper_versions" in expect_error(
        conn, "UPDATE library_items SET active_version_id = :v WHERE id = :i", {"v": version_b, "i": item_a}
    )


# --- ingestion jobs ----------------------------------------------------------


def test_job_version_must_belong_to_job_item(conn: Connection) -> None:
    item_a, item_b = add_item(conn, None), add_item(conn, None)
    version_b = add_version(conn, item_b)
    with pytest.raises(IntegrityError) as info, conn.begin_nested():
        add_job(conn, owner=None, item=item_a, version=version_b)
    assert "fk_ingestion_jobs_version_paper_versions" in str(info.value.orig)


def test_job_scope_must_match_item_scope(conn: Connection) -> None:
    alice, bob = add_user(conn), add_user(conn)
    bob_item = add_item(conn, bob)
    # Alice's private-scope job cannot point at Bob's private item.
    with pytest.raises(IntegrityError) as info, conn.begin_nested():
        add_job(conn, owner=alice, item=bob_item)
    assert "scope does not match" in str(info.value.orig)
    # A common-scope job cannot point at a private item.
    with pytest.raises(IntegrityError), conn.begin_nested():
        add_job(conn, owner=None, item=bob_item, origin="discovery")
    # Retargeting an existing job is checked too.
    job = add_job(conn, owner=bob, item=bob_item)
    other = add_item(conn, alice)
    expect_error(conn, "UPDATE ingestion_jobs SET item_id = :o WHERE id = :j", {"o": other, "j": job})


def test_upload_jobs_are_private(conn: Connection) -> None:
    assert "ck_ingestion_jobs_upload_is_private" in expect_error(
        conn,
        "INSERT INTO ingestion_jobs (origin, scope_visibility, idempotency_key) VALUES ('upload', 'common', 'k')",
    )


def test_idempotency_key_is_unique_per_scope(conn: Connection) -> None:
    alice, bob = add_user(conn), add_user(conn)
    add_job(conn, owner=alice, key="same")
    add_job(conn, owner=bob, key="same")  # different owner scope: allowed
    add_job(conn, owner=None, key="same")  # common scope: allowed once
    for owner in (alice, None):  # NULLS NOT DISTINCT covers the common scope
        with pytest.raises(IntegrityError) as info, conn.begin_nested():
            add_job(conn, owner=owner, key="same")
        assert "uq_ingestion_jobs_scope_idempotency_key" in str(info.value.orig)


def test_job_state_and_cancel_flags_are_consistent(conn: Connection) -> None:
    job = add_job(conn, owner=None)
    assert conn.scalar(text("SELECT state FROM ingestion_jobs WHERE id = :j"), {"j": job}) == "received"
    assert "ck_ingestion_jobs_state_valid" in expect_error(
        conn, "UPDATE ingestion_jobs SET state = 'done' WHERE id = :j", {"j": job}
    )
    assert "ck_ingestion_jobs_cancel_requested_at_consistent" in expect_error(
        conn, "UPDATE ingestion_jobs SET cancel_requested = true WHERE id = :j", {"j": job}
    )
    conn.execute(
        text("UPDATE ingestion_jobs SET cancel_requested = true, cancel_requested_at = now() WHERE id = :j"),
        {"j": job},
    )


# --- source files ------------------------------------------------------------

INSERT_FILE = (
    "INSERT INTO source_files (job_id, storage_key, verified_mime, byte_size, checksum_sha256)"
    " VALUES (:job, :key, :mime, :size, :sum) RETURNING id"
)


def _file_params(job: uuid.UUID, **overrides) -> dict:
    params = {"job": job, "key": f"originals/{uuid.uuid4()}", "mime": "application/pdf", "size": 10, "sum": CHECKSUM}
    params.update(overrides)
    return params


def test_source_file_field_constraints(conn: Connection) -> None:
    job = add_job(conn, owner=None)
    assert "ck_source_files_checksum_sha256_hex" in expect_error(conn, INSERT_FILE, _file_params(job, sum="XYZ"))
    assert "ck_source_files_verified_mime_valid" in expect_error(
        conn, INSERT_FILE, _file_params(job, mime="application/zip")
    )
    assert "ck_source_files_byte_size_positive" in expect_error(conn, INSERT_FILE, _file_params(job, size=0))
    assert "ck_source_files_has_owner_record" in expect_error(conn, INSERT_FILE, _file_params(None))


def test_source_file_is_immutable_except_fill_once_fields(conn: Connection) -> None:
    job = add_job(conn, owner=None)
    file_id = conn.scalar(text(INSERT_FILE), _file_params(job))
    for column, value in (("checksum_sha256", "b" * 64), ("storage_key", "other"), ("byte_size", 11)):
        msg = expect_error(
            conn, f"UPDATE source_files SET {column} = :v WHERE id = :f", {"v": value, "f": file_id}
        )
        assert "immutable" in msg
    conn.execute(text("UPDATE source_files SET page_count = 3 WHERE id = :f"), {"f": file_id})
    assert "immutable" in expect_error(conn, "UPDATE source_files SET page_count = 4 WHERE id = :f", {"f": file_id})


# --- audit -------------------------------------------------------------------


def test_audit_events_are_append_only(conn: Connection) -> None:
    event = conn.scalar(
        text(
            "INSERT INTO audit_events (actor_type, action, outcome) VALUES ('system', 'test.event', 'success')"
            " RETURNING id"
        )
    )
    for sql in ("UPDATE audit_events SET outcome = 'denied' WHERE id = :e", "DELETE FROM audit_events WHERE id = :e"):
        msg = expect_error(conn, sql, {"e": event}, exc=DBAPIError)
        assert "append-only" in msg
    assert "append-only" in expect_error(conn, "TRUNCATE audit_events", exc=DBAPIError)


def test_audit_actor_type_matches_actor(conn: Connection) -> None:
    assert "ck_audit_events_actor_user_matches_type" in expect_error(
        conn, "INSERT INTO audit_events (actor_type, action, outcome) VALUES ('user', 'x', 'success')"
    )


# --- M1b auth tables -----------------------------------------------------------

DIGEST = "b" * 64
INSERT_INVITATION = (
    "INSERT INTO invitations (email, token_hash, invited_by_user_id, created_at, expires_at)"
    " VALUES (:e, :h, :i, now(), now() + interval '72 hours') RETURNING id"
)
INSERT_SESSION = (
    "INSERT INTO sessions (user_id, token_hash, csrf_token_hash, created_at, last_activity_at,"
    " absolute_expires_at) VALUES (:u, :h, :d, now(), now(), now() + interval '8 hours') RETURNING id"
)
INSERT_RESET = (
    "INSERT INTO password_reset_tokens (user_id, token_hash, created_at, expires_at)"
    " VALUES (:u, :h, now(), now() + interval '30 minutes')"
)


def add_invitation(conn: Connection, inviter: uuid.UUID, email: str) -> uuid.UUID:
    return conn.scalar(text(INSERT_INVITATION), {"e": email, "h": uuid.uuid4().hex * 2, "i": inviter})


def test_token_columns_accept_only_sha256_digests(conn: Connection) -> None:
    admin = add_user(conn, role="admin")
    raw = "Zm9vYmFyLXJhdy10b2tlbi12YWx1ZS1ub3QtYS1kaWdlc3Q"  # shaped like a raw urlsafe token
    assert "ck_invitations_token_hash_sha256_hex" in expect_error(
        conn, INSERT_INVITATION, {"e": "x@example.org", "h": raw, "i": admin}
    )
    assert "ck_sessions_token_hash_sha256_hex" in expect_error(conn, INSERT_SESSION, {"u": admin, "h": raw, "d": DIGEST})
    assert "ck_password_reset_tokens_token_hash_sha256_hex" in expect_error(conn, INSERT_RESET, {"u": admin, "h": raw})


def test_one_open_invitation_per_email(conn: Connection) -> None:
    admin = add_user(conn, role="admin")
    first = add_invitation(conn, admin, "inv@example.org")
    with pytest.raises(IntegrityError) as info, conn.begin_nested():
        add_invitation(conn, admin, "inv@example.org")
    assert "uq_invitations_open_email" in str(info.value.orig)
    conn.execute(text("UPDATE invitations SET revoked_at = now() WHERE id = :i"), {"i": first})
    add_invitation(conn, admin, "inv@example.org")


def test_invitation_state_consistency(conn: Connection) -> None:
    admin, user = add_user(conn, role="admin"), add_user(conn)
    inv = add_invitation(conn, admin, "state@example.org")
    assert "ck_invitations_accepted_fields_consistent" in expect_error(
        conn, "UPDATE invitations SET accepted_at = now() WHERE id = :i", {"i": inv}
    )
    assert "ck_invitations_accepted_or_revoked" in expect_error(
        conn,
        "UPDATE invitations SET accepted_at = now(), accepted_user_id = :u, revoked_at = now() WHERE id = :i",
        {"i": inv, "u": user},
    )
    assert "ck_invitations_email_normalised" in expect_error(
        conn, INSERT_INVITATION, {"e": "Mixed@Example.org", "h": DIGEST, "i": admin}
    )


def test_session_revocation_needs_known_reason(conn: Connection) -> None:
    user = add_user(conn)
    sid = conn.scalar(text(INSERT_SESSION), {"u": user, "h": DIGEST, "d": DIGEST})
    assert "ck_sessions_revoked_fields_consistent" in expect_error(
        conn, "UPDATE sessions SET revoked_at = now() WHERE id = :s", {"s": sid}
    )
    assert "ck_sessions_revoked_reason_valid" in expect_error(
        conn, "UPDATE sessions SET revoked_at = now(), revoked_reason = 'whim' WHERE id = :s", {"s": sid}
    )
    conn.execute(text("UPDATE sessions SET revoked_at = now(), revoked_reason = 'logout' WHERE id = :s"), {"s": sid})


def test_one_open_reset_token_per_user(conn: Connection) -> None:
    user = add_user(conn)
    conn.execute(text(INSERT_RESET), {"u": user, "h": "c" * 64})
    assert "uq_password_reset_tokens_open_user_id" in expect_error(conn, INSERT_RESET, {"u": user, "h": "d" * 64})
