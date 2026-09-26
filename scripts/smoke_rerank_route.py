"""One synthetic OpenRouter rerank request; compare the key's actual charge manually."""
import os
from decimal import Decimal

import httpx


def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY absent; live rerank route unverified")
    model = os.environ.get("RAS_RERANK__MODEL_ID", "qwen/qwen3-reranker-8b")
    endpoint = os.environ.get("RAS_RERANK__ENDPOINT", "https://openrouter.ai/api/v1/rerank")
    if endpoint != "https://openrouter.ai/api/v1/rerank":
        raise SystemExit("Probe requires the reviewed OpenRouter rerank route")
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "query": "synthetic apple query",
                      "documents": ["synthetic apple document", "synthetic orange document"], "top_n": 2})
            response.raise_for_status()
            data = response.json()
            if data["model"] != model or {row["index"] for row in data["results"]} != {0, 1}:
                raise ValueError("unexpected model or mapping")
            tokens = data.get("usage", {}).get("total_tokens")
            if tokens is not None and (not isinstance(tokens, int) or tokens < 0):
                raise ValueError("unexpected usage")
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Live rerank probe failed: {type(exc).__name__}") from None
    quote = Decimal(os.environ.get("RAS_RERANK__MAX_USD_PER_MILLION_TOKENS", "0.20"))
    print(f"model={model} total_tokens={tokens} configured_usd_per_million={quote} "
          f"estimated_request_usd={quote * tokens / 1_000_000 if tokens is not None else 'unknown'}")
    print("Check the actual key charge, route and provider before enabling private rerank.")


if __name__ == "__main__":
    main()
