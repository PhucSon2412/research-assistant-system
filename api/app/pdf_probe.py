"""Bounded PDF identity/page-count probe; no text extraction."""

from __future__ import annotations

import os
import subprocess
import sys

from app.config import FileLimits


def page_count(path: str, limits: FileLimits) -> int | None:
    # A separate process bounds CPU/wall time and, on Linux (the API image),
    # address space. It sees only the already size-limited temporary file.
    try:
        done = subprocess.run(
            [sys.executable, "-m", "app.pdf_probe", path, str(limits.pdf_max_pages),
             str(limits.pdf_probe_memory_bytes), str(limits.pdf_probe_timeout_seconds)],
            capture_output=True,
            timeout=limits.pdf_probe_timeout_seconds,
            check=False,
        )
        if done.returncode or len(done.stdout) > 16:
            return None
        count = int(done.stdout)
        return count if 0 < count <= limits.pdf_max_pages else None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _child() -> int:
    if os.name != "nt":
        import resource

        memory = int(sys.argv[3])
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        seconds = int(sys.argv[4])
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    from pypdf import PdfReader

    try:
        with open(sys.argv[1], "rb") as file:
            if file.read(5) != b"%PDF-":
                return 1
            file.seek(0)
            reader = PdfReader(file, strict=True)
            if reader.is_encrypted:
                return 1
            count = len(reader.pages)
            if not 0 < count <= int(sys.argv[2]):
                return 1
            print(count)
            return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_child())
