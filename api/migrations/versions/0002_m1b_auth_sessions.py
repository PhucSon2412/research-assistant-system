"""M1b: invitations, server-side sessions, password reset tokens.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

Schema only; no rows are inserted and no M1a table is altered. Every token
column holds the SHA-256 hex digest of a 256-bit random token: the raw token
exists only in the delivered link or the browser cookie.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID_DEFAULT = sa.text("gen_random_uuid()")
SHA256_HEX = "~ '^[0-9a-f]{64}$'"


def _ts(name: str, nullable: bool = False) -> sa.Column:
    # No server default: the API writes these from its (testable) clock.
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("invited_by_user_id", sa.UUID(), nullable=False),
        _ts("created_at"),
        _ts("expires_at"),
        _ts("accepted_at", nullable=True),
        sa.Column("accepted_user_id", sa.UUID(), nullable=True),
        _ts("revoked_at", nullable=True),
        sa.CheckConstraint(
            "email = lower(btrim(email)) AND position('@' in email) > 1",
            name=op.f("ck_invitations_email_normalised"),
        ),
        sa.CheckConstraint(f"token_hash {SHA256_HEX}", name=op.f("ck_invitations_token_hash_sha256_hex")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_invitations_expires_after_created")),
        sa.CheckConstraint(
            "(accepted_at IS NULL) = (accepted_user_id IS NULL)",
            name=op.f("ck_invitations_accepted_fields_consistent"),
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR revoked_at IS NULL", name=op.f("ck_invitations_accepted_or_revoked")
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_invited_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_accepted_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
        sa.UniqueConstraint("accepted_user_id", name=op.f("uq_invitations_accepted_user_id")),
    )
    op.create_index(
        "uq_invitations_open_email",
        "invitations",
        ["email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token_hash", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("last_activity_at"),
        _ts("absolute_expires_at"),
        _ts("revoked_at", nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(f"token_hash {SHA256_HEX}", name=op.f("ck_sessions_token_hash_sha256_hex")),
        sa.CheckConstraint(
            f"csrf_token_hash {SHA256_HEX}", name=op.f("ck_sessions_csrf_token_hash_sha256_hex")
        ),
        sa.CheckConstraint(
            "absolute_expires_at > created_at", name=op.f("ck_sessions_expires_after_created")
        ),
        sa.CheckConstraint("last_activity_at >= created_at", name=op.f("ck_sessions_activity_after_created")),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)", name=op.f("ck_sessions_revoked_fields_consistent")
        ),
        sa.CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN"
            " ('logout', 'replaced', 'user_revoked', 'password_reset', 'user_disabled')",
            name=op.f("ck_sessions_revoked_reason_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("expires_at"),
        _ts("used_at", nullable=True),
        _ts("revoked_at", nullable=True),
        sa.CheckConstraint(
            f"token_hash {SHA256_HEX}", name=op.f("ck_password_reset_tokens_token_hash_sha256_hex")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_password_reset_tokens_expires_after_created")
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL", name=op.f("ck_password_reset_tokens_used_or_revoked")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_password_reset_tokens_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_reset_tokens_token_hash")),
    )
    op.create_index(
        "uq_password_reset_tokens_open_user_id",
        "password_reset_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("sessions")
    op.drop_table("invitations")
