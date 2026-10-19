"""OpenRouter embedding request/response contract with a local HTTP transport."""

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.ingest.embedding import EmbeddingError, embed


def test_embedding_transport_sends_only_one_extracted_text(monkeypatch):
    settings = Settings(db=None, database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-key")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert str(request.url) == "https://openrouter.ai/api/v1/embeddings"
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert payload == {"model": "openai/text-embedding-3-small", "input": "synthetic extracted body",
                           "input_type": "search_document", "encoding_format": "float",
                           "provider": {"only": ["azure"], "allow_fallbacks": False, "data_collection": "deny",
                                        "zdr": True, "max_price": {"prompt": "0.100000"}}}
        seen.append(payload["input"])
        return httpx.Response(200, json={"model": "text-embedding-3-small", "provider": "Azure",
            "data": [{"index": 0, "embedding": [0.1] * 1536}],
            "usage": {"prompt_tokens": 4, "total_tokens": 4}})

    original_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.ingest.embedding.httpx.Client",
                        lambda timeout: original_client(transport=transport, timeout=timeout))
    result = embed("synthetic extracted body", settings)
    assert len(result.vector) == 1536
    assert result.model_id == settings.embedding.model_id
    assert result.response_model_id == "text-embedding-3-small"
    assert result.response_provider == "Azure"
    assert result.input_tokens == 4
    assert seen == ["synthetic extracted body"]


@pytest.mark.parametrize("response_model,response_provider", [
    ("different/model", "Azure"),
    ("text-embedding-3-small", "Other"),
    ("text-embedding-3-small", None),
    (None, "Azure"),
])
def test_embedding_transport_rejects_unknown_identity(monkeypatch, response_model, response_provider):
    settings = Settings(db=None, database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-key")
    original_client = httpx.Client
    payload = {"data": [{"index": 0, "embedding": [0.1]}]}
    if response_model is not None:
        payload["model"] = response_model
    if response_provider is not None:
        payload["provider"] = response_provider
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr("app.ingest.embedding.httpx.Client",
                        lambda timeout: original_client(transport=transport, timeout=timeout))
    with pytest.raises(EmbeddingError) as error:
        embed("synthetic", settings)
    assert error.value.code == "provider_response_invalid"
    assert error.value.response_model_id == response_model
    assert error.value.response_provider == response_provider
