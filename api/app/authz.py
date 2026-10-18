"""Authorisation: what an authenticated principal may see or change.

Decisions depend on item ownership, not on role alone (docs/access-and-data.md):

- everyone sees ``listed`` ``common`` items and their own ``private`` items;
- nobody, Admin included, sees another user's private item through the
  normal API; the only exception is ``review_grant_clause``, used solely by
  the Admin review routes for the one version/file named in an open request;
- a ``pending`` common copy (M6b-1: rights recorded, not published) is absent
  from every normal route for everyone; only ``pending_publication_clause``
  lets an Admin read its metadata and rights on a dedicated route;
- a published (``listed``) reviewed copy is visible as *metadata* (M6b-2a).
  Its original opens only through ``common_original_clause`` (M6b-2b1): the
  exact file its live AccessRights and publication name, when those rights
  permit download without unenforced conditions. Its indexed text (chunks,
  coverage, references, metadata candidates, search hits) opens only through
  ``COMMON_FULLTEXT_SQL`` (M6b-2b3a): the active common generation of the exact
  version/file its live rights permit for full-text search. Neither grants the
  other. RAG (M6b-2b3b) retrieves through the same gate and sends a common
  chunk to generation only while those rights also permit external
  processing (``external_processing=True``). A ``revoked`` copy is
  absent from every normal route. ``review_copy_admin_clause`` is the
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

from sqlalchemy import ColumnElement, and_, false, or_, select, text, true, tuple_
from sqlalchemy.dialects.postgresql import UUID

from app.models import AccessRights, CommonPublication, LibraryItem, PaperVersion, ReviewRequest, SourceFile
from app.models.enums import PublicationState, ReviewStatus, RightsAudience, UserRole, Visibility

# The generation the common pipeline writes (``app.ingest.indexer`` imports it from here).
COMMON_INDEX_VERSION = "common-keyword-vector-v1"


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
            LibraryItem.deleted_at.is_(None),
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
        LibraryItem.deleted_at.is_(None),
    )


def common_fulltext_sql(*, external_processing: bool = False) -> str:
    """Uncorrelated SELECT of the common copies whose indexed text may be served now (M6b-2b3a).

    One row ``(item_id, version_id, source_file_id, generation_id)`` per copy,
    for the one version/file/generation that may be read; every derivative read
    (search keyword/vector/hydrate, chunk viewer, chunks, coverage, references,
    metadata candidates) matches its own row against it, so another version,
    file or generation of the same copy never matches. Together: the item is
    ``common``, ``listed``, ``indexed`` and has no tombstone; its active
    generation is ``active``, of ``COMMON_INDEX_VERSION``, has dimensions, is
    the item's active version, was built at the current ``rights_epoch`` by a
    promotion job of that same file and epoch; the item has exactly one
    AccessRights row and it is live, granted at the current epoch, names that
    item/version/file/checksum, lets ``instance_users`` (every signed-in
    account, Admin included, alike) use the full text and carries no
    ``scope_conditions``; the live publication names those rights, checksum
    and the file's size. This is the condition of ``ras_common_copy_index_block``
    (revision 0025), which is reapplied here instead of trusted, with
    ``permits_fulltext_search`` alone: search itself sends no paper text out.

    ``external_processing=True`` also requires ``permits_external_processing``:
    only those rows may reach a provider (rerank, RAG generation). ``permits_original_download``
    is never consulted: search does not open the original, nor the reverse.

    The subquery is uncorrelated so PostgreSQL computes it once per statement
    (a hashed subplan), not per candidate row; a revoke commits epoch, rights,
    publication and item state together, so any statement after ``t_rev`` sees
    no row for the copy even while its index rows await cleanup.
    """
    provider = " AND cr.permits_external_processing" if external_processing else ""
    return f"""SELECT ci.id AS item_id, cg.version_id, cg.source_file_id, cg.id AS generation_id
        FROM library_items ci
        JOIN index_generations cg ON cg.id = ci.active_index_generation_id AND cg.item_id = ci.id
        JOIN ingestion_jobs cj ON cj.id = cg.job_id AND cj.item_id = ci.id
        JOIN source_files cf ON cf.id = cg.source_file_id AND cf.version_id = cg.version_id AND cf.item_id = ci.id
        JOIN access_rights cr ON cr.item_id = ci.id AND cr.version_id = cg.version_id AND cr.source_file_id = cf.id
            AND cr.file_checksum_sha256 = cf.checksum_sha256
        JOIN common_publications cp ON cp.item_id = ci.id AND cp.access_rights_id = cr.id
            AND cp.file_checksum_sha256 = cr.file_checksum_sha256 AND cp.verified_byte_size = cf.byte_size
        WHERE ci.visibility = 'common' AND ci.publication_state = 'listed' AND ci.content_status = 'indexed'
          AND ci.active_version_id = cg.version_id
          AND cg.state = 'active' AND cg.index_version = '{COMMON_INDEX_VERSION}' AND cg.dimensions IS NOT NULL
          AND cg.rights_epoch = ci.rights_epoch
          AND cj.origin = 'promotion' AND cj.source_file_id = cg.source_file_id AND cj.version_id = cg.version_id
          AND cj.rights_epoch_at_start = ci.rights_epoch
          AND cr.revoked_at IS NULL AND cr.rights_epoch_at_grant = ci.rights_epoch
          AND cr.scope_audience = 'instance_users' AND cr.permits_fulltext_search
          AND cr.scope_conditions IS NULL{provider}
          AND cp.revoked_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM access_rights other WHERE other.item_id = ci.id AND other.id <> cr.id)
          AND NOT EXISTS (SELECT 1 FROM deletion_tombstones ct WHERE ct.item_id = ci.id)"""


def _common_fulltext(external_processing: bool = False):
    return (text(common_fulltext_sql(external_processing=external_processing))
            .columns(item_id=UUID(as_uuid=True), version_id=UUID(as_uuid=True),
                     source_file_id=UUID(as_uuid=True), generation_id=UUID(as_uuid=True))
            .subquery("common_fulltext"))


def common_fulltext_items_clause(principal: Principal, *, external_processing: bool = False) -> ColumnElement[bool]:
    """``LibraryItem`` is a common copy whose indexed text may be served now (``common_fulltext_sql``).

    ``external_processing=True``: its text may also reach a provider, so RAG can use it as evidence.
    Item-level only: callers reading derived rows must also match each row's
    version/file with ``common_derivatives_clause``.
    """
    rows = _common_fulltext(external_processing)
    return LibraryItem.id.in_(select(rows.c.item_id))


def common_derivatives_clause(principal: Principal, item_id, version_id, source_file_id) -> ColumnElement[bool]:
    """A derived row of a common copy (chunk, PDF page, reference, metadata candidate) that may be read now.

    Matches the row's own item/version/file against ``common_fulltext_sql``: rows
    of another version or file of the same copy stay closed. Never a widening of
    ``m2a_private_derivatives_clause``; callers combine the two with ``or_``.
    """
    rows = _common_fulltext()
    return tuple_(item_id, version_id, source_file_id).in_(
        select(rows.c.item_id, rows.c.version_id, rows.c.source_file_id))


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


def review_history_admin_clause(principal: Principal) -> ColumnElement[bool]:
    """Admin workflow history only; private payload still requires the exact grant.

    Open but inactive and closed requests retain their thin DTO and workflow
    count. This clause grants no private item/version/file/declaration reads.
    """
    return true() if principal.is_admin else false()


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
        LibraryItem.deleted_at.is_(None),
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
    if item.deleted_at is not None:
        return False
    if item.visibility == Visibility.COMMON.value:
        return principal.is_admin
    return item.owner_user_id == principal.user_id
