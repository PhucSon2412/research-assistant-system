"""Single time source for auth decisions, overridable in tests.

Expiry checks compare against this clock (not the database ``now()``), so
boundary tests can move time exactly to 30 minutes idle or 8 hours absolute.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
