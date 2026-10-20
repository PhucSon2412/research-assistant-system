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
  PR #66: smtp_deadline (parent waits without the whole-send deadline),
  late_success (a late child answer counts), ci_upload_always (upload ignores
  the scan), scan_missing_is_clean, minio_unpullable_tag (deploy back on the
  dead quay.io tag; checked by m8a_deploy_check --check-registry)
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
    # PR #66 fixes: the parent no longer enforces the whole-send deadline.
    "smtp_deadline": ("api", "app/delivery.py",
                      "out, _ = child.communicate(job, timeout=max(0.0, deadline - time.monotonic()))",
                      "out, _ = child.communicate(job)",
                      ["tests/test_m8a_smtp_deadline.py::test_dripped_data_reply_cannot_succeed_after_the_deadline",
                       "tests/test_m8a_smtp_deadline.py::test_slow_or_hung_dns_is_inside_the_deadline[2]"]),
    "late_success": ("api", "app/delivery.py",
                     '        if time.monotonic() > deadline:\n            raise DeliveryFailed("smtp_timeout")  # late answers are never a success\n',
                     "",
                     ["tests/test_m8a_smtp_deadline.py::test_a_result_after_the_deadline_is_never_success"]),
    "ci_upload_always": ("repo", ".github/workflows/ci.yml",
                         "        if: ${{ !cancelled() && steps.scan.outputs.clean == 'true' }}",
                         "        if: always()",
                         ["tests/test_m8a_ci_guard.py::test_workflow_upload_depends_on_a_clean_scan_of_the_published_file"]),
    "scan_missing_is_clean": ("repo", "scripts/m8a_ci_scan.py",
                              '            print(f"MISSING {path}")\n            problems += 1\n            continue\n',
                              '            print(f"MISSING {path}")\n            clean[path.name] = b""\n            continue\n',
                              ["tests/test_m8a_ci_guard.py::test_missing_file_is_not_clean"]),
    "minio_unpullable_tag": ("repo", "deploy/compose.yaml",
                             "image: ${RAS_MINIO_IMAGE:-cgr.dev/chainguard/minio@sha256:709099319add48dfb44aa596a7070d3ddd8dd04d7ae881765719aec29df9e510}",
                             "image: quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z",
                             ["!deploy-check"]),
    "tls_verify": ("api", "app/smtp_transport.py",
                   "    ctx.check_hostname = True\n    ctx.verify_mode = ssl.CERT_REQUIRED\n",
                   "    ctx.check_hostname = False\n    ctx.verify_mode = ssl.CERT_NONE\n",
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
    sources = [REPO / "api/app", REPO / "api/tests", REPO / "web/lib", REPO / "web/tests", REPO / "deploy",
               REPO / "scripts", REPO / ".github"]
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
        elif kind == "repo":
            for part in ("api", "scripts", "deploy", ".github"):
                shutil.copytree(REPO / part, copy / part, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            shutil.copyfile(REPO / "docker-compose.yml", copy / "docker-compose.yml")
        else:
            shutil.copytree(REPO / "web", copy, ignore=shutil.ignore_patterns("node_modules", ".next"))
        target = copy / rel
        text = target.read_bytes().decode("utf-8")
        assert text.count(old) == 1, f"{name}: mutation anchor not unique"
        target.write_bytes(text.replace(old, new).encode("utf-8"))
        if kind == "api":
            cmd = [PY, str(REPO / "scripts/m8a_test_host.py"), "--context", str(a.host_context),
                   "--api-dir", str(copy), "test", "--", *tests]
        elif tests == ["!deploy-check"]:
            cmd = [PY, str(copy / "scripts/m8a_deploy_check.py"), "--check-registry"]
        elif kind == "repo":
            cmd = [PY, str(REPO / "scripts/m8a_test_host.py"), "--context", str(a.host_context),
                   "--api-dir", str(copy / "api"), "test", "--", *tests]
        else:
            cmd = ["node", "--test", *tests]
        proc = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=1800)
        (out / f"{name}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        text = proc.stdout + proc.stderr
        # A mutation "holds" only when the targeted tests ran and failed, not on import/usage errors.
        if tests == ["!deploy-check"]:
            holds = proc.returncode == 1 and "FAIL " in text
        elif kind in ("api", "repo"):
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
