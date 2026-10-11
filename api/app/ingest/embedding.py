"""OpenRouter embedding transport. Never log request text, response, or key."""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.provider_identity import VERIFIED_EMBEDDING_IDENTITIES, audit_identity, verified_identity


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model_id: str
    input_tokens: int | None
    response_model_id: str | None = None
    response_provider: str | None = None


class EmbeddingError(Exception):
    def __init__(self, code: str, definitely_rejected: bool = False,
                 response_model_id: str | None = None, response_provider: str | None = None):
        self.code = code
        self.definitely_rejected = definitely_rejected
        self.response_model_id = response_model_id
        self.response_provider = response_provider
        super().__init__(code)


def embed(text: str, settings: Settings, *, input_type: str = "search_document") -> EmbeddingResult:
    policy = settings.embedding
    if policy.api_key is None:
        raise EmbeddingError("embedding_key_missing", definitely_rejected=True)
    try:
        with httpx.Client(timeout=policy.request_timeout_seconds) as client:
            response = client.post(policy.endpoint,
                headers={"Authorization": f"Bearer {policy.api_key.get_secret_value()}",
                         "Content-Type": "application/json"},
                json={"model": policy.model_id, "input": text,
                      "input_type": input_type, "encoding_format": "float"})
    except httpx.RequestError:
        raise EmbeddingError("provider_outcome_uncertain") from None
    if response.status_code == 429:
        raise EmbeddingError("provider_rate_limited", definitely_rejected=True)
    if response.status_code >= 500:
        raise EmbeddingError("provider_outcome_uncertain")
    if response.status_code != 200:
        raise EmbeddingError("provider_rejected", definitely_rejected=True)
    response_model = response_provider = None
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        response_model = audit_identity(payload.get("model"))
        response_provider = audit_identity(payload.get("provider"))
        if not verified_identity(policy.model_id, response_model, response_provider,
                                 VERIFIED_EMBEDDING_IDENTITIES):
            raise EmbeddingError("provider_response_invalid", response_model_id=response_model,
                                 response_provider=response_provider)
        entries = payload["data"]
        vector = entries[0]["embedding"]
        tokens = payload.get("usage", {}).get("prompt_tokens")
        if (len(entries) != 1 or not isinstance(vector, list) or not vector
                or not all(type(v) in (float, int) and math.isfinite(v) for v in vector)
                or (tokens is not None and (type(tokens) is not int or tokens < 0))):
            raise ValueError()
        return EmbeddingResult([float(v) for v in vector], policy.model_id, tokens,
                               response_model, response_provider)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        raise EmbeddingError("provider_response_invalid",
                             response_model_id=response_model,
                             response_provider=response_provider) from None
