"""Centralised configuration.

Every limit, threshold, lifetime and budget the product docs describe lives
here so it can be tested and changed in one place (see AGENTS.md). Values are
the MVP proposals from docs/decisions.md ("đề xuất, cần đo"); many are not
used by code until later milestones (M2 PDF body, M3 budget).

Secrets (database password, S3 keys, OpenRouter key) have no defaults and are
only provided through the environment.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit
import re
import ipaddress

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

MIB = 1024 * 1024


class DatabaseConnection(BaseModel):
    """Connection parts, passed separately so a password containing URL
    delimiters (``@ : / ? # %`` ...) is never spliced into a URL string.
    ``URL.create`` escapes each part when a driver string is rendered."""

    host: str = Field(min_length=1)
    port: int = Field(default=5432, gt=0, le=65535)
    user: str = Field(min_length=1)
    password: SecretStr
    name: str = Field(min_length=1)
    driver: str = "postgresql+psycopg"

    def url(self) -> URL:
        return URL.create(
            self.driver,
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.name,
        )


class PasswordPolicy(BaseModel):
    min_length: int = Field(default=12, ge=8)
    # Upper bound keeps hashing cost bounded against oversized input.
    max_length: int = Field(default=1024, le=4096)

    @model_validator(mode="after")
    def _check_range(self) -> "PasswordPolicy":
        if self.max_length < self.min_length:
            raise ValueError("password max_length must be >= min_length")
        return self


class AuthLifetimes(BaseModel):
    """Session/invite/reset lifetimes.

    A session ends ``session_idle`` after its last authenticated request or
    ``session_absolute`` after login, whichever comes first (server side)."""

    session_idle: timedelta = timedelta(minutes=30)
    session_absolute: timedelta = timedelta(hours=8)
    invite: timedelta = timedelta(hours=72)
    password_reset: timedelta = timedelta(minutes=30)

    @model_validator(mode="after")
    def _check_positive(self) -> "AuthLifetimes":
        for name in ("session_idle", "session_absolute", "invite", "password_reset"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.session_idle > self.session_absolute:
            raise ValueError("session_idle must not exceed session_absolute")
        return self


class FileLimits(BaseModel):
    """Upload limits (enforced from M1c)."""

    pdf_max_bytes: int = Field(default=30 * MIB, gt=0)
    pdf_max_pages: int = Field(default=100, gt=0)
    markdown_max_bytes: int = Field(default=2 * MIB, gt=0)
    upload_intent_lifetime: timedelta = timedelta(minutes=10)
    # Initial settings to measure; per-process staging, separate from downloads.
    upload_max_concurrent: int = Field(default=4, gt=0, le=64)
    upload_body_timeout_seconds: float = Field(default=90, gt=0, le=600)
    pdf_probe_timeout_seconds: int = Field(default=10, gt=0)
    pdf_probe_memory_bytes: int = Field(default=256 * MIB, gt=0)
    orphan_grace: timedelta = timedelta(hours=24)
    # C2d-5e2 manual orphan reconcile, bounded per pass (starting values, not
    # measured): objects per listing page (S3 caps at 1000), listing pages
    # per pass, and fenced keys taken to storage per pass (resumed + new).
    orphan_list_page_size: int = Field(default=500, gt=0, le=1000)
    orphan_max_pages: int = Field(default=20, gt=0, le=1000)
    orphan_max_deletes: int = Field(default=100, gt=0, le=10000)
    # M6b-2b1: a common original is read into a temp file and verified before
    # its first byte is sent. Per API process, at most this many such spools
    # exist at once (disk <= slots x the largest file limit); beyond it -> 503.
    verified_download_max_concurrent: int = Field(default=4, gt=0, le=64)
    # C2d-5b: initial values to measure, not an SLO. Approval copies use their
    # own per-process pool. The deadline only stops observing the copy phase;
    # the attempt still owns slot/temp/key until every storage thread settles.
    approval_copy_max_concurrent: int = Field(default=2, gt=0, le=64)
    approval_copy_deadline_seconds: float = Field(default=90, gt=0, le=600)
    # C2d-5c1, scoped starting values to measure, not an SLO. Observation
    # deadline does not kill an SDK thread; its attempt retains the slot.
    publication_verify_max_concurrent: int = Field(default=2, gt=0, le=64)
    publication_verify_deadline_seconds: float = Field(default=90, gt=0, le=600)

    @model_validator(mode="after")
    def _check_lifetime(self) -> "FileLimits":
        if self.upload_intent_lifetime <= timedelta(0):
            raise ValueError("upload_intent_lifetime must be positive")
        if self.orphan_grace <= self.upload_intent_lifetime:
            raise ValueError("orphan_grace must exceed upload_intent_lifetime")
        return self


class ReviewPolicy(BaseModel):
    """Owner review requests (M6a). There is deliberately no maximum open
    duration yet: that is an open decision (docs/decisions.md), and a request
    only grants the Admin a read while it stays open."""

    notice_version: str = Field(default="2026-09-29.v1", min_length=1)


class ObjectStorage(BaseModel):
    endpoint_url: str
    access_key: SecretStr
    secret_key: SecretStr
    bucket: str = "ras-originals"
    connect_timeout_seconds: int = Field(default=10, gt=0, le=120)
    read_timeout_seconds: int = Field(default=30, gt=0, le=120)
    max_attempts: int = Field(default=2, gt=0, le=5)


class PdfBodyThreshold(BaseModel):
    """Minimum extractable body text before a PDF may be indexed (M2)."""

    min_body_page_ratio: float = Field(default=0.70, gt=0, le=1)
    min_chars_per_body_page: int = Field(default=100, gt=0)
    min_total_body_chars: int = Field(default=2000, gt=0)


class IngestPolicy(BaseModel):
    poll_seconds: int = Field(default=5, gt=0)
    lease_seconds: int = Field(default=120, gt=0)
    max_attempts: int = Field(default=4, gt=0)
    retry_base_seconds: int = Field(default=5, gt=0)
    retry_max_seconds: int = Field(default=300, gt=0)
    dispatch_batch: int = Field(default=40, gt=0)
    markdown_max_chunk_chars: int = Field(default=1200, gt=0)
    pdf_parser_timeout_seconds: int = Field(default=60, gt=0)
    pdf_parser_memory_bytes: int = Field(default=512 * MIB, gt=0)
    pdf_parser_output_bytes: int = Field(default=64 * MIB, gt=0)
    pdf_parser_lease_margin_seconds: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _check_retry(self) -> "IngestPolicy":
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        if self.pdf_parser_timeout_seconds + self.pdf_parser_lease_margin_seconds > self.lease_seconds:
            raise ValueError("PDF parser timeout must leave the configured lease margin")
        return self


class DiscoveryPolicy(BaseModel):
    enabled: bool = True
    topics: list[str] = Field(default_factory=lambda: ["cs.AI", "cs.LG", "cs.CL"])
    initial_lookback_days: int = Field(default=7, ge=1, le=365)
    poll_seconds: int = Field(default=86400, ge=60)
    max_pages_per_topic: int = Field(default=2, ge=1, le=100)
    max_response_bytes: int = Field(default=4 * MIB, ge=1024, le=32 * MIB)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    http_deadline_seconds: int = Field(default=30, ge=1, le=180)
    lease_seconds: int = Field(default=90, ge=5, le=600)
    lease_margin_seconds: int = Field(default=10, ge=1, le=60)
    max_pass_seconds: int = Field(default=300, ge=1, le=3600)
    max_records_per_page: int = Field(default=5000, ge=1, le=50000)
    retarget_limit: int = Field(default=2000, ge=1, le=50000)
    retry_after_max_seconds: int = Field(default=86400, ge=1, le=604800)
    min_request_interval_seconds: int = Field(default=3, ge=3)
    retry_base_seconds: int = Field(default=60, ge=1)
    retry_max_seconds: int = Field(default=3600, ge=1)
    user_agent: str = "ResearchAssistantSystem/0.1 (metadata discovery)"

    @model_validator(mode="after")
    def _check(self) -> "DiscoveryPolicy":
        import re
        if not self.topics or len(set(self.topics)) != len(self.topics):
            raise ValueError("discovery topics must be nonempty and unique")
        if any(not re.fullmatch(r"[a-z-]+\.[A-Z][A-Z0-9-]*", t) for t in self.topics):
            raise ValueError("invalid arXiv category")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("discovery retry max must exceed base")
        if self.http_deadline_seconds + self.lease_margin_seconds >= self.lease_seconds:
            raise ValueError("discovery HTTP deadline must leave the lease margin")
        return self


class CitationPolicy(BaseModel):
    """M5b-1 exact arXiv reference links; bounded background reconciliation."""

    enabled: bool = True
    reconcile_poll_seconds: int = Field(default=300, ge=30, le=86400)
    reconcile_batch_size: int = Field(default=500, ge=1, le=5000)
    reconcile_max_batches: int = Field(default=20, ge=1, le=1000)


class Retention(BaseModel):
    """Cleanup and backup windows (M6).

    ``deleted_cleanup_deadline`` is the promise that a revoked common copy's
    object and derived data are gone (M6b-2a tombstone ``purge_due_at``); the
    beat task runs every ``cleanup_poll_seconds`` and retries until then.
    """

    deleted_cleanup_deadline: timedelta = timedelta(hours=24)
    backup_retention: timedelta = timedelta(days=30)
    cleanup_poll_seconds: int = Field(default=900, gt=0)
    cleanup_batch_size: int = Field(default=50, gt=0, le=1000)
    private_delete_manifest_limit: int = Field(default=128, ge=1, le=128)

    @model_validator(mode="after")
    def _check_positive(self) -> "Retention":
        if self.deleted_cleanup_deadline <= timedelta(0) or self.backup_retention <= timedelta(0):
            raise ValueError("retention windows must be positive")
        # Several attempts must fit inside the deadline.
        if timedelta(seconds=self.cleanup_poll_seconds * 4) > self.deleted_cleanup_deadline:
            raise ValueError("cleanup_poll_seconds must allow at least four attempts before the deadline")
        return self


class RecoveryPolicy(BaseModel):
    """Revocation journal and recovery gate (M6c-1).

    ``journal_path`` is the journal file, which must live outside the
    PostgreSQL data and the paper object store and outside their backups. With
    no path, or a path the process cannot read, the gate stays closed: the API
    serves only ``/healthz`` (and ``/readyz`` answers 503), workers process
    nothing. ``gate_lock_timeout_ms`` bounds how long a gate check waits for a
    revocation or recovery holding the journal lock before it counts as closed.
    ``cleanup_passes`` is how many cleanup rounds ``recover`` runs before it
    gives up and leaves the gate closed.
    """

    journal_path: Path | None = None
    gate_lock_timeout_ms: int = Field(default=2000, ge=50, le=30_000)
    cleanup_passes: int = Field(default=5, ge=1, le=100)

    @field_validator("journal_path", mode="before")
    @classmethod
    def _empty_path_is_absent(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_path(self) -> "RecoveryPolicy":
        if self.journal_path is not None and (not self.journal_path.is_absolute() or self.journal_path.name == ""):
            raise ValueError("recovery journal_path must be an absolute file path")
        return self


class AuthRecoveryPolicy(BaseModel):
    """Operator credential/initial-status recovery (C3).

    ``case_max_lifetime`` caps ``expires_at - approved_at`` of a recovery case;
    an approval is a short, single-use decision, never a standing permission.
    ``lock_timeout_ms`` bounds waits for the target User/token/session rows and
    the journal lock; a busy target is refused, never retried automatically.
    ``case_file_max_bytes`` bounds the restricted case file read by the CLI.
    """

    case_max_lifetime: timedelta = Field(default=timedelta(hours=24))
    lock_timeout_ms: int = Field(default=5000, ge=50, le=60_000)
    case_file_max_bytes: int = Field(default=4096, ge=512, le=65_536)

    @model_validator(mode="after")
    def _check_lifetime(self) -> "AuthRecoveryPolicy":
        if not timedelta(minutes=5) <= self.case_max_lifetime <= timedelta(days=7):
            raise ValueError("auth_recovery case_max_lifetime must be between 5 minutes and 7 days")
        return self


class ProviderBudget(BaseModel):
    """Application trial budget in USD/day (M3+).

    This is the project's own budget. It is separate from the OpenRouter key
    spending cap ($50/day, limit_reset=daily), which is *not* new credit.
    """

    app_daily_usd: float = Field(default=10.0, gt=0)
    embedding_daily_usd: float = Field(default=3.0, ge=0)
    embedding_warning_fraction: float = Field(default=0.8, gt=0, le=1)
    user_embedding_daily_usd: float = Field(default=1.0, ge=0)
    user_embedding_warning_fraction: float = Field(default=0.8, gt=0, le=1)
    rerank_daily_usd: float = Field(default=2.0, ge=0)
    rerank_warning_fraction: float = Field(default=0.8, gt=0, le=1)
    user_rerank_daily_usd: float = Field(default=0.5, ge=0)
    generation_daily_usd: float = Field(default=5.0, ge=0)
    user_generation_daily_usd: float = Field(default=1.0, ge=0)
    generation_warning_fraction: float = Field(default=0.8, gt=0, le=1)
    key_spending_cap_daily_usd: float = Field(default=50.0, gt=0)

    @model_validator(mode="after")
    def _check_split(self) -> "ProviderBudget":
        split = self.embedding_daily_usd + self.rerank_daily_usd + self.generation_daily_usd
        if split > self.app_daily_usd + 1e-9:
            raise ValueError("budget buckets must not exceed app_daily_usd")
        if self.app_daily_usd > self.key_spending_cap_daily_usd:
            raise ValueError("app_daily_usd must not exceed the key spending cap")
        if self.user_embedding_daily_usd > self.embedding_daily_usd:
            raise ValueError("user_embedding_daily_usd must not exceed embedding_daily_usd")
        if self.user_rerank_daily_usd > self.rerank_daily_usd:
            raise ValueError("user_rerank_daily_usd must not exceed rerank_daily_usd")
        if self.user_generation_daily_usd > self.generation_daily_usd:
            raise ValueError("user_generation_daily_usd must not exceed generation_daily_usd")
        return self


_PROVIDER_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}(/[a-z0-9][a-z0-9-]{0,63})?$")


class ProviderRouting(BaseModel):
    """M7a request-level OpenRouter provider policy, sent on every call of a route.

    Pins a nonempty set of reviewed provider slugs with no fallback, data
    collection denied and Zero Data Retention required; ``max_price`` is derived
    from the route's own configured price ceiling so a provider priced above
    the reservation estimate is never selected. Pinning a provider does not
    enable a route: ``pricing_verified`` stays the separate gate.
    """
    only: tuple[str, ...]
    zdr: Literal[True] = True
    data_collection: Literal["deny"] = "deny"
    allow_fallbacks: Literal[False] = False

    @field_validator("only")
    @classmethod
    def _slugs(cls, value):
        if not value or len(value) > 8 or len(set(value)) != len(value) or not all(
                isinstance(slug, str) and _PROVIDER_SLUG.fullmatch(slug) for slug in value):
            raise ValueError("routing.only needs 1-8 distinct lowercase provider slugs")
        return tuple(value)

    def body(self, max_price: dict, *, require_parameters: bool = False) -> dict:
        out = {"only": list(self.only), "allow_fallbacks": False, "data_collection": "deny",
               "zdr": True, "max_price": {k: format(v, "f") for k, v in max_price.items()}}
        if require_parameters:
            out["require_parameters"] = True
        return out


class EmbeddingPolicy(BaseModel):
    model_id: str = "openai/text-embedding-3-small"
    provider: str = "openrouter"
    endpoint: str = "https://openrouter.ai/api/v1/embeddings"
    api_key: SecretStr | None = None
    request_timeout_seconds: int = Field(default=30, gt=0)
    reservation_lease_seconds: int = Field(default=90, gt=0)
    max_concurrent_requests: int = Field(default=2, gt=0)
    max_batch_items: int = Field(default=1, ge=1, le=1)
    max_request_bytes: int = Field(default=16_384, gt=0)
    # Configured price ceiling/estimate; verify route price and usage with a tiny
    # synthetic request before setting pricing_verified for private documents.
    max_usd_per_million_tokens: float = Field(default=0.10, gt=0)
    pricing_verified: bool = False
    # M7a: OpenRouter lists only the Azure endpoint of this model as ZDR.
    routing: ProviderRouting = ProviderRouting(only=("azure",))

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_is_absent(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_endpoint(self) -> "EmbeddingPolicy":
        if not self.endpoint.startswith("https://"):
            raise ValueError("embedding endpoint must use HTTPS")
        return self


class SearchPolicy(BaseModel):
    max_query_bytes: int = Field(default=1024, gt=0)
    keyword_top_k: int = Field(default=30, gt=0, le=100)
    vector_top_k: int = Field(default=30, gt=0, le=100)
    max_results: int = Field(default=20, gt=0, le=100)
    max_requests_per_user_day: int = Field(default=200, gt=0)
    max_query_request_usd: float = Field(default=0.0002, gt=0)
    # Keyword query built from a question (app.keyword_query): distinct useful terms kept in
    # input order, their length bounds, and how many kept terms a chunk must match (the share,
    # at least keyword_min_match_terms, at most all kept terms).
    keyword_max_terms: int = Field(default=8, gt=0, le=32)
    keyword_min_term_chars: int = Field(default=2, gt=0)
    keyword_max_term_chars: int = Field(default=64, gt=0, le=256)
    keyword_min_match_fraction: float = Field(default=0.5, gt=0, le=1)
    keyword_min_match_terms: int = Field(default=2, gt=0)

    @model_validator(mode="after")
    def _check_keyword_terms(self) -> "SearchPolicy":
        if self.keyword_min_term_chars > self.keyword_max_term_chars:
            raise ValueError("keyword_min_term_chars must not exceed keyword_max_term_chars")
        return self


class RerankPolicy(BaseModel):
    model_id: str = "qwen/qwen3-reranker-8b"
    endpoint: str = "https://openrouter.ai/api/v1/rerank"
    pricing_verified: bool = False
    max_concurrent_requests: int = Field(default=2, gt=0)
    max_candidates: int = Field(default=12, gt=0, le=50)
    max_text_bytes: int = Field(default=2048, gt=0)
    request_timeout_seconds: int = Field(default=20, gt=0)
    reservation_lease_seconds: int = Field(default=60, gt=0)
    max_usd_per_million_tokens: float = Field(default=0.20, gt=0)
    max_request_usd: float = Field(default=0.01, gt=0)
    # M7a: the only listed endpoint (Fireworks) is also OpenRouter's ZDR endpoint.
    routing: ProviderRouting = ProviderRouting(only=("fireworks",))

    @model_validator(mode="after")
    def _check_endpoint(self) -> "RerankPolicy":
        if not self.endpoint.startswith("https://"):
            raise ValueError("rerank endpoint must use HTTPS")
        return self


class GenerationPolicy(BaseModel):
    model_id: str = "openai/gpt-6-sol"
    endpoint: str = "https://openrouter.ai/api/v1/chat/completions"
    pricing_verified: bool = False
    max_question_bytes: int = Field(default=1024, gt=0)
    max_evidence_chunks: int = Field(default=6, gt=0, le=30)
    max_evidence_papers: int = Field(default=3, gt=0, le=30)
    max_evidence_bytes: int = Field(default=12_000, gt=0)
    max_input_tokens_estimate: int = Field(default=16000, gt=0)
    max_output_tokens: int = Field(default=512, gt=0)
    max_response_bytes: int = Field(default=16_384, gt=0)
    max_claims: int = Field(default=8, gt=0)
    max_requests_per_user_day: int = Field(default=50, gt=0)
    max_concurrent_requests: int = Field(default=2, gt=0)
    request_timeout_seconds: int = Field(default=60, gt=0)
    reservation_lease_seconds: int = Field(default=100, gt=0)
    max_input_usd_per_million_tokens: float = Field(default=2.0, gt=0)
    # M7a live charge: prompts above the provider's cache threshold were billed at
    # the endpoint's cache-write price (listed $2.5/M for the reviewed Azure
    # endpoint), which max_price.prompt does not bound. Input is estimated at the
    # higher of the two so reservation and settlement never fall below the charge.
    max_input_cache_write_usd_per_million_tokens: float = Field(default=2.5, gt=0)
    max_output_usd_per_million_tokens: float = Field(default=10.0, gt=0)
    max_request_usd: float = Field(default=0.05, gt=0)
    # M7a: OpenRouter's ZDR endpoints of the model are Azure; max_price excludes
    # the Azure variants priced above the configured ceilings.
    routing: ProviderRouting = ProviderRouting(only=("azure",))

    @property
    def input_usd_per_million_ceiling(self) -> float:
        return max(self.max_input_usd_per_million_tokens, self.max_input_cache_write_usd_per_million_tokens)

    @model_validator(mode="after")
    def _check_endpoint(self) -> "GenerationPolicy":
        if not self.endpoint.startswith("https://"):
            raise ValueError("generation endpoint must use HTTPS")
        if self.max_evidence_papers > self.max_evidence_chunks:
            raise ValueError("max_evidence_papers must not exceed max_evidence_chunks")
        return self


def normalise_origin(value: str) -> str:
    """Canonical HTTP(S) scheme/host/effective port, with no URL extras."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value) or any(c in value for c in ('\\', '%', '*', '?', '#')):
        raise ValueError("invalid HTTP origin")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must be HTTP(S) scheme://host[:port]")
    host = parsed.hostname
    if not host or parsed.netloc.endswith(':'):
        raise ValueError("invalid origin host/port")
    try:
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid origin port")
        if ':' in host:
            host = '[' + ipaddress.IPv6Address(host).compressed + ']'
        else:
            host = host.encode('idna').decode('ascii').lower()
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label) for label in host.split('.')):
                raise ValueError("invalid origin hostname")
    except (UnicodeError, ValueError):
        raise ValueError("invalid origin host/port") from None
    scheme = parsed.scheme.lower()
    port = port or (443 if scheme == 'https' else 80)
    suffix = '' if port == (443 if scheme == 'https' else 80) else f':{port}'
    return f'{scheme}://{host}{suffix}'


def origin_allowed(value: str | None, allowed: list[str]) -> bool:
    try:
        return normalise_origin(value) in allowed
    except (ValueError, TypeError):
        return False


class HttpSecurity(BaseModel):
    """Cookie and request-origin policy (M1b).

    ``cookie_secure`` must stay true behind HTTPS; only the local HTTP dev
    stack turns it off. ``allowed_origins`` lists the browser origins whose
    state-changing requests are accepted (``Origin`` header check, on top of
    the CSRF token). ``public_base_url`` is the web origin used to build
    invitation/reset links.

    ``trusted_proxy_cidrs`` (M8a) lists the exact proxy addresses (the web
    container in the pilot deployment) whose single ``X-RAS-Client-IP`` header
    names the browser address for auth rate limiting. A request from any other
    peer is keyed by its own TCP address and the header is ignored; the API
    never reads ``X-Forwarded-For``. Empty means trust no proxy.
    """

    cookie_secure: bool = True
    allowed_origins: list[str] = Field(default_factory=list)
    public_base_url: str | None = None
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_urls(self) -> "HttpSecurity":
        self.allowed_origins = list(dict.fromkeys(normalise_origin(o) for o in self.allowed_origins))
        if self.public_base_url is not None:
            if not self.public_base_url.startswith(("http://", "https://")):
                raise ValueError("public_base_url must start with http:// or https://")
            self.public_base_url = self.public_base_url.rstrip("/")
        networks = []
        for raw in self.trusted_proxy_cidrs:
            try:
                network = ipaddress.ip_network(raw.strip(), strict=False)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("trusted_proxy_cidrs entries must be IP networks") from None
            if network.prefixlen == 0:
                raise ValueError("trusted_proxy_cidrs must not trust every address")
            networks.append(str(network))
        self.trusted_proxy_cidrs = list(dict.fromkeys(networks))
        return self


class SmtpSettings(BaseModel):
    """M8a SMTP channel. TLS is mandatory and always verified: ``implicit_tls``
    (SMTPS, usually 465) or ``starttls`` (usually 587, refused when the server
    does not offer it). There is no plaintext mode and no switch to skip
    certificate or hostname verification. ``ca_file`` replaces the system trust
    store (a private relay CA, or the isolated test CA); without it the image's
    system CAs are used. The password is read from ``password_file`` at each
    send, never from argv or a plain variable. Timeouts are initial operational
    values, not measured SLOs; ``total_timeout_seconds`` bounds the whole send.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(gt=0, le=65535)
    security: Literal["implicit_tls", "starttls"]
    sender: str = Field(min_length=3, max_length=320)
    sender_name: str | None = Field(default=None, max_length=120)
    username: str | None = Field(default=None, min_length=1, max_length=320)
    password_file: Path | None = None
    ca_file: Path | None = None
    helo_name: str | None = Field(default=None, max_length=253)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    total_timeout_seconds: float = Field(default=10, gt=0, le=60)
    password_max_bytes: int = Field(default=4096, ge=16, le=65536)

    @field_validator("password_file", "ca_file", "username", "sender_name", "helo_name", mode="before")
    @classmethod
    def _empty_is_absent(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def _check(self) -> "SmtpSettings":
        for name in ("host", "sender", "sender_name", "username", "helo_name"):
            value = getattr(self, name)
            if value is not None and any(c in value for c in "\r\n\0"):
                raise ValueError(f"smtp {name} must be a single line")
        if not re.fullmatch(r"[^@\s<>,;\"]+@[^@\s<>,;\"]+", self.sender):
            raise ValueError("smtp sender must be a bare email address")
        if self.helo_name is not None and not re.fullmatch(r"[A-Za-z0-9.-]+", self.helo_name):
            raise ValueError("smtp helo_name must be a hostname")
        if (self.username is None) != (self.password_file is None):
            raise ValueError("smtp username and password_file go together")
        for name in ("password_file", "ca_file"):
            path = getattr(self, name)
            if path is not None and not path.is_absolute():
                raise ValueError(f"smtp {name} must be an absolute path")
        if self.connect_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("smtp connect timeout must not exceed the total timeout")
        return self


class Delivery(BaseModel):
    """How invitation/reset links reach people (M1b, M8a).

    ``disabled``: nothing is sent; creating an invitation is refused.
    ``dev_outbox``: links are kept in API process memory for an Admin to read
    (development only).
    ``smtp`` (M8a): one message per link through ``smtp`` over verified TLS.
    The server accepting a message is not proof the recipient received it.
    """

    mode: Literal["disabled", "dev_outbox", "smtp"] = "disabled"
    dev_outbox_max_entries: int = Field(default=200, gt=0, le=10_000)
    smtp: SmtpSettings | None = None

    @model_validator(mode="after")
    def _check_smtp(self) -> "Delivery":
        if self.mode == "smtp" and self.smtp is None:
            raise ValueError("RAS_DELIVERY__MODE=smtp needs RAS_DELIVERY__SMTP__* settings")
        return self


class RateRule(BaseModel):
    """At most ``limit`` requests per fixed ``window_seconds`` window."""

    limit: int = Field(gt=0, le=100_000)
    window_seconds: int = Field(gt=0, le=86_400)


class RateLimitPolicy(BaseModel):
    """M8a auth rate limits, shared by every API process through Redis.

    Buckets are per source address (IPv6 by ``ipv6_prefix``) and, where an
    account is named, per HMAC of the normalised email, whether or not that
    account exists. Keys hold only HMAC digests made with the secret read from
    ``key_secret_file``. Defaults are initial operational values for a small
    pilot (sized so a shared NAT is not throttled by one quota), not a
    benchmark or SLO. ``disabled`` is refused in production; a missing secret
    or an unreachable Redis makes the protected auth routes answer 503, never
    an unlimited pass.
    """

    mode: Literal["disabled", "redis"] = "redis"
    key_secret_file: Path | None = None
    key_secret_min_bytes: int = Field(default=32, ge=32, le=1024)
    redis_db: int = Field(default=1, ge=0, le=15)
    redis_timeout_seconds: float = Field(default=0.5, gt=0, le=5)
    ipv6_prefix: int = Field(default=64, ge=32, le=128)
    login_source: RateRule = RateRule(limit=30, window_seconds=300)
    login_account: RateRule = RateRule(limit=10, window_seconds=900)
    reset_request_source: RateRule = RateRule(limit=10, window_seconds=900)
    reset_request_account: RateRule = RateRule(limit=3, window_seconds=3600)
    token_source: RateRule = RateRule(limit=20, window_seconds=900)

    @field_validator("key_secret_file", mode="before")
    @classmethod
    def _empty_path_is_absent(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_path(self) -> "RateLimitPolicy":
        if self.key_secret_file is not None and not self.key_secret_file.is_absolute():
            raise ValueError("rate_limit key_secret_file must be an absolute path")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Safe by default: "production" forces secure cookies and forbids the dev
    # outbox. The Compose dev stack sets RAS_ENVIRONMENT=development.
    environment: Literal["development", "production"] = "production"

    # Exactly one of these is required; no default so a missing value fails
    # loudly instead of silently connecting with a built-in credential.
    # Prefer ``db`` (RAS_DB__HOST/PORT/USER/PASSWORD/NAME). ``database_url``
    # (RAS_DATABASE_URL) must already be percent-encoded.
    db: DatabaseConnection | None = None
    database_url: SecretStr | None = None

    password: PasswordPolicy = PasswordPolicy()
    auth: AuthLifetimes = AuthLifetimes()
    files: FileLimits = FileLimits()
    upload_notice_version: str = Field(default="2026-09-24.v1", min_length=1)
    review: ReviewPolicy = ReviewPolicy()
    pdf_body: PdfBodyThreshold = PdfBodyThreshold()
    ingest: IngestPolicy = IngestPolicy()
    discovery: DiscoveryPolicy = DiscoveryPolicy()
    citations: CitationPolicy = CitationPolicy()
    retention: Retention = Retention()
    recovery: RecoveryPolicy = RecoveryPolicy()
    auth_recovery: AuthRecoveryPolicy = AuthRecoveryPolicy()
    budget: ProviderBudget = ProviderBudget()
    embedding: EmbeddingPolicy = EmbeddingPolicy()
    search: SearchPolicy = SearchPolicy()
    rerank: RerankPolicy = RerankPolicy()
    generation: GenerationPolicy = GenerationPolicy()
    http: HttpSecurity = HttpSecurity()
    delivery: Delivery = Delivery()
    rate_limit: RateLimitPolicy = RateLimitPolicy()
    storage: ObjectStorage | None = None
    redis_host: str = "redis"
    redis_port: int = Field(default=6379, gt=0, le=65535)
    redis_password: SecretStr | None = None

    @model_validator(mode="after")
    def _check_database(self) -> "Settings":
        if (self.db is None) == (self.database_url is None):
            raise ValueError("set exactly one of RAS_DB__* (host/user/password/name) or RAS_DATABASE_URL")
        return self

    @model_validator(mode="after")
    def _check_environment(self) -> "Settings":
        if self.embedding.request_timeout_seconds + 10 >= self.ingest.lease_seconds:
            raise ValueError("embedding timeout must leave at least 10 seconds of lease margin")
        if self.embedding.reservation_lease_seconds <= self.embedding.request_timeout_seconds + 10:
            raise ValueError("embedding reservation lease must exceed provider timeout by 10 seconds")
        if self.rerank.reservation_lease_seconds <= self.rerank.request_timeout_seconds + 10:
            raise ValueError("rerank reservation lease must exceed provider timeout by 10 seconds")
        if self.generation.reservation_lease_seconds <= self.generation.request_timeout_seconds + 10:
            raise ValueError("generation reservation lease must exceed provider timeout by 10 seconds")
        if self.environment == "production":
            if not self.http.cookie_secure:
                raise ValueError("production requires RAS_HTTP__COOKIE_SECURE=true")
            if self.delivery.mode == "dev_outbox":
                raise ValueError("the dev outbox is only allowed with RAS_ENVIRONMENT=development")
            if self.rate_limit.mode == "disabled":
                raise ValueError("production requires the Redis auth rate limiter")
            # M8a: browser origins and the link base are explicit HTTPS origins.
            if any(not o.startswith("https://") for o in self.http.allowed_origins):
                raise ValueError("production allowed_origins must all be https://")
            if self.http.public_base_url is not None:
                try:
                    canonical = normalise_origin(self.http.public_base_url)
                except ValueError:
                    raise ValueError("production public_base_url must be an https:// origin") from None
                if not canonical.startswith("https://") or canonical not in self.http.allowed_origins:
                    raise ValueError("production public_base_url must be an https:// origin listed in allowed_origins")
                self.http.public_base_url = canonical
        if self.delivery.mode != "disabled" and self.http.public_base_url is None:
            raise ValueError("RAS_HTTP__PUBLIC_BASE_URL is required to deliver invitation/reset links")
        return self

    def sqlalchemy_url(self) -> URL:
        if self.db is not None:
            return self.db.url()
        return make_url(self.database_url.get_secret_value())  # type: ignore[union-attr]

    def celery_broker_url(self) -> str:
        if self.redis_password is None:
            raise ValueError("RAS_REDIS_PASSWORD is required for the worker")
        return f"redis://:{quote(self.redis_password.get_secret_value(), safe='')}@{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
