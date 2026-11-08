"""Turning text into vectors for search.

Hosted embedding models treat a question and a stored note differently: each is encoded
with a hint saying which it is, and a question embedded as a document (or the reverse)
matches worse. That is why ``kind`` is required, with no default.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

EmbeddingKind = Literal["document", "query"]


@runtime_checkable
class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str], *, kind: EmbeddingKind) -> list[list[float]]: ...


__all__ = ["EmbeddingKind", "EmbeddingProvider"]
