"""Refuse to publish CI evidence that contains a context secret.

    python scripts/m8a_ci_scan.py --context <m8a_test_host context> <file> ...

Scans each existing file for the context's generated passwords and for
token-like link fragments; exit 1 names the file and the kind of leak (never
the value). A missing file is reported, not treated as clean evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("files", nargs="+", type=Path)
    a = p.parse_args()
    ctx = json.loads((a.context / "context.json").read_text(encoding="utf-8"))
    secrets = {k: v for k, v in ctx.items() if isinstance(v, str) and len(v) >= 16}
    bad = 0
    for path in a.files:
        if not path.exists():
            print(f"MISSING {path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, value in secrets.items():
            if value in text:
                print(f"LEAK {path}: context secret '{name}'")
                bad += 1
        if re.search(r"#token=[A-Za-z0-9_\-]{20,}", text):
            print(f"LEAK {path}: link token fragment")
            bad += 1
        print(f"scanned {path}: {'clean' if not bad else 'LEAKS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
