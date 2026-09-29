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


class PublicationState(StrEnum):
    """Common items only (private items keep NULL).

    ``listed``: metadata visible to every User, the behaviour common items had
    before M6b-1; it says nothing about full-text rights. ``pending``: a common
    copy with Admin-recorded AccessRights that is not published yet; no normal
    route lists, counts or opens it.
    """

    LISTED = "listed"
    PENDING = "pending"


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


class ReviewStatus(StrEnum):
    # Only an open request grants the Admin a temporary review read.
    OPEN = "open"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"
    # Rights recorded and a pending common copy created; not a publication.
    APPROVED = "approved"


class ReviewRejectReason(StrEnum):
    RIGHTS_NOT_ESTABLISHED = "rights_not_established"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    WRONG_FILE_OR_VERSION = "wrong_file_or_version"
    OUT_OF_SCOPE = "out_of_scope"
    OTHER = "other"


class RightsOrigin(StrEnum):
    REVIEW_REQUEST = "review_request"


class RightsAudience(StrEnum):
    # Every invited account of this instance; no public/anonymous audience.
    INSTANCE_USERS = "instance_users"


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
