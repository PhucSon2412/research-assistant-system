"""Domain errors raised by identity services; the API maps them to responses.

Messages never contain passwords, tokens or the looked-up email address.
"""

from __future__ import annotations


class InvalidCredentials(Exception):
    """Unknown account, wrong password or disabled account (indistinguishable)."""


class InvalidToken(Exception):
    """Invitation/reset token unknown, expired, revoked or already used."""


class AccountExists(Exception):
    """An account already uses this email (only reported to Admins)."""


class InvitationConflict(Exception):
    """A concurrent invitation for the same email won the race."""


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass
