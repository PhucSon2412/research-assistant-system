"""Tiny synthetic live probe. Set OPENROUTER_API_KEY securely; never prints it.

Run with the Conda ras interpreter before setting RAS_EMBEDDING__PRICING_VERIFIED=true.
The model list quote is only a current estimate; inspect the key's actual usage
and charges after this request before enabling private uploads.
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx


def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY absent; live route unverified")
    model = os.environ.get("RAS_EMBEDDING__MODEL_ID", "openai/text-embedding-3-small")
    ceiling = Decimal(os.environ.get("RAS_EMBEDDING__MAX_USD_PER_MILLION_TOKENS", "0.10"))
    endpoint = os.environ.get("RAS_EMBEDDING__ENDPOINT", "https://openrouter.ai/api/v1/embeddings")
    if endpoint != "https://openrouter.ai/api/v1/embeddings":
        raise SystemExit("Probe requires the reviewed OpenRouter embeddings route")
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=30) as client:
            catalog = client.get(endpoint + "/models", headers=headers)
            catalog.raise_for_status()
            entry = next(row for row in catalog.json()["data"] if row["id"] == model)
            quote = Decimal(entry["pricing"]["prompt"]) * Decimal(1_000_000)
            if quote <= 0 or quote > ceiling:
                raise ValueError("quoted price exceeds configured reservation ceiling")
            response = client.post(endpoint, headers=headers,
                json={"model": model, "input": "Synthetic test sentence.",
                      "input_type": "search_document", "encoding_format": "float"})
            response.raise_for_status()
            payload = response.json()
            tokens = payload["usage"]["prompt_tokens"]
            vector = payload["data"][0]["embedding"]
            if payload["model"] != model or not isinstance(tokens, int) or tokens <= 0 or not vector:
                raise ValueError("unexpected model, dimensions or usage")
    except (httpx.HTTPError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise SystemExit(f"Live embedding probe failed: {type(exc).__name__}") from None
    print(f"model={model} dimensions={len(vector)} prompt_tokens={tokens} "
          f"quoted_usd_per_million={quote} estimated_request_usd={quote * tokens / 1_000_000}")
    print("Check this request's actual key charge/usage before enabling private data.")


if __name__ == "__main__":
    main()
