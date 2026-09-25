"""Allowed values for status/kind columns.

Stored as ``text`` with CHECK constraints instead of PostgreSQL ENUM types so
later migrations can add values without ``ALTER TYPE`` transaction limits.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SessionRevokeReason(StrEnum):
    LOGOUT = "logout"
    # Replaced by a new login from the same browser.
    REPLACED = "replaced"
    # The user ended one of their own sessions (or all others).
    USER_REVOKED = "user_revoked"
    PASSWORD_RESET = "password_reset"
    USER_DISABLED = "user_disabled"


class Visibility(StrEnum):
    COMMON = "common"
    PRIVATE = "private"


class ContentStatus(StrEnum):
    # No active full-text index: not eligible for full-text search/RAG.
    METADATA_ONLY = "metadata_only"
    INDEXED = "indexed"


class Coverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


class VerifiedMime(StrEnum):
    PDF = "application/pdf"
    MARKDOWN = "text/markdown"


class JobOrigin(StrEnum):
    DISCOVERY = "discovery"
    UPLOAD = "upload"
    PROMOTION = "promotion"


class JobState(StrEnum):
    RECEIVED = "received"
    VALIDATING = "validating"
    DUPLICATE = "duplicate"
    STORED = "stored"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    AWAITING_INDEX = "awaiting_index"
    INDEXING = "indexing"
    READY = "ready"
    FULLTEXT_REJECTED = "fulltext_rejected"
    NEEDS_REVIEW = "needs_review"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    CANCELLED = "cancelled"


class AuditActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


def sql_in(values: type[StrEnum]) -> str:
    """Render ``'a', 'b'`` for use inside a CHECK ... IN (...) clause."""
    return ", ".join(f"'{v.value}'" for v in values)
