"""Central config: MVP defaults from docs/decisions.md and boundary validation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import (
    MIB,
    AuthLifetimes,
    FileLimits,
    IngestPolicy,
    PasswordPolicy,
    PdfBodyThreshold,
    ProviderBudget,
    Retention,
    Settings,
)

URL = "postgresql+psycopg://u@localhost/db"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("RAS_"):
            monkeypatch.delenv(key)


def test_database_url_has_no_default() -> None:
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_defaults_match_documented_mvp_values() -> None:
    s = Settings(database_url=URL)
    assert s.auth.session_idle == timedelta(minutes=30)
    assert s.auth.session_absolute == timedelta(hours=8)
    assert s.auth.invite == timedelta(hours=72)
    assert s.auth.password_reset == timedelta(minutes=30)
    assert (s.files.pdf_max_bytes, s.files.pdf_max_pages, s.files.markdown_max_bytes) == (30 * MIB, 100, 2 * MIB)
    assert s.files.upload_intent_lifetime == timedelta(minutes=10)
    assert s.files.orphan_grace == timedelta(hours=24)
    assert s.files.pdf_probe_timeout_seconds == 10
    assert s.files.pdf_probe_memory_bytes == 256 * MIB
    assert s.upload_notice_version
    assert (s.pdf_body.min_body_page_ratio, s.pdf_body.min_chars_per_body_page, s.pdf_body.min_total_body_chars) == (
        0.70, 100, 2000
    )
    assert s.retention.deleted_cleanup_deadline == timedelta(hours=24)
    assert s.retention.backup_retention == timedelta(days=30)
    assert s.budget.app_daily_usd == 10.0
    assert s.budget.key_spending_cap_daily_usd == 50.0
    assert s.password.min_length == 12
    assert (s.ingest.poll_seconds, s.ingest.lease_seconds, s.ingest.max_attempts,
            s.ingest.markdown_max_chunk_chars) == (5, 120, 4, 1200)


def test_nested_values_override_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAS_DATABASE_URL", URL)
    monkeypatch.setenv("RAS_AUTH__SESSION_IDLE", "PT15M")
    monkeypatch.setenv("RAS_FILES__PDF_MAX_PAGES", "50")
    s = Settings()  # type: ignore[call-arg]
    assert s.auth.session_idle == timedelta(minutes=15)
    assert s.files.pdf_max_pages == 50


def test_redis_broker_password_is_url_escaped() -> None:
    s = Settings(database_url=URL, redis_host="redis", redis_password="a@b/c")
    assert s.celery_broker_url() == "redis://:a%40b%2Fc@redis:6379/0"


@pytest.mark.parametrize(
    "build",
    [
        lambda: AuthLifetimes(session_idle=timedelta(hours=9)),  # idle > absolute
        lambda: AuthLifetimes(invite=timedelta(0)),
        lambda: FileLimits(pdf_max_bytes=0),
        lambda: FileLimits(upload_intent_lifetime=timedelta(0)),
        lambda: FileLimits(orphan_grace=timedelta(minutes=10)),
        lambda: FileLimits(pdf_probe_timeout_seconds=0),
        lambda: PdfBodyThreshold(min_body_page_ratio=1.01),
        lambda: PdfBodyThreshold(min_body_page_ratio=0),
        lambda: Retention(backup_retention=timedelta(0)),
        lambda: ProviderBudget(generation_daily_usd=6.0),  # 3 + 2 + 6 > 10
        lambda: ProviderBudget(app_daily_usd=60.0),  # above the key cap
        lambda: PasswordPolicy(min_length=7),
        lambda: PasswordPolicy(min_length=20, max_length=19),
        lambda: IngestPolicy(max_attempts=0),
        lambda: IngestPolicy(retry_base_seconds=6, retry_max_seconds=5),
        lambda: IngestPolicy(pdf_parser_timeout_seconds=91),
    ],
)
def test_invalid_values_are_rejected(build) -> None:
    with pytest.raises(ValidationError):
        build()


def test_boundary_values_are_accepted() -> None:
    AuthLifetimes(session_idle=timedelta(hours=8))  # idle == absolute
    PdfBodyThreshold(min_body_page_ratio=1.0)
    ProviderBudget(embedding_daily_usd=3, rerank_daily_usd=2, generation_daily_usd=5)  # sum == app budget
    ProviderBudget(app_daily_usd=50.0, key_spending_cap_daily_usd=50.0)
    PasswordPolicy(min_length=8)


# --- M1b: environment, cookies, delivery ---------------------------------------


def test_production_defaults_are_safe() -> None:
    s = Settings(database_url=URL)
    assert s.environment == "production"
    assert s.http.cookie_secure is True
    assert s.delivery.mode == "disabled"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"http": {"cookie_secure": False}},  # insecure cookie in production
        {"delivery": {"mode": "dev_outbox"}, "http": {"public_base_url": "https://x.example"}},  # dev outbox in prod
        {"environment": "development", "delivery": {"mode": "dev_outbox"}},  # no base URL for links
        {"environment": "development", "http": {"allowed_origins": ["http://x.example/"]}},
        {"environment": "development", "http": {"public_base_url": "ftp://x.example"}},
        {"delivery": {"dev_outbox_max_entries": 0}},
    ],
)
def test_unsafe_auth_settings_are_rejected(kwargs) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=URL, **kwargs)


def test_development_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAS_DATABASE_URL", URL)
    monkeypatch.setenv("RAS_ENVIRONMENT", "development")
    monkeypatch.setenv("RAS_HTTP__COOKIE_SECURE", "false")
    monkeypatch.setenv("RAS_HTTP__PUBLIC_BASE_URL", "http://127.0.0.1:3000/")
    monkeypatch.setenv("RAS_HTTP__ALLOWED_ORIGINS", '["http://127.0.0.1:3000","http://localhost:3000"]')
    monkeypatch.setenv("RAS_DELIVERY__MODE", "dev_outbox")
    s = Settings()  # type: ignore[call-arg]
    assert s.http.public_base_url == "http://127.0.0.1:3000"
    assert s.http.allowed_origins == ["http://127.0.0.1:3000", "http://localhost:3000"]
    assert (s.delivery.mode, s.http.cookie_secure) == ("dev_outbox", False)
