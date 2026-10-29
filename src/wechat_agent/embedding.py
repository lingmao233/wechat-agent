from __future__ import annotations

"""Small OpenAI-compatible embedding client used by scoped memory.

The configured Volcengine embedding endpoint follows the OpenAI response
shape.  This module intentionally contains no logging of request headers or
environment values: a failed call exposes only a typed error and the caller
can safely fall back to lexical retrieval.
"""

from dataclasses import dataclass
import math
from typing import Any

import httpx

from .config import Settings


class EmbeddingError(RuntimeError):
    """A safe, non-secret embedding failure."""


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    dimension: int


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = (settings.embed_base or "").strip()
        self.model = (settings.embed_model or "").strip()
        self.api_key = (settings.embed_key or "").strip()
        self.expected_dimension = int(settings.embed_dimension)

    @property
    def endpoint(self) -> str:
        """Return the configured endpoint without duplicating a path.

        Ark's multimodal endpoint is commonly configured as the complete
        ``.../embeddings/multimodal`` URL, while OpenAI-compatible providers
        usually expose a version root and expect ``/embeddings`` to be added.
        """

        value = self.base_url.rstrip("/")
        if not value:
            return value
        path = value.lower().split("?", 1)[0]
        if path.endswith("/embeddings") or "/embeddings/" in path:
            return value
        return value + "/embeddings"

    @property
    def multimodal_endpoint(self) -> bool:
        return "/embeddings/multimodal" in self.endpoint.lower()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.embedding_enabled
            and self.base_url
            and self.model
            and self.api_key
        )

    def embed(self, texts: list[str], *, is_query: bool = False) -> EmbeddingResult:
        values = [str(text or "").strip() for text in texts]
        if not values or any(not value for value in values):
            raise EmbeddingError("embedding_input_empty")
        if not self.configured:
            raise EmbeddingError("embedding_not_configured")
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(
                timeout=self.settings.embed_timeout_seconds,
                trust_env=False,
            ) as client:
                if self.multimodal_endpoint:
                    # Ark's multimodal API returns one embedding for the
                    # supplied content list.  Make one request per text so a
                    # list of memories remains a list of independent vectors.
                    rows: list[dict[str, Any]] = []
                    for value in values:
                        response = client.post(
                            self.endpoint,
                            headers=headers,
                            json={
                                "model": self.model,
                                "input": [{"type": "text", "text": value}],
                            },
                        )
                        response.raise_for_status()
                        data = response.json()
                        row = data.get("data") if isinstance(data, dict) else None
                        if not isinstance(row, dict):
                            raise EmbeddingError("embedding_response_shape_invalid")
                        rows.append(row)
                else:
                    payload: dict[str, Any] = {
                        "model": self.model,
                        "input": values,
                        "encoding_format": "float",
                    }
                    if self.expected_dimension:
                        payload["dimensions"] = self.expected_dimension
                    # Some providers accept this OpenAI-compatible hint; omit
                    # it for providers that do not document it.
                    if is_query:
                        payload["input_type"] = "query"
                    response = client.post(
                        self.endpoint,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    raw_rows = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(raw_rows, list) or len(raw_rows) != len(values):
                        raise EmbeddingError("embedding_response_shape_invalid")
                    rows = [row for row in raw_rows if isinstance(row, dict)]
                    if len(rows) != len(values):
                        raise EmbeddingError("embedding_response_shape_invalid")
        except EmbeddingError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # Preserve the exception class for diagnostics while never copying
            # a provider response body (which might echo request metadata).
            raise EmbeddingError(f"embedding_request_failed:{type(exc).__name__}") from exc

        vectors: list[list[float]] = []
        for row in rows:
            vector = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("embedding_vector_missing")
            try:
                converted = [float(value) for value in vector]
            except (TypeError, ValueError) as exc:
                raise EmbeddingError("embedding_vector_not_numeric") from exc
            if not all(math.isfinite(value) for value in converted):
                raise EmbeddingError("embedding_vector_not_finite")
            if self.expected_dimension and len(converted) != self.expected_dimension:
                raise EmbeddingError(
                    f"embedding_dimension_mismatch:{len(converted)}:{self.expected_dimension}"
                )
            if vectors and len(converted) != len(vectors[0]):
                raise EmbeddingError("embedding_dimension_inconsistent")
            vectors.append(converted)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dimension=len(vectors[0]),
        )

    def embed_one(self, text: str, *, is_query: bool = False) -> tuple[list[float], str]:
        result = self.embed([text], is_query=is_query)
        return result.vectors[0], result.model

