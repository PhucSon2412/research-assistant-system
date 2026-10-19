"""M8a counterchecks: each guard is broken in a COPY outside the repo and its
targeted tests must then fail; the original sources are hashed before/after.

    python scripts/m8a_counterchecks.py --host-context <m8a_test_host ctx> --out <dir outside repo>

Mutations (one at a time, exact single-occurrence replacements):
  tls_verify      SMTP adapter stops verifying certificate and host name
  https_origin    production accepts http:// allowed origins / link base
  trusted_ip      any peer may name the client address
  atomic_limiter  admit/TTL done as separate non-atomic Redis commands
  fail_open       an unavailable limiter lets the request through
  send_after_lost_proof  invitation mail sent although the pre-send proof failed
  web_edge_token  web forwards the client address without the edge token
A countercheck "holds" only when the mutated copy fails the named tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable

MUTATIONS = {
    "tls_verify": ("api", "app/delivery.py",
                   "        ctx.check_hostname = True\n        ctx.verify_mode = ssl.CERT_REQUIRED\n",
                   "        ctx.check_hostname = False\n        ctx.verify_mode = ssl.CERT_NONE\n",
                   ["tests/test_m8a_smtp_delivery.py::test_unknown_ca_is_refused_before_any_mail_command",
                    "tests/test_m8a_smtp_delivery.py::test_hostname_mismatch_is_refused"]),
    "https_origin": ("api", "app/config.py",
                     '            if any(not o.startswith("https://") for o in self.http.allowed_origins):\n'
                     '                raise ValueError("production allowed_origins must all be https://")\n',
                     "", ["tests/test_m8a_smtp_delivery.py::test_smtp_mode_requires_settings_and_https_link_base_in_production"]),
    "trusted_ip": ("api", "app/rate_limit.py",
                   "    if any(peer_ip in ipaddress.ip_network(cidr) for cidr in trusted):",
                   "    if True:",
                   ["tests/test_m8a_rate_limit.py::test_untrusted_peer_header_is_ignored",
                    "tests/test_m8a_rate_limit.py::test_spoofed_client_headers_from_an_untrusted_peer_do_not_change_the_bucket"]),
    "atomic_limiter": ("api", "app/rate_limit.py",
                       "            script = self._redis()\n            deny_ms = int(script(keys=keys, args=args))\n",
                       "            self._redis()\n            deny_ms = 0\n            import time as _t\n"
                       "            for i, k in enumerate(keys):\n"
                       "                seen = int(self._client.get(k) or 0)\n                _t.sleep(0.002)\n"
                       "                if seen >= args[2 * i]:\n                    deny_ms = max(deny_ms, 1000)\n"
                       "                else:\n                    self._client.set(k, seen + 1, px=args[2 * i + 1])\n",
                       ["tests/test_m8a_rate_limit.py::test_threads_never_admit_more_than_the_limit",
                        "tests/test_m8a_rate_limit.py::test_two_processes_share_one_bucket"]),
    "fail_open": ("api", "app/rate_limit.py",
                  '            raise RateLimitUnavailable("rate_limit_unavailable") from None\n',
                  "            return None\n",
                  ["tests/test_m8a_rate_limit.py::test_secret_problems_and_unreachable_redis_fail_closed",
                   "tests/test_m8a_rate_limit.py::test_redis_outage_is_a_controlled_503_and_health_stays_up"]),
    "send_after_lost_proof": ("api", "app/identity/invitations.py",
                              "    except ApiError:\n        db.rollback()\n        _compensate_delivery(db, created, clock)\n        raise\n    try:\n        delivery.send(message, now=at)",
                              "    except ApiError:\n        db.rollback()\n        delivery.send(OutgoingMessage(MessageKind.INVITATION, created.invitation.email,"
                              " build_link(settings.http.public_base_url or '', '/invite', created.token),"
                              " created.invitation.expires_at), now=clock())\n        _compensate_delivery(db, created, clock)\n        raise\n"
                              "    try:\n        delivery.send(message, now=at)",
                              ["tests/test_m8a_auth_delivery.py::test_no_mail_when_the_proof_is_lost_before_sending"]),
    "web_edge_token": ("web", "lib/proxy.ts",
                       "  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return null;\n",
                       "", ["tests/edge-trust.test.mjs"]),
}


def tree_hash(paths):
    digest = {}
    for base in paths:
        for f in sorted(base.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and "node_modules" not in f.parts and ".next" not in f.parts:
                digest[str(f.relative_to(REPO))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return digest


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host-context", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--only", nargs="*")
    a = p.parse_args()
    out = a.out.resolve()
    if out == REPO or REPO in out.parents:
        raise SystemExit("output must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    sources = [REPO / "api/app", REPO / "api/tests", REPO / "web/lib", REPO / "web/tests"]
    before = tree_hash(sources)
    results = []
    for name, (kind, rel, old, new, tests) in MUTATIONS.items():
        if a.only and name not in a.only:
            continue
        copy = out / name
        if copy.exists():
            shutil.rmtree(copy)
        if kind == "api":
            shutil.copytree(REPO / "api", copy, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        else:
            shutil.copytree(REPO / "web", copy, ignore=shutil.ignore_patterns("node_modules", ".next"))
        target = copy / rel
        text = target.read_bytes().decode("utf-8")
        assert text.count(old) == 1, f"{name}: mutation anchor not unique"
        target.write_bytes(text.replace(old, new).encode("utf-8"))
        if kind == "api":
            cmd = [PY, str(REPO / "scripts/m8a_test_host.py"), "--context", str(a.host_context),
                   "--api-dir", str(copy), "test", "--", *tests]
        else:
            cmd = ["node", "--test", *tests]
        proc = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=1800)
        (out / f"{name}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        text = proc.stdout + proc.stderr
        # A mutation "holds" only when the targeted tests ran and failed, not on import/usage errors.
        if kind == "api":
            holds = proc.returncode == 1 and " failed" in text and "error" not in text.splitlines()[-1].lower()
        else:
            holds = proc.returncode != 0 and "ℹ fail 0" not in text and "ℹ fail" in text
        results.append({"mutation": name, "file": rel, "tests": tests, "exit": proc.returncode, "holds": holds})
        print(("HOLDS " if holds else "MISSED ") + name + f" (exit {proc.returncode})", flush=True)
        shutil.rmtree(copy)
    after = tree_hash(sources)
    report = {"results": results, "source_files": len(before), "source_unchanged": before == after}
    (out / "counterchecks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"counterchecks: {sum(r['holds'] for r in results)}/{len(results)} hold; sources unchanged={before == after}")
    return 0 if all(r["holds"] for r in results) and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
