"""Random bearer tokens (invitation, reset, session, CSRF).

Tokens carry 256 bits from ``secrets``; only their SHA-256 hex digest is
stored. A fast hash is sufficient because the input is high-entropy random
data, not a password, and it keeps lookups indexable.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_TOKEN_BYTES = 32
# token_urlsafe(32) is 43 characters; anything much longer is not ours.
MAX_TOKEN_LENGTH = 128


def generate_token() -> tuple[str, str]:
    """Return ``(raw, digest)``. Hand ``raw`` to the recipient, store ``digest``."""
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plausible_token(raw: str | None) -> bool:
    return bool(raw) and len(raw) <= MAX_TOKEN_LENGTH  # type: ignore[arg-type]


def digest_matches(raw: str, digest: str) -> bool:
    return hmac.compare_digest(hash_token(raw), digest)
