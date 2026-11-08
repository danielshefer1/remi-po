"""The Voyage embedder over ``httpx.MockTransport``: request shape, error mapping, privacy."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from personal_organizer.core.errors import (
    ConfigError,
    EmbeddingRejectedError,
    EmbeddingUnavailableError,
)
from personal_organizer.interfaces.embedding import EmbeddingProvider
from personal_organizer.providers.embedding.voyage import VoyageEmbedder

TEXT = "תזכיר לי מחר בשש"
VECTOR = [0.1] * 1024
PATH = "/v1/embeddings"
DOCUMENT_WIRE = "document"
QUERY_WIRE = "query"
Handler = Callable[[httpx.Request], httpx.Response]


def expected_body(texts: list[str], wire: str) -> dict[str, Any]:
    return {
        "input": texts,
        "model": "voyage-4",
        "input_type": wire,
        "output_dimension": 1024,
    }


def _embedder(settings_factory: Any, handler: Handler, **env: str) -> VoyageEmbedder:
    settings = settings_factory(
        EMBEDDING__API_KEY="embed-test-key", MODELS__EMBEDDING_MODEL="voyage-4", **env
    )
    return VoyageEmbedder.from_settings(settings, transport=httpx.MockTransport(handler))


def _ok(vectors: list[list[float]], seen: list[httpx.Request]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 3}})

    return handler


@pytest.mark.parametrize(("kind", "wire"), [("document", DOCUMENT_WIRE), ("query", QUERY_WIRE)])
async def test_it_sends_the_model_kind_and_dimension(
    settings_factory: Any, kind: Any, wire: str
) -> None:
    seen: list[httpx.Request] = []
    embedder = _embedder(settings_factory, _ok([VECTOR], seen))
    assert isinstance(embedder, EmbeddingProvider)
    assert await embedder.embed([TEXT], kind=kind) == [VECTOR]
    assert seen[0].url.path == PATH
    assert seen[0].headers["authorization"] == "Bearer embed-test-key"
    assert json.loads(seen[0].read()) == expected_body([TEXT], wire)


async def test_an_empty_list_makes_no_call(settings_factory: Any) -> None:
    seen: list[httpx.Request] = []
    assert await _embedder(settings_factory, _ok([], seen)).embed([], kind="query") == []
    assert seen == []


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_statuses(settings_factory: Any, status: int) -> None:
    embedder = _embedder(settings_factory, lambda r: httpx.Response(status, json={}))
    with pytest.raises(EmbeddingUnavailableError):
        await embedder.embed([TEXT], kind="query")


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
async def test_refusals(settings_factory: Any, status: int) -> None:
    embedder = _embedder(settings_factory, lambda r: httpx.Response(status, json={}))
    with pytest.raises(EmbeddingRejectedError):
        await embedder.embed([TEXT], kind="query")


async def test_unreachable_is_unavailable_and_tried_once(settings_factory: Any) -> None:
    tries: list[httpx.Request] = []

    def boom(request: httpx.Request) -> httpx.Response:
        tries.append(request)
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(EmbeddingUnavailableError) as excinfo:
        await _embedder(settings_factory, boom).embed([TEXT], kind="query")
    assert len(tries) == 1  # the job layer is the only retry
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize("vectors", [[], [[0.1] * 3], [VECTOR, VECTOR]])
async def test_the_wrong_count_or_length_is_refused(
    settings_factory: Any, vectors: list[list[float]]
) -> None:
    with pytest.raises(EmbeddingRejectedError):
        await _embedder(settings_factory, _ok(vectors, [])).embed([TEXT], kind="query")


async def test_an_unusable_body_is_refused(settings_factory: Any) -> None:
    for reply in (httpx.Response(200, text="not json"), httpx.Response(200, json={})):

        def handler(request: httpx.Request, reply: httpx.Response = reply) -> httpx.Response:
            return reply

        with pytest.raises(EmbeddingRejectedError):
            await _embedder(settings_factory, handler).embed([TEXT], kind="query")


async def test_the_text_stays_out_of_errors_and_logs(settings_factory: Any) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": TEXT, "message": TEXT})

    with capture_logs() as logs, pytest.raises(EmbeddingRejectedError) as excinfo:
        await _embedder(settings_factory, echo).embed([TEXT], kind="document")
    assert TEXT not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert TEXT not in repr(logs)


async def test_a_success_logs_counts_only(settings_factory: Any) -> None:
    with capture_logs() as logs:
        await _embedder(settings_factory, _ok([VECTOR], [])).embed([TEXT], kind="document")
    assert logs[0]["event"] == "embedding.completed"
    assert (logs[0]["texts"], logs[0]["tokens"]) == (1, 3)
    assert TEXT not in repr(logs)
    assert "embed-test-key" not in repr(logs)


def test_no_key_is_a_config_error(settings_factory: Any) -> None:
    with pytest.raises(ConfigError):
        VoyageEmbedder.from_settings(settings_factory())


async def test_vectors_line_up_with_their_texts_when_out_of_order(settings_factory: Any) -> None:
    first, second = [0.1] * 1024, [0.2] * 1024
    body = {
        "data": [{"index": 1, "embedding": second}, {"index": 0, "embedding": first}],
        "usage": {"total_tokens": 3},
    }
    embedder = _embedder(settings_factory, lambda r: httpx.Response(200, json=body))
    assert await embedder.embed(["a", "b"], kind="document") == [first, second]
