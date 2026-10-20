"""Gate CI evidence on a successful secret scan (PR #66 P2-3).

    python scripts/m8a_ci_scan.py --context <m8a_test_host context> [--publish-dir <new dir>] <file> ...

Each file is read once; those exact bytes are scanned for the context's
generated secrets and for token-like link fragments. Only when every named
file exists, is readable and is clean are the scanned bytes written to
``--publish-dir`` (a fresh directory) and ``clean=true`` appended to
``$GITHUB_OUTPUT``; the workflow uploads only that directory and only on that
output. A missing, unreadable or leaking file, an unreadable context or an
existing non-empty publish directory exits non-zero and publishes nothing.
Messages name the file and the kind of problem, never the value.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

TOKEN = re.compile(rb"#token=[A-Za-z0-9_\-]{20,}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("--publish-dir", type=Path)
    p.add_argument("files", nargs="+", type=Path)
    a = p.parse_args()
    try:
        ctx = json.loads((a.context / "context.json").read_text(encoding="utf-8"))
        if not isinstance(ctx, dict):
            raise ValueError
    except (OSError, ValueError):
        print("ERROR scan context unreadable; nothing published")
        return 2
    secrets = [(k, v.encode("utf-8")) for k, v in ctx.items() if isinstance(v, str) and len(v) >= 16]
    if not secrets:
        print("ERROR scan context holds no secrets to check; nothing published")
        return 2
    clean, problems = {}, 0
    for path in a.files:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            print(f"MISSING {path}")
            problems += 1
            continue
        except OSError:
            print(f"UNREADABLE {path}")
            problems += 1
            continue
        leaks = [name for name, value in secrets if value in data]
        if TOKEN.search(data):
            leaks.append("link token fragment")
        for name in leaks:
            print(f"LEAK {path}: {name}")
        problems += len(leaks)
        if not leaks:
            clean[path.name] = data
            print(f"scanned {path}: clean")
    if problems or len(clean) != len(a.files):
        print(f"scan failed ({problems} problem(s)); nothing published")
        return 1
    if a.publish_dir is not None:
        if a.publish_dir.exists() and any(a.publish_dir.iterdir()):
            print("ERROR publish directory not empty; nothing published")
            return 2
        a.publish_dir.mkdir(parents=True, exist_ok=True)
        for name, data in clean.items():
            (a.publish_dir / name).write_bytes(data)
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write("clean=true\n")
        print(f"published {len(clean)} scanned file(s) to {a.publish_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
