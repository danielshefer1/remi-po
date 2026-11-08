"""Voyage as the :class:`~personal_organizer.interfaces.embedding.EmbeddingProvider`.

The model is ``MODELS__EMBEDDING_MODEL`` (``voyage-4``) at ``MODELS__EMBEDDING_DIMENSIONS``.
One POST per call. There are no retries here (the job layer is the one retry), and errors
are translated without chaining, and nothing is logged but counts: the vendor's error body
can echo the text, which is the person's words.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import httpx
import structlog

from personal_organizer.core.errors import (
    ConfigError,
    EmbeddingRejectedError,
    EmbeddingUnavailableError,
)
from personal_organizer.interfaces.embedding import EmbeddingKind
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

BASE_URL: Final = "https://api.voyageai.com"
#: Statuses worth another attempt later. Any 5xx is too.
_TRANSIENT_STATUSES: Final = frozenset({408, 429})


def _translate(exc: httpx.HTTPError) -> EmbeddingRejectedError | EmbeddingUnavailableError:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return EmbeddingUnavailableError(f"embedding provider answered {status}")
        return EmbeddingRejectedError(f"embedding provider refused ({status})")
    return EmbeddingUnavailableError("embedding provider unreachable")


class VoyageEmbedder:
    def __init__(self, client: httpx.AsyncClient, *, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> VoyageEmbedder:
        """``transport`` is for tests."""
        if settings.embedding.api_key is None:
            msg = "VoyageEmbedder needs EMBEDDING__API_KEY"
            raise ConfigError(msg)
        client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {settings.embedding.api_key.get_secret_value()}"},
            timeout=settings.embedding.request_timeout_s,
            transport=transport,
        )
        return cls(
            client,
            model=settings.models.embedding_model,
            dimensions=settings.models.embedding_dimensions,
        )

    # shortcut: one request, so over 1000 texts is a 400; split when a backfill needs it.
    async def embed(self, texts: Sequence[str], *, kind: EmbeddingKind) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.post(
                "/v1/embeddings",
                json={
                    "input": list(texts),
                    "model": self._model,
                    "input_type": kind,
                    "output_dimension": self._dimensions,
                },
            )
            response.raise_for_status()
            body = response.json()
            rows = sorted(body["data"], key=lambda row: row["index"])
            vectors = [row["embedding"] for row in rows]
            tokens = int(body["usage"]["total_tokens"])
        except httpx.HTTPError as exc:
            raise _translate(exc) from None
        except KeyError, TypeError, ValueError:  # ValueError covers a body that is not JSON
            raise EmbeddingRejectedError("embedding response unusable") from None
        if len(vectors) != len(texts) or any(len(v) != self._dimensions for v in vectors):
            raise EmbeddingRejectedError("embedding response unusable")
        log.info("embedding.completed", model=self._model, texts=len(texts), tokens=tokens)
        return vectors


__all__ = ["VoyageEmbedder"]
