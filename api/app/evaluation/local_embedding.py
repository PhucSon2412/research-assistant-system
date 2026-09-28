"""Deterministic local stand-in for the embedding provider.

A hashed bag of words: it lets the evaluation exercise the real pgvector path
(index gate, model/dimension checks, exact cosine scan, fusion) without sending
text anywhere. It is a lexical proxy, so its rankings say nothing about the
quality of ``openai/text-embedding-3-small``.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from app.config import Settings
from app.ingest.embedding import EmbeddingResult

MODEL_ID = "local/hash-bow-512-v1"
DIMENSIONS = 512
TOKEN = re.compile(r"[0-9a-z]+(?:[.'][0-9a-z]+)*")
STOPWORDS = frozenset("""a an and are as at be by did do does for from how in is it its of on or that the
their this to was were what when which who why with""".split())


def vector(text: str) -> list[float]:
    counts = Counter(token for token in TOKEN.findall(text.casefold()) if token not in STOPWORDS)
    values = [0.0] * DIMENSIONS
    for token, count in counts.items():
        digest = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        sign = 1.0 if digest >> 63 else -1.0
        values[digest % DIMENSIONS] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        # pgvector cosine distance is undefined for a zero vector.
        values[0], norm = 1.0, 1.0
    return [v / norm for v in values]


def embed(text: str, settings: Settings, *, input_type: str = "search_document") -> EmbeddingResult:
    """Same call shape as ``app.ingest.embedding.embed``; never opens a connection.

    Usage is 0 tokens because no provider processed the text.
    """
    return EmbeddingResult(vector(text), settings.embedding.model_id, 0, MODEL_ID, "local")
