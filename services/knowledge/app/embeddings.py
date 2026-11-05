from __future__ import annotations

from hashlib import blake2b
import math
import os
import re

import httpx


class EmbeddingProvider:
    def __init__(self) -> None:
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
        self.base_url = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("EMBEDDING_API_KEY", "")
        self.model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

    @property
    def mode(self) -> str:
        return "openai-compatible" if self.base_url and self.api_key else "local-hash"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.mode == "openai-compatible":
            return self._remote_embed(texts)
        return [self._hash_embed(text) for text in texts]

    def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts, "dimensions": self.dimensions},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in data]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError(f"Embedding 维度必须为 {self.dimensions}")
        return vectors

    def _hash_embed(self, text: str) -> list[float]:
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        characters = [char for char in normalized if not char.isspace()]
        tokens = characters + ["".join(characters[index:index + 2]) for index in range(len(characters) - 1)]
        vector = [0.0] * self.dimensions
        for token in tokens or [normalized]:
            digest = blake2b(token.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            vector[index] += 1.0 if digest[8] % 2 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


embedding_provider = EmbeddingProvider()
