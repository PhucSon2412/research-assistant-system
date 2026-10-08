"""Shared pieces of review approval (M6b-1) and verified original reads.

Approval itself runs in ``app.api.review_approval`` (C2d-5b): short proofs on
their own Sessions, a copy to a new object key verified by size and SHA-256
outside every DB lock, then one transaction that creates the pending common
item/version/file/AccessRights, closes the request ``approved``, writes the
audit row and the journal mirror. No index, job or provider call.

``verified_read`` is also the C2d-4c original download and publication read:
it streams a stored object, bounded by ``size``, into an optional spool and
reports whether the bytes match ``checksum``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import IO

from app.models.enums import RightsAudience
from app.storage import OriginalStorage

COPY_CHUNK_BYTES = 64 * 1024
COMMON_FILE_SOURCE = "review_approval"


@dataclass(frozen=True)
class RecordedRights:
    """The Admin's own findings; nothing here is taken from the User's claim."""

    title: str
    verified_source: str
    verified_source_url: str | None
    license_url: str | None
    evidence_note: str | None
    checked_on: date
    audience: RightsAudience
    permits_original_download: bool
    permits_fulltext_search: bool
    permits_external_processing: bool
    scope_conditions: str | None


def verified_read(storage: OriginalStorage, key: str, size: int, checksum: str, spool: IO[bytes] | None) -> bool:
    body = storage.open(key)
    digest, seen = hashlib.sha256(), 0
    try:
        for chunk in body.iter_chunks(chunk_size=COPY_CHUNK_BYTES):
            seen += len(chunk)
            if seen > size:
                return False
            digest.update(chunk)
            if spool is not None:
                spool.write(chunk)
    finally:
        body.close()
    return seen == size and digest.hexdigest() == checksum
