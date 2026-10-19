"""Exact response identities observed on reviewed OpenRouter routes."""

VERIFIED_EMBEDDING_IDENTITIES = frozenset({
    ("openai/text-embedding-3-small", "text-embedding-3-small", "Azure"),
})

VERIFIED_RERANK_IDENTITIES = frozenset({
    ("qwen/qwen3-reranker-8b", "accounts/fireworks/models/qwen3-reranker-8b", "Fireworks"),
})


# Requested generation model -> dated endpoint model that OpenRouter's selected endpoint
# reports for it (M7a: live Azure response and the model's endpoint catalog agree).
VERIFIED_GENERATION_ENDPOINT_MODELS = frozenset({
    ("openai/gpt-6-sol", "openai/gpt-6-sol-20260922"),
})


def generation_endpoint_model(requested: str, selected: object) -> bool:
    return selected == requested or (requested, selected) in VERIFIED_GENERATION_ENDPOINT_MODELS


# Response provider names of the reviewed routing slugs (M7a, from the provider catalog).
ROUTING_PROVIDER_NAMES = {"azure": "Azure", "fireworks": "Fireworks"}


def routed_provider(response_provider: str | None, only: tuple[str, ...]) -> bool:
    """The serving provider is one of the pinned slugs (a variant slug maps by its base)."""
    names = {ROUTING_PROVIDER_NAMES.get(slug.split("/", 1)[0]) for slug in only} - {None}
    return response_provider is not None and response_provider in names


def audit_identity(value: object) -> str | None:
    """Store only bounded, nonempty model/provider identifiers from a response."""
    return value if isinstance(value, str) and 0 < len(value) <= 255 else None


def verified_identity(requested: str, response_model: str | None,
                      response_provider: str | None, allowed: frozenset[tuple[str, str, str]]) -> bool:
    return (requested, response_model, response_provider) in allowed
