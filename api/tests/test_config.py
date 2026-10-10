"""Central config: MVP defaults from docs/decisions.md and boundary validation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import (
    MIB,
    AuthLifetimes,
    FileLimits,
    EmbeddingPolicy,
    IngestPolicy,
    PasswordPolicy,
    PdfBodyThreshold,
    ProviderBudget,
    RerankPolicy,
    GenerationPolicy,
    RecoveryPolicy,
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
    # C2d-5e2: bounded manual orphan reconcile pass.
    assert (s.files.orphan_list_page_size, s.files.orphan_max_pages, s.files.orphan_max_deletes) == (500, 20, 100)
    assert s.files.pdf_probe_timeout_seconds == 10
    assert s.files.pdf_probe_memory_bytes == 256 * MIB
    # M6b-2b1: verified common downloads spooled at once per API process.
    assert s.files.verified_download_max_concurrent == 4
    assert s.upload_notice_version
    # M6a: a review notice version, and no maximum open duration (open decision).
    assert s.review.notice_version and set(s.review.model_dump()) == {"notice_version"}
    assert (s.pdf_body.min_body_page_ratio, s.pdf_body.min_chars_per_body_page, s.pdf_body.min_total_body_chars) == (
        0.70, 100, 2000
    )
    assert s.retention.deleted_cleanup_deadline == timedelta(hours=24)
    assert s.retention.backup_retention == timedelta(days=30)
    assert (s.retention.cleanup_poll_seconds, s.retention.cleanup_batch_size) == (900, 50)
    assert s.budget.app_daily_usd == 10.0
    assert s.budget.key_spending_cap_daily_usd == 50.0
    assert s.budget.embedding_daily_usd == 3.0
    assert s.budget.embedding_warning_fraction == 0.8
    assert s.budget.user_embedding_daily_usd == 1.0
    assert s.budget.user_embedding_warning_fraction == 0.8
    assert s.embedding.model_id == "openai/text-embedding-3-small"
    assert s.embedding.max_batch_items == 1
    assert s.embedding.api_key is None
    assert s.embedding.pricing_verified is False
    assert s.rerank.pricing_verified is False
    assert s.rerank.model_id == "qwen/qwen3-reranker-8b"
    assert s.search.max_requests_per_user_day > 0
    assert s.budget.user_rerank_daily_usd <= s.budget.rerank_daily_usd
    assert s.budget.rerank_warning_fraction == 0.8
    assert s.rerank.max_concurrent_requests == 2
    assert s.generation.model_id == "openai/gpt-6-sol"
    assert s.generation.pricing_verified is False
    assert s.budget.generation_daily_usd == 5.0
    assert s.budget.user_generation_daily_usd <= s.budget.generation_daily_usd
    assert s.generation.max_evidence_chunks >= s.generation.max_evidence_papers > 0
    assert s.generation.max_evidence_bytes > 0
    assert s.generation.max_input_tokens_estimate > s.generation.max_question_bytes
    assert s.generation.max_output_tokens > 0
    assert s.embedding.reservation_lease_seconds > s.embedding.request_timeout_seconds + 10
    assert s.rerank.reservation_lease_seconds > s.rerank.request_timeout_seconds + 10
    assert s.password.min_length == 12
    assert (s.ingest.poll_seconds, s.ingest.lease_seconds, s.ingest.max_attempts,
            s.ingest.markdown_max_chunk_chars) == (5, 120, 4, 1200)


def test_nested_values_override_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAS_DATABASE_URL", URL)
    monkeypatch.setenv("RAS_AUTH__SESSION_IDLE", "PT15M")
    monkeypatch.setenv("RAS_FILES__PDF_MAX_PAGES", "50")
    monkeypatch.setenv("RAS_BUDGET__EMBEDDING_WARNING_FRACTION", "0.75")
    s = Settings()  # type: ignore[call-arg]
    assert s.auth.session_idle == timedelta(minutes=15)
    assert s.files.pdf_max_pages == 50
    assert s.budget.embedding_warning_fraction == 0.75


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
        lambda: FileLimits(orphan_list_page_size=0),
        lambda: FileLimits(orphan_list_page_size=1001),
        lambda: FileLimits(orphan_max_pages=0),
        lambda: FileLimits(orphan_max_pages=1001),
        lambda: FileLimits(orphan_max_deletes=0),
        lambda: FileLimits(orphan_max_deletes=10001),
        lambda: FileLimits(pdf_probe_timeout_seconds=0),
        lambda: FileLimits(verified_download_max_concurrent=0),
        lambda: FileLimits(verified_download_max_concurrent=65),
        lambda: PdfBodyThreshold(min_body_page_ratio=1.01),
        lambda: PdfBodyThreshold(min_body_page_ratio=0),
        lambda: Retention(backup_retention=timedelta(0)),
        lambda: Retention(cleanup_poll_seconds=6 * 3600 + 1),  # fewer than four tries in 24 h
        lambda: Retention(cleanup_batch_size=0),
        lambda: ProviderBudget(generation_daily_usd=6.0),  # 3 + 2 + 6 > 10
        lambda: ProviderBudget(app_daily_usd=60.0),  # above the key cap
        lambda: ProviderBudget(user_embedding_daily_usd=4.0),
        lambda: ProviderBudget(user_rerank_daily_usd=3.0),
        lambda: ProviderBudget(user_generation_daily_usd=6.0),
        lambda: ProviderBudget(generation_warning_fraction=0),
        lambda: GenerationPolicy(max_evidence_chunks=0),
        lambda: GenerationPolicy(max_evidence_papers=7),
        lambda: GenerationPolicy(max_output_tokens=0),
        lambda: GenerationPolicy(endpoint="http://example.org/chat"),
        lambda: ProviderBudget(rerank_warning_fraction=0),
        lambda: ProviderBudget(rerank_warning_fraction=1.01),
        lambda: ProviderBudget(embedding_warning_fraction=0),
        lambda: ProviderBudget(embedding_warning_fraction=1.01),
        lambda: ProviderBudget(user_embedding_warning_fraction=1.01),
        lambda: PasswordPolicy(min_length=7),
        lambda: PasswordPolicy(min_length=20, max_length=19),
        lambda: IngestPolicy(max_attempts=0),
        lambda: IngestPolicy(retry_base_seconds=6, retry_max_seconds=5),
        lambda: IngestPolicy(pdf_parser_timeout_seconds=91),
        lambda: EmbeddingPolicy(max_batch_items=2),
        lambda: EmbeddingPolicy(endpoint="http://example.org/embeddings"),
        lambda: EmbeddingPolicy(max_concurrent_requests=0),
        lambda: RerankPolicy(max_concurrent_requests=0),
        lambda: EmbeddingPolicy(reservation_lease_seconds=0),
        lambda: RerankPolicy(reservation_lease_seconds=0),
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
    assert EmbeddingPolicy(api_key="").api_key is None
    with pytest.raises(ValidationError):
        Settings(database_url=URL, embedding={"request_timeout_seconds": 110})
    with pytest.raises(ValidationError):
        Settings(database_url=URL, embedding={"reservation_lease_seconds": 40})
    with pytest.raises(ValidationError):
        Settings(database_url=URL, rerank={"reservation_lease_seconds": 30})
    with pytest.raises(ValidationError):
        Settings(database_url=URL, generation={"reservation_lease_seconds": 60})


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


def test_keyword_query_limits_defaults_environment_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(database_url=URL)
    assert (s.search.keyword_max_terms, s.search.keyword_min_term_chars, s.search.keyword_max_term_chars,
            s.search.keyword_min_match_fraction, s.search.keyword_min_match_terms) == (8, 2, 64, 0.5, 2)
    monkeypatch.setenv("RAS_DATABASE_URL", URL)
    monkeypatch.setenv("RAS_SEARCH__KEYWORD_MAX_TERMS", "5")
    monkeypatch.setenv("RAS_SEARCH__KEYWORD_MIN_MATCH_FRACTION", "0.75")
    s = Settings()  # type: ignore[call-arg]
    assert (s.search.keyword_max_terms, s.search.keyword_min_match_fraction) == (5, 0.75)
    for bad in ({"keyword_max_terms": 0}, {"keyword_max_terms": 33}, {"keyword_min_match_fraction": 0},
                {"keyword_min_match_fraction": 1.5}, {"keyword_min_match_terms": 0},
                {"keyword_max_term_chars": 257},
                {"keyword_min_term_chars": 10, "keyword_max_term_chars": 5}):
        with pytest.raises(ValidationError):
            Settings(database_url=URL, search=bad)


def test_recovery_journal_is_required_absolute_and_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    s = Settings(database_url=URL)
    # No journal configured: the recovery gate stays closed (app.recovery), nothing is served.
    assert s.recovery.journal_path is None
    assert (s.recovery.gate_lock_timeout_ms, s.recovery.cleanup_passes) == (2000, 5)
    monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", str(tmp_path / "revocations.jsonl"))
    monkeypatch.setenv("RAS_RECOVERY__GATE_LOCK_TIMEOUT_MS", "50")
    monkeypatch.setenv("RAS_RECOVERY__CLEANUP_PASSES", "100")
    s = Settings(database_url=URL)
    assert s.recovery.journal_path == tmp_path / "revocations.jsonl"
    assert (s.recovery.gate_lock_timeout_ms, s.recovery.cleanup_passes) == (50, 100)
    assert RecoveryPolicy(journal_path="").journal_path is None
    for bad in ({"journal_path": "relative/revocations.jsonl"}, {"gate_lock_timeout_ms": 49},
                {"gate_lock_timeout_ms": 30_001}, {"cleanup_passes": 0}, {"cleanup_passes": 101}):
        with pytest.raises(ValidationError):
            RecoveryPolicy(**bad)
