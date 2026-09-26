"""OpenRouter embedding request/response contract with a local HTTP transport."""

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.ingest.embedding import EmbeddingError, embed


def test_embedding_transport_sends_only_one_extracted_text(monkeypatch):
    settings = Settings(database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-key")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert str(request.url) == "https://openrouter.ai/api/v1/embeddings"
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert payload == {"model": "openai/text-embedding-3-small", "input": "synthetic extracted body",
                           "input_type": "search_document", "encoding_format": "float"}
        seen.append(payload["input"])
        return httpx.Response(200, json={"model": payload["model"], "data": [{"index": 0,
            "embedding": [0.1, 0.2, 0.3]}], "usage": {"prompt_tokens": 4, "total_tokens": 4}})

    original_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.ingest.embedding.httpx.Client",
                        lambda timeout: original_client(transport=transport, timeout=timeout))
    result = embed("synthetic extracted body", settings)
    assert len(result.vector) == 3
    assert result.model_id == settings.embedding.model_id
    assert result.input_tokens == 4
    assert seen == ["synthetic extracted body"]


def test_embedding_transport_rejects_wrong_model(monkeypatch):
    settings = Settings(database_url="postgresql+psycopg://test@localhost/test")
    settings.embedding.api_key = SecretStr("synthetic-key")
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "model": "different/model", "data": [{"index": 0, "embedding": [0.1]}]}))
    monkeypatch.setattr("app.ingest.embedding.httpx.Client",
                        lambda timeout: original_client(transport=transport, timeout=timeout))
    with pytest.raises(EmbeddingError) as error:
        embed("synthetic", settings)
    assert error.value.code == "provider_response_invalid"
