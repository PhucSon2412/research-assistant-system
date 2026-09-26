"""One synthetic OpenRouter request through the production rerank adapter."""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

from pydantic import SecretStr

from app.config import Settings
from app.search import RerankError, rerank, rerank_order


def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY absent; live rerank route unverified")
    model = os.environ.get("RAS_RERANK__MODEL_ID", "qwen/qwen3-reranker-8b")
    endpoint = os.environ.get("RAS_RERANK__ENDPOINT", "https://openrouter.ai/api/v1/rerank")
    if endpoint != "https://openrouter.ai/api/v1/rerank":
        raise SystemExit("Probe requires the reviewed OpenRouter rerank route")
    try:
        settings = Settings(_env_prefix="RAS_SMOKE_UNUSED_",
            database_url="postgresql+psycopg://unused@localhost/unused",
            embedding={"api_key": SecretStr(key)},
            rerank={"model_id": model, "endpoint": endpoint, "pricing_verified": False})
        result = rerank("synthetic apple query",
                        ["synthetic apple document", "synthetic orange document"], settings)
        rerank_order([uuid.uuid4(), uuid.uuid4()], result.results)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Live rerank probe failed: {type(exc).__name__}") from None
    except RerankError as exc:
        raise SystemExit(f"Live rerank adapter rejected response: {exc.code}") from None
    quote = Decimal(os.environ.get("RAS_RERANK__MAX_USD_PER_MILLION_TOKENS", "0.20"))
    print(f"requested_model={model} response_model={result.response_model_id} "
          f"response_provider={result.response_provider} total_tokens={result.tokens} "
          f"configured_usd_per_million={quote} "
          f"estimated_request_usd={quote * result.tokens / 1_000_000 if result.tokens is not None else 'unknown'}")
    print("Catalog/config quote is an estimate. Reconcile actual key charge later; an unchanged GET /api/v1/key immediately after the call does not prove zero charge. Pricing flags remain off.")


if __name__ == "__main__":
    main()
