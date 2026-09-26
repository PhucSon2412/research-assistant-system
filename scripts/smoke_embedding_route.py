"""One synthetic OpenRouter request through the production embedding adapter.

Set OPENROUTER_API_KEY securely. This probe never enables private-data pricing.
"""
from __future__ import annotations

import os
from decimal import Decimal

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.ingest.embedding import EmbeddingError, embed


def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY absent; live route unverified")
    model = os.environ.get("RAS_EMBEDDING__MODEL_ID", "openai/text-embedding-3-small")
    ceiling = Decimal(os.environ.get("RAS_EMBEDDING__MAX_USD_PER_MILLION_TOKENS", "0.10"))
    endpoint = os.environ.get("RAS_EMBEDDING__ENDPOINT", "https://openrouter.ai/api/v1/embeddings")
    if endpoint != "https://openrouter.ai/api/v1/embeddings":
        raise SystemExit("Probe requires the reviewed OpenRouter embeddings route")
    try:
        with httpx.Client(timeout=30) as client:
            catalog = client.get(endpoint + "/models", headers={"Authorization": f"Bearer {key}"})
            catalog.raise_for_status()
            entry = next(row for row in catalog.json()["data"] if row["id"] == model)
            quote = Decimal(entry["pricing"]["prompt"]) * Decimal(1_000_000)
            if quote <= 0 or quote > ceiling:
                raise ValueError("quoted price exceeds configured reservation ceiling")
        settings = Settings(_env_prefix="RAS_SMOKE_UNUSED_",
            database_url="postgresql+psycopg://unused@localhost/unused",
            embedding={"api_key": SecretStr(key), "model_id": model, "endpoint": endpoint,
                       "pricing_verified": False})
        result = embed("Synthetic test sentence.", settings)
        if result.input_tokens is None or result.input_tokens <= 0:
            raise ValueError("unexpected usage")
    except (httpx.HTTPError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise SystemExit(f"Live embedding probe failed: {type(exc).__name__}") from None
    except EmbeddingError as exc:
        raise SystemExit(f"Live embedding adapter rejected response: {exc.code}") from None

    print(f"requested_model={result.model_id} response_model={result.response_model_id} "
          f"response_provider={result.response_provider} dimensions={len(result.vector)} "
          f"prompt_tokens={result.input_tokens} quoted_usd_per_million={quote} "
          f"estimated_request_usd={quote * result.input_tokens / 1_000_000}")
    print("Catalog quote is an estimate. Reconcile actual key charge later; an unchanged GET /api/v1/key immediately after the call does not prove zero charge. Pricing flags remain off.")


if __name__ == "__main__":
    main()
