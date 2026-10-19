"""Auth rate limits shared by every API process through Redis (M8a).

One Lua script increments every bucket of a request and reads each TTL in a
single atomic step, so concurrent processes cannot both pass a full bucket
and a window always ends (``PEXPIRE`` on the first hit, repaired when a key
lost its TTL). Windows are fixed: a denied request still counts, a success
(login, reset) never clears a counter.

Keys hold only HMAC-SHA256 digests under a deployment secret: no email,
token, cookie or raw IP reaches Redis keys, logs or responses. The account
bucket is keyed by the normalised email whether or not an account exists, so
existence is never revealed by a different policy.

Failure policy: a missing secret, an unreachable Redis, a timeout or a
script error raises ``RateLimitUnavailable`` (503 at the route); a request is
never let through unlimited. No database connection is opened before the
check: routes call it first, and the Session dependency connects lazily.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import math
import threading
from enum import StrEnum
from functools import lru_cache

from app.config import RateLimitPolicy, RateRule, Settings, get_settings

log = logging.getLogger(__name__)

CLIENT_IP_HEADER = "x-ras-client-ip"
KEY_PREFIX = "ras:rl:v1"

# KEYS: bucket keys. ARGV: limit_1, window_ms_1, limit_2, window_ms_2, ...
# Returns 0 when every bucket admits the request, otherwise the largest
# remaining window (ms) among the buckets that are over their limit.
_SCRIPT = """
local deny = 0
for i, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[2 * i - 1])
  local window = tonumber(ARGV[2 * i])
  local count = redis.call('INCR', key)
  local ttl = redis.call('PTTL', key)
  if ttl < 0 then
    redis.call('PEXPIRE', key, window)
    ttl = window
  end
  if count > limit and ttl > deny then
    deny = ttl
  end
end
return deny
"""


class RateLimitUnavailable(Exception):
    """The limiter could not decide; the protected route answers 503."""


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class AuthAction(StrEnum):
    LOGIN = "login"
    RESET_REQUEST = "reset_request"
    TOKEN = "token"  # invitation inspect/accept and reset confirm


def _rules(policy: RateLimitPolicy, action: AuthAction) -> tuple[RateRule, RateRule | None]:
    if action is AuthAction.LOGIN:
        return policy.login_source, policy.login_account
    if action is AuthAction.RESET_REQUEST:
        return policy.reset_request_source, policy.reset_request_account
    return policy.token_source, None


def source_bucket(address: str, ipv6_prefix: int) -> str:
    """Canonical source: an IPv4 address, or the IPv6 network of ``ipv6_prefix``."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((ip, ipv6_prefix), strict=False))
    return str(ip)


def account_bucket(email: str) -> str:
    """Same normalisation as login/reset, applied even when the format is invalid."""
    return email.strip().lower()


def client_address(peer: str | None, header_values: list[str], trusted: list[str]) -> str:
    """The TCP peer, or the single client address named by a trusted proxy.

    Only a peer inside ``trusted`` may name the client, with exactly one
    well-formed ``X-RAS-Client-IP``; anything else keeps the peer address.
    ``X-Forwarded-For`` and similar headers are never read."""
    if peer is None:
        return "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"
    if any(peer_ip in ipaddress.ip_network(cidr) for cidr in trusted):
        if len(header_values) == 1:
            try:
                return str(ipaddress.ip_address(header_values[0].strip()))
            except ValueError:
                pass
        log.warning("trusted proxy request without a valid client address header")
    return str(peer_ip)


class RedisRateLimiter:
    enabled = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._policy = settings.rate_limit
        self._lock = threading.Lock()
        self._client = None
        self._script = None

    def _secret(self) -> bytes:
        path = self._policy.key_secret_file
        if path is None:
            raise RateLimitUnavailable("rate_limit_secret_unconfigured")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(4097)
        except OSError:
            raise RateLimitUnavailable("rate_limit_secret_unavailable") from None
        secret = raw.rstrip(b"\r\n")
        if len(raw) > 4096 or len(secret) < self._policy.key_secret_min_bytes:
            raise RateLimitUnavailable("rate_limit_secret_invalid")
        return secret

    def _redis(self):
        with self._lock:
            if self._script is None:
                import redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry

                password = self._settings.redis_password
                self._client = redis.Redis(
                    host=self._settings.redis_host, port=self._settings.redis_port,
                    password=password.get_secret_value() if password else None,
                    db=self._policy.redis_db,
                    socket_timeout=self._policy.redis_timeout_seconds,
                    socket_connect_timeout=self._policy.redis_timeout_seconds,
                    # INCR is not idempotent: one attempt, the caller answers 503.
                    retry=Retry(NoBackoff(), 0),
                    health_check_interval=0)
                self._script = self._client.register_script(_SCRIPT)
            return self._script

    def check(self, action: AuthAction, *, source: str, account: str | None = None) -> None:
        secret = self._secret()
        source_rule, account_rule = _rules(self._policy, action)

        def key(kind: str, value: str) -> str:
            digest = hmac.new(secret, f"{action.value}\0{kind}\0{value}".encode(), hashlib.sha256).hexdigest()
            return f"{KEY_PREFIX}:{action.value}:{kind}:{digest}"

        keys = [key("source", source_bucket(source, self._policy.ipv6_prefix))]
        args: list[int] = [source_rule.limit, source_rule.window_seconds * 1000]
        if account_rule is not None and account is not None:
            keys.append(key("account", account_bucket(account)))
            args += [account_rule.limit, account_rule.window_seconds * 1000]
        try:
            script = self._redis()
            deny_ms = int(script(keys=keys, args=args))
        except RateLimitUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - redis/socket errors only reach here
            log.error("auth rate limiter unavailable (%s)", type(exc).__name__)
            raise RateLimitUnavailable("rate_limit_unavailable") from None
        if deny_ms > 0:
            raise RateLimited(max(1, math.ceil(deny_ms / 1000)))


class DisabledRateLimiter:
    """Development only: the settings refuse ``disabled`` in production."""

    enabled = False

    def check(self, action: AuthAction, *, source: str, account: str | None = None) -> None:
        return None


def make_rate_limiter(settings: Settings):
    if settings.rate_limit.mode == "disabled":
        return DisabledRateLimiter()
    return RedisRateLimiter(settings)


@lru_cache
def get_rate_limiter():
    return make_rate_limiter(get_settings())
