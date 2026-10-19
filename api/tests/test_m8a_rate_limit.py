"""M8a auth rate limits: trusted client address, atomic Redis buckets, routes.

Redis tests use a real, isolated Redis named by RAS_TEST_REDIS_HOST/PORT/
PASSWORD (database 15, flushed per test); they fail rather than skip when it
is missing. Route tests also use the isolated PostgreSQL of the API suite.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import HttpSecurity, RateLimitPolicy, RateRule, Settings
from app.rate_limit import (AuthAction, RateLimited, RateLimitUnavailable, RedisRateLimiter, client_address,
                            get_rate_limiter, source_bucket)

API_DIR = Path(__file__).resolve().parents[1]
DB = 15


# --- trusted client address -------------------------------------------------

def test_untrusted_peer_header_is_ignored():
    assert client_address("198.51.100.7", ["203.0.113.9"], []) == "198.51.100.7"
    assert client_address("198.51.100.7", ["203.0.113.9"], ["10.9.0.10/32"]) == "198.51.100.7"


def test_trusted_proxy_names_one_valid_client():
    trusted = ["10.9.0.10/32"]
    assert client_address("10.9.0.10", ["203.0.113.9"], trusted) == "203.0.113.9"
    assert client_address("10.9.0.10", [" 2001:db8::5 "], trusted) == "2001:db8::5"


@pytest.mark.parametrize("values", [[], ["203.0.113.9", "198.51.100.1"], ["203.0.113.9, 198.51.100.1"],
                                    ["not-an-ip"], [""], ["203.0.113.9/24"]])
def test_trusted_proxy_without_one_valid_value_keeps_the_peer(values, caplog):
    with caplog.at_level(logging.WARNING):
        assert client_address("10.9.0.10", values, ["10.9.0.10/32"]) == "10.9.0.10"
    for value in values:
        for part in value.split(","):
            if part.strip():
                assert part.strip() not in caplog.text


def test_bucket_groups_ipv6_by_prefix_and_maps_ipv4():
    assert source_bucket("2001:db8:1:2:aaaa::1", 64) == source_bucket("2001:db8:1:2:bbbb::9", 64)
    assert source_bucket("2001:db8:1:2::1", 64) != source_bucket("2001:db8:1:3::1", 64)
    assert source_bucket("::ffff:198.51.100.7", 64) == "198.51.100.7"
    assert source_bucket("garbage", 64) == "unknown"


def test_trusted_proxy_settings_refuse_everything_and_garbage():
    assert HttpSecurity(trusted_proxy_cidrs=["10.9.0.10"]).trusted_proxy_cidrs == ["10.9.0.10/32"]
    for bad in (["0.0.0.0/0"], ["::/0"], ["web"], ["10.0.0.0/33"]):
        with pytest.raises(ValidationError):
            HttpSecurity(trusted_proxy_cidrs=bad)
    for bad in ({"key_secret_file": "relative"}, {"login_source": {"limit": 0, "window_seconds": 1}},
                {"mode": "memory"}):
        with pytest.raises(ValidationError):
            RateLimitPolicy(**bad)


# --- Redis-backed limiter ---------------------------------------------------

@pytest.fixture
def redis_env(tmp_path, monkeypatch, real_rate_limiter):
    host, port, password = (os.environ.get(f"RAS_TEST_REDIS_{k}") for k in ("HOST", "PORT", "PASSWORD"))
    if not (host and port and password):
        pytest.fail("Set RAS_TEST_REDIS_HOST/PORT/PASSWORD to an isolated Redis for the M8a limiter tests")
    import redis
    client = redis.Redis(host=host, port=int(port), password=password, db=DB)
    client.flushdb()
    secret = tmp_path / "rate-limit-secret"
    secret.write_bytes(os.urandom(24).hex().encode() + b"\n")
    env = {"RAS_REDIS_HOST": host, "RAS_REDIS_PORT": port, "RAS_REDIS_PASSWORD": password,
           "RAS_RATE_LIMIT__MODE": "redis", "RAS_RATE_LIMIT__REDIS_DB": str(DB),
           "RAS_RATE_LIMIT__KEY_SECRET_FILE": str(secret)}
    yield {"env": env, "client": client, "secret": secret}
    client.flushdb()
    client.close()


def limiter_settings(redis_env, **policy) -> Settings:
    env = redis_env["env"]
    return Settings(db={"host": "h", "user": "u", "password": "p", "name": "d"}, redis_host=env["RAS_REDIS_HOST"],
                    redis_port=int(env["RAS_REDIS_PORT"]), redis_password=env["RAS_REDIS_PASSWORD"],
                    rate_limit={"mode": "redis", "redis_db": DB, "key_secret_file": str(redis_env["secret"]),
                                **policy})


def admitted(limiter, action=AuthAction.TOKEN, n=1, **kw):
    ok = 0
    for _ in range(n):
        try:
            limiter.check(action, source=kw.get("source", "198.51.100.7"), account=kw.get("account"))
            ok += 1
        except RateLimited:
            pass
    return ok


def test_threads_never_admit_more_than_the_limit(redis_env):
    limiter = RedisRateLimiter(limiter_settings(redis_env, token_source={"limit": 7, "window_seconds": 60}))
    results, barrier = [], threading.Barrier(40)

    def worker():
        barrier.wait()
        results.append(admitted(limiter))

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 7


def test_two_processes_share_one_bucket(redis_env, tmp_path):
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("RAS_DB__") and k != "RAS_DATABASE_URL"}
    env = {**inherited, **redis_env["env"], "PYTHONPATH": str(API_DIR),
           "RAS_DATABASE_URL": "postgresql+psycopg://u@h/db",
           "RAS_RATE_LIMIT__TOKEN_SOURCE__LIMIT": "9", "RAS_RATE_LIMIT__TOKEN_SOURCE__WINDOW_SECONDS": "60"}
    go = tmp_path / "go"
    code = ("import sys,time,pathlib\n"
            "from app.config import get_settings\nfrom app.rate_limit import RedisRateLimiter,AuthAction,RateLimited\n"
            "l=RedisRateLimiter(get_settings())\ngo=pathlib.Path(sys.argv[1])\n"
            "while not go.exists(): time.sleep(0.005)\nn=0\n"
            "for _ in range(25):\n  try: l.check(AuthAction.TOKEN,source='198.51.100.7'); n+=1\n"
            "  except RateLimited: pass\nprint(n)\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(go)], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    time.sleep(1.5)
    go.write_text("go")
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    counts = [int(o[0].strip()) for o in outs]
    assert sum(counts) == 9 and all(c < 25 for c in counts), counts


def test_window_ends_and_retry_after_is_bounded(redis_env):
    limiter = RedisRateLimiter(limiter_settings(redis_env, token_source={"limit": 2, "window_seconds": 2}))
    assert admitted(limiter, n=2) == 2
    with pytest.raises(RateLimited) as exc:
        limiter.check(AuthAction.TOKEN, source="198.51.100.7")
    assert 1 <= exc.value.retry_after <= 2
    assert_window_reopens(limiter, exc.value.retry_after)


def assert_window_reopens(limiter, retry_after, **kw):
    # The window is Redis server time; Docker Desktop's VM clock can drift from
    # the host's, so poll a bounded few seconds instead of one fixed sleep.
    time.sleep(retry_after + 0.2)
    deadline = time.monotonic() + 5
    while admitted(limiter, n=1, **kw) != 1:
        assert time.monotonic() < deadline, "window never reopened"
        time.sleep(0.25)


def test_a_key_left_without_ttl_is_repaired(redis_env):
    limiter = RedisRateLimiter(limiter_settings(redis_env, token_source={"limit": 1, "window_seconds": 2}))
    admitted(limiter)
    client = redis_env["client"]
    [key] = client.keys("ras:rl:v1:*")
    client.persist(key)  # e.g. a crash between commands of a non-atomic implementation
    with pytest.raises(RateLimited):
        limiter.check(AuthAction.TOKEN, source="198.51.100.7")
    assert 0 < client.pttl(key) <= 2000
    assert_window_reopens(limiter, 2)


def test_keys_hold_only_digests_and_account_bucket_is_per_email(redis_env):
    limiter = RedisRateLimiter(limiter_settings(redis_env, login_source={"limit": 100, "window_seconds": 60},
                                                login_account={"limit": 2, "window_seconds": 60}))
    for source in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        admitted(limiter, AuthAction.LOGIN, source=source, account=" Victim@Example.TEST ")
    with pytest.raises(RateLimited):
        limiter.check(AuthAction.LOGIN, source="198.51.100.4", account="victim@example.test")
    assert admitted(limiter, AuthAction.LOGIN, source="198.51.100.4", account="other@example.test") == 1
    keys = [k.decode() for k in redis_env["client"].keys("*")]
    assert keys and all(k.startswith("ras:rl:v1:login:") for k in keys)
    joined = " ".join(keys).lower()
    assert "victim" not in joined and "example" not in joined and "198.51" not in joined


def test_secret_problems_and_unreachable_redis_fail_closed(redis_env, tmp_path):
    settings = limiter_settings(redis_env)
    redis_env["secret"].write_bytes(b"short\n")
    with pytest.raises(RateLimitUnavailable):
        RedisRateLimiter(settings).check(AuthAction.TOKEN, source="198.51.100.7")
    redis_env["secret"].unlink()
    with pytest.raises(RateLimitUnavailable):
        RedisRateLimiter(settings).check(AuthAction.TOKEN, source="198.51.100.7")
    with pytest.raises(RateLimitUnavailable):
        RedisRateLimiter(settings.model_copy(update={"rate_limit": settings.rate_limit.model_copy(
            update={"key_secret_file": None})})).check(AuthAction.TOKEN, source="198.51.100.7")
    redis_env["secret"].write_bytes(os.urandom(32).hex().encode())
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    down = settings.model_copy(update={"redis_port": dead})
    started = time.monotonic()
    with pytest.raises(RateLimitUnavailable):
        RedisRateLimiter(down).check(AuthAction.TOKEN, source="198.51.100.7")
    assert time.monotonic() - started < 5
    wrong = settings.model_copy(update={"redis_password": "not-the-password"})
    with pytest.raises(RateLimitUnavailable):
        RedisRateLimiter(wrong).check(AuthAction.TOKEN, source="198.51.100.7")


# --- routes -----------------------------------------------------------------

LOGIN_LIMIT = 4
ACCOUNT_LIMIT = 3


@pytest.fixture
def limited(api, redis_env, monkeypatch):
    from app.config import get_settings
    env = {**redis_env["env"],
           "RAS_RATE_LIMIT__LOGIN_SOURCE__LIMIT": str(LOGIN_LIMIT), "RAS_RATE_LIMIT__LOGIN_SOURCE__WINDOW_SECONDS": "60",
           "RAS_RATE_LIMIT__LOGIN_ACCOUNT__LIMIT": str(ACCOUNT_LIMIT),
           "RAS_RATE_LIMIT__LOGIN_ACCOUNT__WINDOW_SECONDS": "60",
           "RAS_RATE_LIMIT__RESET_REQUEST_SOURCE__LIMIT": "100",
           "RAS_RATE_LIMIT__RESET_REQUEST_SOURCE__WINDOW_SECONDS": "60",
           "RAS_RATE_LIMIT__RESET_REQUEST_ACCOUNT__LIMIT": "2",
           "RAS_RATE_LIMIT__RESET_REQUEST_ACCOUNT__WINDOW_SECONDS": "60",
           "RAS_RATE_LIMIT__TOKEN_SOURCE__LIMIT": "3", "RAS_RATE_LIMIT__TOKEN_SOURCE__WINDOW_SECONDS": "60",
           "RAS_HTTP__TRUSTED_PROXY_CIDRS": '["10.9.0.10/32"]'}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_rate_limiter.cache_clear()
    yield api
    get_rate_limiter.cache_clear()


def client(peer: str = "198.51.100.7") -> TestClient:
    from app.main import app
    from tests.helpers import WEB_ORIGIN
    c = TestClient(app, client=(peer, 50000))
    c.headers["origin"] = WEB_ORIGIN
    c.get("/auth/csrf")
    c.headers["x-csrf-token"] = c.cookies.get("ras_csrf")
    return c


def login(c, email, password="wrong-password-123", **headers):
    return c.post("/auth/login", json={"email": email, "password": password}, headers=headers)


def assert_limited(response):
    assert response.status_code == 429, response.text
    retry = int(response.headers["retry-after"])
    assert 1 <= retry <= 60 and response.json() == {"error": "rate_limited", "retry_after": retry}
    assert "set-cookie" not in response.headers


def test_login_source_limit_blocks_even_the_right_password(limited):
    from tests.helpers import ADMIN_EMAIL, ADMIN_PASSWORD
    limited.bootstrap_admin()
    c = client()
    for i in range(LOGIN_LIMIT):
        assert login(c, f"nobody{i}@example.test").status_code == 401
    assert_limited(login(c, ADMIN_EMAIL, ADMIN_PASSWORD))
    # Another source is unaffected: the bucket is per source, not global.
    assert login(client("198.51.100.8"), ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200


def test_spoofed_client_headers_from_an_untrusted_peer_do_not_change_the_bucket(limited):
    c = client("198.51.100.7")
    for i in range(LOGIN_LIMIT + 3):
        response = login(c, f"spoof{i}@example.test", **{"x-ras-client-ip": f"203.0.113.{i}",
                                                       "x-forwarded-for": f"203.0.113.{i}", "x-real-ip": f"203.0.113.{i}",
                                                       "forwarded": f"for=203.0.113.{i}"})
        assert response.status_code == (401 if i < LOGIN_LIMIT else 429)


def test_only_the_trusted_proxy_can_name_the_client(limited):
    proxy = client("10.9.0.10")
    for i in range(LOGIN_LIMIT + 2):
        response = login(proxy, f"user{i}@example.test", **{"x-ras-client-ip": f"203.0.113.{i}"})
        assert response.status_code == 401  # each named client has its own source bucket
    for i in range(LOGIN_LIMIT):
        assert login(proxy, f"same{i}@example.test", **{"x-ras-client-ip": "203.0.113.200"}).status_code == 401
    assert_limited(login(proxy, "same-x@example.test", **{"x-ras-client-ip": "203.0.113.200"}))
    # A trusted proxy request without the header is keyed by the proxy itself.
    for i in range(LOGIN_LIMIT):
        assert login(proxy, f"bare{i}@example.test").status_code == 401
    assert_limited(login(proxy, "bare-x@example.test"))


def test_account_bucket_applies_across_sources_whether_or_not_the_account_exists(limited):
    from tests.helpers import ADMIN_EMAIL
    limited.bootstrap_admin()
    for email in (ADMIN_EMAIL, "ghost@example.test"):
        statuses = [login(client(f"198.51.100.{20 + i}"), email).status_code for i in range(ACCOUNT_LIMIT + 1)]
        assert statuses == [401] * ACCOUNT_LIMIT + [429]


def test_reset_request_policy_does_not_reveal_accounts(limited):
    limited.bootstrap_admin()
    limited.create_user("member@example.test")
    c = client()
    results = {}
    for email in ("member@example.test", "absent@example.test"):
        results[email] = [c.post("/auth/password-reset/request", json={"email": email}) for _ in range(3)]
    for email, responses in results.items():
        assert [r.status_code for r in responses] == [202, 202, 429]
        assert responses[0].json() == {"status": "accepted"}
        assert_limited(responses[2])
    outbox = [e.message for e in limited.outbox().entries(now=limited.clock.now)]
    assert len([m for m in outbox if m.recipient == "member@example.test"]) == 2


def test_token_routes_share_a_source_limit_before_touching_tokens(limited):
    c = client()
    calls = [lambda: c.post("/auth/invitations/inspect", json={"token": "x" * 43}),
             lambda: c.post("/auth/invitations/accept", json={"token": "x" * 43, "password": "Long-enough-pass1"}),
             lambda: c.post("/auth/password-reset/confirm", json={"token": "x" * 43, "new_password": "Long-enough-pass1"})]
    assert [call().status_code for call in calls] == [400, 400, 400]
    for call in calls:
        assert_limited(call())


def test_successful_reset_does_not_clear_brute_force_counters(limited):
    from tests.helpers import USER_PASSWORD
    limited.bootstrap_admin()
    limited.create_user("member@example.test")
    c = client("198.51.100.30")
    for _ in range(ACCOUNT_LIMIT - 1):
        assert login(c, "member@example.test").status_code == 401
    other = client("198.51.100.31")
    assert other.post("/auth/password-reset/request", json={"email": "member@example.test"}).status_code == 202
    token = limited.last_link_token("password_reset", "member@example.test")
    assert other.post("/auth/password-reset/confirm",
                      json={"token": token, "new_password": "Fresh-Passw0rd-long"}).status_code == 200
    assert login(client("198.51.100.32"), "member@example.test", "Fresh-Passw0rd-long").status_code == 200
    assert_limited(login(client("198.51.100.33"), "member@example.test", "Fresh-Passw0rd-long"))
    assert USER_PASSWORD


def test_redis_outage_is_a_controlled_503_and_health_stays_up(limited, monkeypatch, redis_env):
    from app.config import get_settings
    from tests.helpers import ADMIN_EMAIL, ADMIN_PASSWORD
    limited.bootstrap_admin()
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    monkeypatch.setenv("RAS_REDIS_PORT", str(dead))
    get_settings.cache_clear()
    get_rate_limiter.cache_clear()
    c = client()
    for _ in range(3):
        response = login(c, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert response.status_code == 503 and response.json() == {"error": "rate_limit_unavailable"}
        assert response.headers["retry-after"] == "30" and "set-cookie" not in response.headers
    reset = c.post("/auth/password-reset/request", json={"email": ADMIN_EMAIL})
    assert reset.status_code == 503 and reset.json() == {"error": "rate_limit_unavailable"}
    assert c.get("/healthz").status_code == 200
    assert limited.outbox().entries(now=limited.clock.now) == []
    monkeypatch.setenv("RAS_REDIS_PORT", redis_env["env"]["RAS_REDIS_PORT"])
    get_settings.cache_clear()
    get_rate_limiter.cache_clear()
    assert login(c, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200  # no account lockout


def test_closed_recovery_gate_answers_first_and_counts_nothing(limited, monkeypatch, tmp_path, redis_env):
    from app.config import get_settings
    c = client()  # CSRF cookie first; the closed gate answers every route but health
    monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", str(tmp_path / "missing" / "journal.jsonl"))
    get_settings.cache_clear()
    for _ in range(LOGIN_LIMIT + 2):
        response = login(c, "gate@example.test")
        assert response.status_code == 503 and response.json() == {"error": "service_recovering"}
    assert redis_env["client"].keys("*") == []


def test_limiter_logs_and_responses_hold_no_identifiers(limited, caplog):
    c = client("198.51.100.77")
    with caplog.at_level(logging.DEBUG):
        responses = [login(c, "private.person@example.test") for _ in range(LOGIN_LIMIT + 1)]
    text = caplog.text + json.dumps([r.text for r in responses])
    assert "private.person" not in text and "198.51.100.77" not in text
