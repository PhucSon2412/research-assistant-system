"""Build docs/m8a-test-inventory.json from evidence outside the repo.

    python scripts/m8a_inventory.py --spec <inventory-spec.json> --out docs/m8a-test-inventory.json

The spec (outside the repo) names each evidence file and its label: JUnit
XMLs (counted per outcome and per file, plus the SHA-256 of the file),
smoke/browser/countercheck result JSONs (copied as check lists) and free-text
notes. Source hashes cover every file changed against the base commit, so a
reviewer can compare the evidence to the tree. Nothing secret is copied:
JUnit failure text is not included, only node IDs and outcomes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    outcomes, per_file = Counter(), {}
    for case in root.iter("testcase"):
        kind = "pass"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                kind = tag
        outcomes[kind] += 1
        name = case.get("classname", "").replace(".", "/") + ".py"
        per_file.setdefault(name, Counter())[kind] += 1
    return {"sha256": sha(path), "total": sum(outcomes.values()), **dict(outcomes),
            "per_file": {k: dict(v) for k, v in sorted(per_file.items())}}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    spec = json.loads(a.spec.read_text(encoding="utf-8"))
    base = spec["base"]
    changed = subprocess.run(["git", "diff", "--name-only", base], capture_output=True, text=True, cwd=REPO).stdout.split()
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True,
                               cwd=REPO).stdout.split()
    files = sorted(set(changed) | set(untracked))
    out = {k: v for k, v in spec.items() if k not in ("junit", "results")}
    files = [f for f in files if f != str(a.out.resolve().relative_to(REPO)).replace("\\", "/")]
    out["source"] = {"base": base, "changed_files": {f: sha(REPO / f) for f in files if (REPO / f).is_file()}}
    out["junit"] = {label: junit(Path(path)) for label, path in spec.get("junit", {}).items()}
    out["results"] = {}
    for label, path in spec.get("results", {}).items():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # Labels and outcomes only: smoke "detail" may hold synthetic cookies/headers.
        if isinstance(data, list):
            data = {"checks": data}
        # M8b drills also record "metrics" (timings/counts/codes measured, never secrets).
        summary = {k: v for k, v in data.items() if k in ("chrome", "source_files", "source_unchanged", "metrics")}
        if "checks" in data:
            summary["checks"] = [{"check": c["check"], "pass": c["pass"]} for c in data["checks"]]
        if "results" in data:
            summary["results"] = data["results"]
        if "outbound_denied" in data:
            summary["outbound_denied"] = len(data["outbound_denied"])
        out["results"][label] = {"sha256": sha(Path(path)), **summary}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"inventory: {len(out['source']['changed_files'])} source hashes, {len(out['junit'])} JUnit, "
          f"{len(out['results'])} result files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
