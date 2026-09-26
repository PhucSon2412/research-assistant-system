"""Synthetic smoke-script runs must exercise the real adapters without a live key."""
import httpx
import pytest

from scripts import smoke_embedding_route, smoke_rerank_route


def test_embedding_smoke_uses_adapter_and_keeps_pricing_off(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-secret")
    monkeypatch.setenv("RAS_DB__HOST", "ignored-by-smoke")
    original_client = httpx.Client
    actual_embed = smoke_embedding_route.embed
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "openai/text-embedding-3-small",
                "pricing": {"prompt": "0.0000001"}}]})
        seen.append(request.method)
        return httpx.Response(200, json={"model": "text-embedding-3-small", "provider": "Azure",
            "data": [{"embedding": [.1] * 1536}], "usage": {"prompt_tokens": 4}})

    monkeypatch.setattr(httpx, "Client", lambda timeout: original_client(
        transport=httpx.MockTransport(handler), timeout=timeout))
    def checked_embed(value, settings):
        assert settings.embedding.pricing_verified is False
        return actual_embed(value, settings)
    monkeypatch.setattr(smoke_embedding_route, "embed", checked_embed)
    smoke_embedding_route.main()
    output = capsys.readouterr().out
    assert seen == ["POST"]
    assert "response_model=text-embedding-3-small" in output
    assert "response_provider=Azure" in output and "dimensions=1536" in output
    assert "synthetic-secret" not in output


def test_rerank_smoke_uses_adapter_and_rejects_unknown_identity(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-secret")
    monkeypatch.setenv("RAS_DB__HOST", "ignored-by-smoke")
    original_client = httpx.Client
    actual_rerank = smoke_rerank_route.rerank
    response_model = ["accounts/fireworks/models/qwen3-reranker-8b"]

    def handler(request):
        return httpx.Response(200, json={"model": response_model[0], "provider": "Fireworks",
            "results": [{"index": 0, "relevance_score": .9},
                        {"index": 1, "relevance_score": .2}],
            "usage": {"total_tokens": 6}})

    monkeypatch.setattr(httpx, "Client", lambda timeout: original_client(
        transport=httpx.MockTransport(handler), timeout=timeout))
    def checked_rerank(query, docs, settings):
        assert settings.rerank.pricing_verified is False
        return actual_rerank(query, docs, settings)
    monkeypatch.setattr(smoke_rerank_route, "rerank", checked_rerank)
    smoke_rerank_route.main()
    output = capsys.readouterr().out
    assert "response_model=accounts/fireworks/models/qwen3-reranker-8b" in output
    assert "response_provider=Fireworks" in output and "synthetic-secret" not in output
    response_model[0] = "unverified/model"
    with pytest.raises(SystemExit, match="adapter rejected response: rerank_response_invalid"):
        smoke_rerank_route.main()
