"""Which CI jobs a pull request needs, from its changed paths (M12).

    git diff --name-only HEAD^1 HEAD | python scripts/ci_paths.py

On a pull_request checkout HEAD is GitHub's merge commit and HEAD^1 the base
branch tip, so the diff is exactly what the PR changes. Prints and appends to
``$GITHUB_OUTPUT`` one ``<job>=true|false`` line per job. No secret, no
third-party action. Fail-safe: a path no rule knows runs every job, and an
empty list (diff unavailable) runs every job too.

- docs/** and *.md at the repository root: no job.
- web/**: web.
- deploy/**: deploy-config, and api (the test harness pulls the PostgreSQL/
  Redis/MinIO digests pinned in deploy/compose.yaml).
- api/**, scripts/**, evaluation/** (test data): api and deploy-config (the
  API image is built and validated there).
- .github/** and anything else: every job.
"""
from __future__ import annotations

import os
import sys

JOBS = ("api", "web", "deploy")


def jobs_for(path: str) -> set[str]:
    if path.startswith("docs/") or ("/" not in path and path.endswith(".md")):
        return set()
    if path.startswith("web/"):
        return {"web"}
    if path.startswith("deploy/"):
        return {"deploy", "api"}
    if path.startswith(("api/", "scripts/", "evaluation/")):
        return {"api", "deploy"}
    return set(JOBS)


def needed(paths: list[str]) -> dict[str, bool]:
    paths = [p.strip() for p in paths if p.strip()]
    if not paths:
        return dict.fromkeys(JOBS, True)
    wanted = set().union(*(jobs_for(p) for p in paths))
    return {job: job in wanted for job in JOBS}


def main() -> int:
    result = needed(sys.stdin.read().splitlines())
    lines = [f"{job}={'true' if run else 'false'}" for job, run in result.items()]
    print("\n".join(lines))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
