"""Common gate rows and shared verified spool/response ownership.

The two GET boundaries live in api.original_downloads. All three permission
branches share this per-process pool. No authorization or audit proof crosses
storage or streaming; finalizers are fallback only.
"""

from __future__ import annotations

import tempfile
import asyncio
import threading
import weakref
from functools import lru_cache
from typing import IO

from fastapi.responses import StreamingResponse

from sqlalchemy import Select, select

from app.authz import Principal, common_original_clause
from app.models import LibraryItem, PaperVersion, SourceFile

STREAM_CHUNK_BYTES = 64 * 1024
ACTION = "common_copy.original.download"


def common_original_rows(principal: Principal) -> Select:
    return (select(SourceFile, LibraryItem.rights_epoch)
            .join(PaperVersion, SourceFile.version_id == PaperVersion.id)
            .join(LibraryItem, PaperVersion.item_id == LibraryItem.id)
            .where(common_original_clause(principal)))


@lru_cache
def _slots(limit: int) -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(limit)


class _Hold:
    """One spool slot and the temp file it pays for; released exactly once."""

    def __init__(self, slots: threading.BoundedSemaphore):
        self.slots, self.spool, self.done = slots, None, False
        self.lock = threading.Lock()

    def release(self) -> None:
        with self.lock:
            if self.done:
                return
            self.done = True
        try:
            if self.spool is not None:
                try:
                    self.spool.close()
                except BaseException:
                    # Try the buffered owner and raw handle before giving up;
                    # never hide the original teardown error with file bytes.
                    try:
                        self.spool.close()
                    except BaseException:
                        raw = getattr(self.spool, "raw", None)
                        if raw is not None:
                            raw.close()
                    raise
        finally:
            self.slots.release()


class VerifiedOriginal:
    """Bytes already proved, in a temp file that holds one spool slot.

    An iterator object rather than a generator: ``close()`` frees the file and
    the slot even when nothing was read yet (the ``finally`` of a generator
    that never started does not run). ``VerifiedFileResponse`` calls it however
    the response ends; a finalizer frees them if the object is dropped unclosed.
    """

    def __init__(self, slots: threading.BoundedSemaphore):
        self._hold = _Hold(slots)
        self._finalizer = weakref.finalize(self, self._hold.release)
        self._started = False

    def open_spool(self) -> IO[bytes]:
        self._hold.spool = tempfile.TemporaryFile(prefix="ras-verified-original-")
        return self._hold.spool

    @property
    def closed(self) -> bool:
        return self._hold.done

    def close(self) -> None:
        self._finalizer()  # runs _Hold.release at most once, then detaches

    def __iter__(self) -> "VerifiedOriginal":
        return self

    def __next__(self) -> bytes:
        # Bytes sent after a later revoke commits cannot be recalled (docs/access-and-data.md).
        block = b""
        with self._hold.lock:
            spool = None if self._hold.done else self._hold.spool
            if spool is not None:
                if not self._started:
                    spool.seek(0)
                    self._started = True
                block = spool.read(STREAM_CHUNK_BYTES)
        if not block:
            self.close()
            raise StopIteration
        return block


class VerifiedFileResponse(StreamingResponse):
    """Streams a ``VerifiedOriginal`` and closes it however the response ends:
    completion, client disconnect (before or after the first chunk) or error."""

    def __init__(self, verified: VerifiedOriginal, **kwargs):
        super().__init__(verified, **kwargs)
        self._verified = verified

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A native cancellation can abandon a thread-pool read. Close waits
            # on the owner's lock in an executor, keeping the slot until that
            # read settles, without blocking the API event loop under the lock.
            work = asyncio.get_running_loop().run_in_executor(None, self._verified.close)
            cancelled = False
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
            work.result()
            if cancelled:
                raise asyncio.CancelledError()
