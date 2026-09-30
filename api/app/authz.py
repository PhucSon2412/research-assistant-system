"""Authorisation: what an authenticated principal may see or change.

Decisions depend on item ownership, not on role alone (docs/access-and-data.md):

- everyone sees ``listed`` ``common`` items and their own ``private`` items;
- nobody, Admin included, sees another user's private item through the
  normal API; the only exception is ``review_grant_clause``, used solely by
  the Admin review routes for the one version/file named in an open request;
- a ``pending`` common copy (M6b-1: rights recorded, not published) is absent
  from every normal route for everyone; only ``pending_publication_clause``
  lets an Admin read its metadata and rights on a dedicated route;
- a published (``listed``) reviewed copy is visible as *metadata* (M6b-2a);
  chunks, search and RAG still go through the private-owner gates, so none of
  its text opens. Its original opens only through ``common_original_clause``
  (M6b-2b1): the exact file its live AccessRights and publication name, when
  those rights permit download without unenforced conditions. A ``revoked``
  copy is absent from every normal route. ``review_copy_admin_clause`` is the
  Admin-only view of reviewed copies in any state, used by
  ``/admin/common-copies``;
- ``common`` metadata is edited by Admins, ``private`` metadata by its owner.

List/count queries embed ``visible_items_clause`` in SQL *before* ordering,
pagination and counting; direct access by ID re-applies it and answers
"not found" for both missing and forbidden items.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, false, or_, select

from app.models import AccessRights, CommonPublication, LibraryItem, PaperVersion, ReviewRequest, SourceFile
from app.models.enums import PublicationState, ReviewStatus, RightsAudience, UserRole, Visibility


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
    # A pending common copy (recorded rights, not published) is invisible to
    # everyone here, Admin included; only pending_publication_clause opens it.
    return or_(
        and_(
            LibraryItem.visibility == Visibility.COMMON.value,
            LibraryItem.publication_state == PublicationState.LISTED.value,
        ),
        and_(
            LibraryItem.visibility == Visibility.PRIVATE.value,
            LibraryItem.owner_user_id == principal.user_id,
        ),
    )


def m2a_private_derivatives_clause(principal: Principal) -> ColumnElement[bool]:
    """M2a full-text read gate: private items of their owner only.

    Metadata visibility alone does not grant access to extracted text. A common
    branch for derivatives must be a separate clause joined to each row's exact
    version and file with verified AccessRights (as ``common_original_clause``
    does for originals), never a widening of this one.
    """
    return and_(
        LibraryItem.visibility == Visibility.PRIVATE.value,
        LibraryItem.owner_user_id == principal.user_id,
    )


def common_original_clause(principal: Principal) -> ColumnElement[bool]:
    """Read of one published common copy's original under its AccessRights (M6b-2b1).

    The outer query must select from ``SourceFile``, ``PaperVersion`` and
    ``LibraryItem``. Matches only when, together: the item is ``common`` and
    ``listed``; its publication is live and names these rights and checksum,
    with the verified size of this file; the rights are live, name exactly
    this item/version/file/checksum, were granted at the item's current
    ``rights_epoch``, permit original download to ``instance_users`` (every
    signed-in account, Admin included) and carry **no** ``scope_conditions``:
    that text is not machine-enforced, so a copy with conditions stays closed
    until a structured, enforceable rule exists. ``content_status`` is not
    consulted: ``metadata_only`` still keeps search/RAG closed, not download.
    Nothing here opens chunks, search, RAG or another version/file.
    """
    live_rights = (
        select(AccessRights.id)
        .join(CommonPublication, and_(CommonPublication.access_rights_id == AccessRights.id,
                                      CommonPublication.item_id == AccessRights.item_id,
                                      CommonPublication.file_checksum_sha256 == AccessRights.file_checksum_sha256))
        .where(
            AccessRights.item_id == LibraryItem.id,
            AccessRights.version_id == SourceFile.version_id,
            AccessRights.source_file_id == SourceFile.id,
            AccessRights.file_checksum_sha256 == SourceFile.checksum_sha256,
            AccessRights.revoked_at.is_(None),
            AccessRights.rights_epoch_at_grant == LibraryItem.rights_epoch,
            AccessRights.permits_original_download.is_(True),
            AccessRights.scope_audience == RightsAudience.INSTANCE_USERS.value,
            AccessRights.scope_conditions.is_(None),
            CommonPublication.revoked_at.is_(None),
            CommonPublication.verified_byte_size == SourceFile.byte_size,
        )
        .correlate(LibraryItem, SourceFile)
        .exists()
    )
    return and_(
        LibraryItem.visibility == Visibility.COMMON.value,
        LibraryItem.publication_state == PublicationState.LISTED.value,
        SourceFile.item_id == LibraryItem.id,
        PaperVersion.id == SourceFile.version_id,
        PaperVersion.item_id == LibraryItem.id,
        live_rights,
    )


def review_grant_clause(principal: Principal) -> ColumnElement[bool]:
    """Temporary Admin read of one private original named by an open request.

    Joins ``ReviewRequest`` to exactly its ``SourceFile``/``PaperVersion``/
    ``LibraryItem``: another version or file of the same item never matches.
    It ends when the request closes or the item's rights epoch moves (a later
    revoke/delete), and grants nothing for chunks, search, RAG or collections.
    """
    if not principal.is_admin:
        return false()
    return and_(
        ReviewRequest.status == ReviewStatus.OPEN.value,
        ReviewRequest.source_file_id == SourceFile.id,
        ReviewRequest.version_id == SourceFile.version_id,
        ReviewRequest.version_id == PaperVersion.id,
        ReviewRequest.item_id == PaperVersion.item_id,
        ReviewRequest.item_id == LibraryItem.id,
        ReviewRequest.file_checksum_sha256 == SourceFile.checksum_sha256,
        LibraryItem.visibility == Visibility.PRIVATE.value,
        LibraryItem.owner_user_id == ReviewRequest.requester_user_id,
        LibraryItem.rights_epoch == ReviewRequest.rights_epoch_at_submit,
    )


def pending_publication_clause(principal: Principal) -> ColumnElement[bool]:
    """Admin-only read of common copies approved from a review, not yet published.

    Used solely by ``/admin/pending-publications``. It opens metadata and the
    recorded AccessRights, never file bytes, chunks, search or RAG.
    """
    if not principal.is_admin:
        return false()
    return and_(
        LibraryItem.visibility == Visibility.COMMON.value,
        LibraryItem.publication_state == PublicationState.PENDING.value,
    )


def review_copy_admin_clause(principal: Principal) -> ColumnElement[bool]:
    """Admin-only view of common copies created from a review, in any state.

    Used solely by ``/admin/common-copies`` (publish, revoke, cleanup status).
    It opens metadata and recorded rights, never file bytes, chunks or index.
    Common items without AccessRights (pre-M6b-1 ``listed``) never match.
    """
    if not principal.is_admin:
        return false()
    return and_(
        LibraryItem.visibility == Visibility.COMMON.value,
        # Correlate only the item: callers may also join AccessRights in the outer query.
        select(AccessRights.id).where(AccessRights.item_id == LibraryItem.id).correlate_except(AccessRights).exists(),
    )


def can_edit_item(principal: Principal, item: LibraryItem) -> bool:
    if item.visibility == Visibility.COMMON.value:
        return principal.is_admin
    return item.owner_user_id == principal.user_id
