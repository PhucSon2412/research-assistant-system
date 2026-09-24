from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


class EmailFormatError(ValueError):
    pass


def normalise_email(raw: str) -> str:
    """Lower-cased, trimmed form stored in ``users.email``."""
    email = raw.strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise EmailFormatError("invalid email address")
    return email
