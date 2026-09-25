"""Authorisation: what an authenticated principal may see or change.

Decisions depend on item ownership, not on role alone (docs/access-and-data.md):

- everyone sees ``common`` items and their own ``private`` items;
- nobody, Admin included, sees another user's private item through the
  normal API (Admin review of a submitted version arrives with promotion);
- ``common`` metadata is edited by Admins, ``private`` metadata by its owner.

List/count queries embed ``visible_items_clause`` in SQL *before* ordering,
pagination and counting; direct access by ID re-applies it and answers
"not found" for both missing and forbidden items.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, or_

from app.models import LibraryItem
from app.models.enums import UserRole, Visibility


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    role: str
    session_id: uuid.UUID
    email: str
    display_name: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value


def visible_items_clause(principal: Principal) -> ColumnElement[bool]:
    return or_(
        LibraryItem.visibility == Visibility.COMMON.value,
        and_(
            LibraryItem.visibility == Visibility.PRIVATE.value,
            LibraryItem.owner_user_id == principal.user_id,
        ),
    )


def m2a_private_derivatives_clause(principal: Principal) -> ColumnElement[bool]:
    """M2a full-text read gate; common needs per-file-version rights in M3+.

    Metadata visibility alone does not grant access to extracted text. M3
    must add a common branch joined to each derivative row's exact version
    and file with verified AccessRights before returning common derivatives.
    """
    return and_(
        LibraryItem.visibility == Visibility.PRIVATE.value,
        LibraryItem.owner_user_id == principal.user_id,
    )


def can_edit_item(principal: Principal, item: LibraryItem) -> bool:
    if item.visibility == Visibility.COMMON.value:
        return principal.is_admin
    return item.owner_user_id == principal.user_id
