"""Password verifiers: Argon2id PHC strings via argon2-cffi.

The PHC string embeds algorithm, parameters and salt, so M1b login can call
``needs_rehash`` and upgrade parameters without a schema change.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import PasswordPolicy

# argon2-cffi defaults follow RFC 9106's low-memory profile (Argon2id).
_hasher = PasswordHasher()


class PasswordPolicyError(ValueError):
    """Raised with a message that never contains the password itself."""


def validate_password(password: str, policy: PasswordPolicy, *, email: str | None = None) -> None:
    if len(password) < policy.min_length:
        raise PasswordPolicyError(f"password must be at least {policy.min_length} characters")
    if len(password) > policy.max_length:
        raise PasswordPolicyError(f"password must be at most {policy.max_length} characters")
    if not password.strip():
        raise PasswordPolicyError("password must not be blank")
    if email is not None and password.strip().lower() == email.strip().lower():
        raise PasswordPolicyError("password must not equal the email address")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(verifier: str, password: str) -> bool:
    try:
        return _hasher.verify(verifier, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(verifier: str) -> bool:
    return _hasher.check_needs_rehash(verifier)
