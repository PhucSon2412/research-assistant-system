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
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
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
    pdf_probe_timeout_seconds: int = Field(default=10, gt=0)
    pdf_probe_memory_bytes: int = Field(default=256 * MIB, gt=0)
    orphan_grace: timedelta = timedelta(hours=24)

    @model_validator(mode="after")
    def _check_lifetime(self) -> "FileLimits":
        if self.upload_intent_lifetime <= timedelta(0):
            raise ValueError("upload_intent_lifetime must be positive")
        if self.orphan_grace <= self.upload_intent_lifetime:
            raise ValueError("orphan_grace must exceed upload_intent_lifetime")
        return self


class ObjectStorage(BaseModel):
    endpoint_url: str
    access_key: SecretStr
    secret_key: SecretStr
    bucket: str = "ras-originals"


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


class Retention(BaseModel):
    """Cleanup and backup windows (M6)."""

    deleted_cleanup_deadline: timedelta = timedelta(hours=24)
    backup_retention: timedelta = timedelta(days=30)

    @model_validator(mode="after")
    def _check_positive(self) -> "Retention":
        if self.deleted_cleanup_deadline <= timedelta(0) or self.backup_retention <= timedelta(0):
            raise ValueError("retention windows must be positive")
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
    max_output_usd_per_million_tokens: float = Field(default=10.0, gt=0)
    max_request_usd: float = Field(default=0.05, gt=0)

    @model_validator(mode="after")
    def _check_endpoint(self) -> "GenerationPolicy":
        if not self.endpoint.startswith("https://"):
            raise ValueError("generation endpoint must use HTTPS")
        return self


class HttpSecurity(BaseModel):
    """Cookie and request-origin policy (M1b).

    ``cookie_secure`` must stay true behind HTTPS; only the local HTTP dev
    stack turns it off. ``allowed_origins`` lists the browser origins whose
    state-changing requests are accepted (``Origin`` header check, on top of
    the CSRF token). ``public_base_url`` is the web origin used to build
    invitation/reset links.
    """

    cookie_secure: bool = True
    allowed_origins: list[str] = Field(default_factory=list)
    public_base_url: str | None = None

    @model_validator(mode="after")
    def _check_urls(self) -> "HttpSecurity":
        for origin in self.allowed_origins:
            if not origin.startswith(("http://", "https://")) or origin.endswith("/"):
                raise ValueError("allowed_origins entries must be scheme://host[:port] without a trailing slash")
        if self.public_base_url is not None:
            if not self.public_base_url.startswith(("http://", "https://")):
                raise ValueError("public_base_url must start with http:// or https://")
            self.public_base_url = self.public_base_url.rstrip("/")
        return self


class Delivery(BaseModel):
    """How invitation/reset links reach people (M1b).

    ``disabled``: nothing is sent; creating an invitation is refused.
    ``dev_outbox``: links are kept in API process memory for an Admin to read
    (development only; the real channel is still an open decision).
    """

    mode: Literal["disabled", "dev_outbox"] = "disabled"
    dev_outbox_max_entries: int = Field(default=200, gt=0, le=10_000)


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
    pdf_body: PdfBodyThreshold = PdfBodyThreshold()
    ingest: IngestPolicy = IngestPolicy()
    retention: Retention = Retention()
    budget: ProviderBudget = ProviderBudget()
    embedding: EmbeddingPolicy = EmbeddingPolicy()
    search: SearchPolicy = SearchPolicy()
    rerank: RerankPolicy = RerankPolicy()
    generation: GenerationPolicy = GenerationPolicy()
    http: HttpSecurity = HttpSecurity()
    delivery: Delivery = Delivery()
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
