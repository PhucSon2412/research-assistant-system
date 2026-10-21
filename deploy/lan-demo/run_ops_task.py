"""Windows Task Scheduler entry for the M9 LAN demo (scripts/m9_lan_demo.py installs it).

    pythonw.exe <root>\\bin\\run_ops_task.py backup|check

Runs the frozen ``scripts/ras_ops.py`` of the release copy with the Conda interpreter, without a
console window, and appends its output to ``<root>\\logs\\ops-<date>.log``. The paths come from
``task.json`` next to this file (written by ``m9_lan_demo.py prepare``); nothing here reads an env
file or a secret. ras_ops itself prints only codes, counts and timestamps.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUTS = {"backup": 3 * 3600, "check": 600}
KEEP_LOG_DAYS = 30


def main() -> int:
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if command not in TIMEOUTS:
        return 2
    cfg = json.loads((HERE / "task.json").read_text(encoding="utf-8"))
    argv = [cfg["python"], cfg["ras_ops"], command, "--env-file", cfg["env_file"], "--project", cfg["project"]]
    for path in cfg["compose_files"]:
        argv += ["--compose-file", path]
    logs = Path(cfg["log_dir"])
    logs.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - KEEP_LOG_DAYS * 86400
    for old in logs.glob("ops-*.log"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    with open(logs / f"ops-{datetime.now():%Y%m%d}.log", "ab") as log:
        log.write(f"--- {datetime.now().astimezone().isoformat()} {command}\n".encode())
        log.flush()
        try:
            code = subprocess.run(argv, stdout=log, stderr=log, timeout=TIMEOUTS[command],
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
        except subprocess.TimeoutExpired:
            code = 124
        log.write(f"--- exit {code}\n".encode())
    return code


if __name__ == "__main__":
    sys.exit(main())
