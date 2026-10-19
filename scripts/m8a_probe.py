"""M8a smoke probe run inside a throwaway container on one Compose network (stdlib only).

    python m8a_probe.py connect host:port ...            # which targets are reachable
    python m8a_probe.py logins --base URL --csrf-path P --login-path P --origin O --count N
                         [--ca FILE] [--spoof] [--edge-token TOKEN]

``logins`` sends N wrong-password logins for synthetic addresses and prints
the status codes and Retry-After values as JSON. ``--spoof`` rotates forged
client-address headers on every request; ``--edge-token`` forges the edge
token a direct caller cannot know. Never used against real accounts.
"""
from __future__ import annotations

import argparse
import http.client
import json
import socket
import ssl
import sys
from urllib.parse import urlsplit


def connect(targets: list[str]) -> dict:
    out = {}
    for target in targets:
        host, port = target.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                out[target] = "open"
        except socket.gaierror:
            out[target] = "no_such_host"
        except OSError as exc:
            out[target] = type(exc).__name__
    return out


def logins(a) -> dict:
    url = urlsplit(a.base)
    if url.scheme == "https":
        ctx = ssl.create_default_context(cafile=a.ca)
        make = lambda: http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=20, context=ctx)  # noqa: E731
    else:
        make = lambda: http.client.HTTPConnection(url.hostname, url.port or 80, timeout=20)  # noqa: E731
    conn = make()
    conn.request("GET", a.csrf_path)
    response = conn.getresponse()
    response.read()
    cookies = {}
    for header, value in response.getheaders():
        if header.lower() == "set-cookie":
            name, _, rest = value.partition("=")
            cookies[name] = rest.split(";", 1)[0]
    csrf = cookies.get("ras_csrf", "")
    results = []
    for i in range(a.count):
        headers = {"content-type": "application/json", "origin": a.origin, "x-csrf-token": csrf,
                   "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
        if a.spoof:
            forged = f"203.0.113.{(i * 7) % 250 + 1}"
            headers.update({"x-ras-client-ip": forged, "x-forwarded-for": forged, "x-real-ip": forged,
                            "forwarded": f"for={forged}", "true-client-ip": forged})
        if a.edge_token:
            headers["x-ras-edge-token"] = a.edge_token
        body = json.dumps({"email": f"probe{i}@example.test", "password": "wrong-password-probe"})
        conn.request("POST", a.login_path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read().decode("utf-8", "replace")
        try:
            code = json.loads(payload).get("error")
        except ValueError:
            code = None
        results.append({"status": response.status, "error": code, "retry_after": response.getheader("retry-after")})
    return {"results": results}


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("targets", nargs="+")
    lg = sub.add_parser("logins")
    for name in ("--base", "--csrf-path", "--login-path", "--origin"):
        lg.add_argument(name, required=True)
    lg.add_argument("--count", type=int, required=True)
    lg.add_argument("--ca")
    lg.add_argument("--spoof", action="store_true")
    lg.add_argument("--edge-token")
    a = p.parse_args()
    print(json.dumps(connect(a.targets) if a.cmd == "connect" else logins(a)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
