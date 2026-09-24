"""Centralised configuration.

Every limit, threshold, lifetime and budget the product docs describe lives
here so it can be tested and changed in one place (see AGENTS.md). Values are
the MVP proposals from docs/decisions.md ("đề xuất, cần đo"); many are not
used by code until later milestones (M1b auth, M1c upload, M2 PDF, M3 budget).

Secrets (database password, S3 keys, OpenRouter key) have no defaults and are
only provided through the environment.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    """Session/invite/reset lifetimes (used from M1b)."""

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


class PdfBodyThreshold(BaseModel):
    """Minimum extractable body text before a PDF may be indexed (M2)."""

    min_body_page_ratio: float = Field(default=0.70, gt=0, le=1)
    min_chars_per_body_page: int = Field(default=100, gt=0)
    min_total_body_chars: int = Field(default=2000, gt=0)


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
    rerank_daily_usd: float = Field(default=2.0, ge=0)
    generation_daily_usd: float = Field(default=5.0, ge=0)
    key_spending_cap_daily_usd: float = Field(default=50.0, gt=0)

    @model_validator(mode="after")
    def _check_split(self) -> "ProviderBudget":
        split = self.embedding_daily_usd + self.rerank_daily_usd + self.generation_daily_usd
        if split > self.app_daily_usd + 1e-9:
            raise ValueError("budget buckets must not exceed app_daily_usd")
        if self.app_daily_usd > self.key_spending_cap_daily_usd:
            raise ValueError("app_daily_usd must not exceed the key spending cap")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Exactly one of these is required; no default so a missing value fails
    # loudly instead of silently connecting with a built-in credential.
    # Prefer ``db`` (RAS_DB__HOST/PORT/USER/PASSWORD/NAME). ``database_url``
    # (RAS_DATABASE_URL) must already be percent-encoded.
    db: DatabaseConnection | None = None
    database_url: SecretStr | None = None

    password: PasswordPolicy = PasswordPolicy()
    auth: AuthLifetimes = AuthLifetimes()
    files: FileLimits = FileLimits()
    pdf_body: PdfBodyThreshold = PdfBodyThreshold()
    retention: Retention = Retention()
    budget: ProviderBudget = ProviderBudget()

    @model_validator(mode="after")
    def _check_database(self) -> "Settings":
        if (self.db is None) == (self.database_url is None):
            raise ValueError("set exactly one of RAS_DB__* (host/user/password/name) or RAS_DATABASE_URL")
        return self

    def sqlalchemy_url(self) -> URL:
        if self.db is not None:
            return self.db.url()
        return make_url(self.database_url.get_secret_value())  # type: ignore[union-attr]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
