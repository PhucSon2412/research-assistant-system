"""Exact response identities observed on reviewed OpenRouter routes."""

VERIFIED_EMBEDDING_IDENTITIES = frozenset({
    ("openai/text-embedding-3-small", "text-embedding-3-small", "Azure"),
})

VERIFIED_RERANK_IDENTITIES = frozenset({
    ("qwen/qwen3-reranker-8b", "accounts/fireworks/models/qwen3-reranker-8b", "Fireworks"),
})


def audit_identity(value: object) -> str | None:
    """Store only bounded, nonempty model/provider identifiers from a response."""
    return value if isinstance(value, str) and 0 < len(value) <= 255 else None


def verified_identity(requested: str, response_model: str | None,
                      response_provider: str | None, allowed: frozenset[tuple[str, str, str]]) -> bool:
    return (requested, response_model, response_provider) in allowed
