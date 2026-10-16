"""The Soniox transcriber over ``httpx.MockTransport``: the flow, cleanup and error mapping."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from personal_organizer.core.errors import (
    ConfigError,
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
)
from personal_organizer.interfaces.voice import Audio, Transcriber
from personal_organizer.providers.transcription.soniox import SonioxTranscriber

AUDIO = Audio(b"OggS\x00fake", "voice.ogg")
Handler = Callable[[httpx.Request], httpx.Response]


def _transcriber(settings_factory: Any, handler: Handler, **env: str) -> SonioxTranscriber:
    settings = settings_factory(
        VOICE__ENABLED="true",
        VOICE__API_KEY="soniox-test-key",
        MODELS__TRANSCRIPTION_MODEL="stt-async-v5",
        **env,
    )
    return SonioxTranscriber.from_settings(
        settings, transport=httpx.MockTransport(handler), poll_interval_s=0
    )


def _soniox(
    seen: list[httpx.Request],
    *,
    statuses: list[str] | None = None,
    transcript: dict[str, Any] | None = None,
    fail: tuple[str, str, int] | None = None,
) -> Handler:
    """A fake Soniox: ``statuses`` are what successive polls see; ``fail`` is (method, path
    prefix, status) for one endpoint that errors."""
    polls = iter(statuses or ["completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        method, path = request.method, request.url.path
        if fail and method == fail[0] and path.startswith(fail[1]):
            return httpx.Response(fail[2], json={"message": "x"})
        if method == "DELETE":
            return httpx.Response(204)
        if (method, path) == ("POST", "/v1/files"):
            return httpx.Response(201, json={"id": "file-1"})
        if (method, path) == ("POST", "/v1/transcriptions"):
            return httpx.Response(201, json={"id": "tr-1", "status": "queued"})
        if (method, path) == ("GET", "/v1/transcriptions/tr-1"):
            return httpx.Response(200, json={"id": "tr-1", "status": next(polls)})
        if (method, path) == ("GET", "/v1/transcriptions/tr-1/transcript"):
            body = transcript if transcript is not None else {"text": "  תזכיר לי מחר בשש  "}
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return handler


def _deleted(seen: list[httpx.Request]) -> set[str]:
    return {r.url.path for r in seen if r.method == "DELETE"}


async def test_it_uploads_transcribes_and_cleans_up(settings_factory: Any) -> None:
    seen: list[httpx.Request] = []
    transcriber = _transcriber(
        settings_factory, _soniox(seen, statuses=["queued", "transcribing", "completed"])
    )
    assert isinstance(transcriber, Transcriber)
    assert await transcriber.transcribe(AUDIO) == "תזכיר לי מחר בשש"

    upload, create = seen[0], seen[1]
    assert upload.headers["authorization"] == "Bearer soniox-test-key"
    assert b'filename="voice.ogg"' in upload.read()
    config = json.loads(create.read())
    assert config == {
        "model": "stt-async-v5",
        "file_id": "file-1",
        "language_hints": ["he", "en"],  # hints, never a forced language
    }
    assert _deleted(seen) == {"/v1/transcriptions/tr-1", "/v1/files/file-1"}


async def test_a_failed_transcription_is_refused_and_cleaned_up(settings_factory: Any) -> None:
    seen: list[httpx.Request] = []
    transcriber = _transcriber(settings_factory, _soniox(seen, statuses=["error"]))
    with pytest.raises(TranscriptionRefusedError):
        await transcriber.transcribe(AUDIO)
    assert _deleted(seen) == {"/v1/transcriptions/tr-1", "/v1/files/file-1"}


async def test_too_slow_is_unavailable(settings_factory: Any) -> None:
    seen: list[httpx.Request] = []
    transcriber = _transcriber(
        settings_factory,
        _soniox(seen, statuses=["queued"] * 1000),
        VOICE__REQUEST_TIMEOUT_S="0",
    )
    with pytest.raises(TranscriptionUnavailableError):
        await transcriber.transcribe(AUDIO)
    assert _deleted(seen) == {"/v1/transcriptions/tr-1", "/v1/files/file-1"}


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_statuses(settings_factory: Any, status: int) -> None:
    transcriber = _transcriber(settings_factory, _soniox([], fail=("POST", "/v1/files", status)))
    with pytest.raises(TranscriptionUnavailableError):
        await transcriber.transcribe(AUDIO)


@pytest.mark.parametrize("status", [400, 401, 402])
async def test_refusals(settings_factory: Any, status: int) -> None:
    seen: list[httpx.Request] = []
    transcriber = _transcriber(
        settings_factory, _soniox(seen, fail=("POST", "/v1/transcriptions", status))
    )
    with pytest.raises(TranscriptionRefusedError):
        await transcriber.transcribe(AUDIO)
    assert _deleted(seen) == {"/v1/files/file-1"}  # the upload never lingers


async def test_unreachable_is_unavailable_and_tried_once(settings_factory: Any) -> None:
    tries: list[httpx.Request] = []

    def boom(request: httpx.Request) -> httpx.Response:
        tries.append(request)
        raise httpx.ConnectError("down", request=request)

    transcriber = _transcriber(settings_factory, boom)
    with pytest.raises(TranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(AUDIO)
    assert len(tries) == 1  # no retries here: INBOUND_RETRY is the one layer
    assert excinfo.value.__cause__ is None


async def test_a_failed_cleanup_still_returns_the_text(settings_factory: Any) -> None:
    transcriber = _transcriber(settings_factory, _soniox([], fail=("DELETE", "/v1/", 500)))
    assert await transcriber.transcribe(AUDIO) == "תזכיר לי מחר בשש"


def test_no_key_is_a_config_error(settings_factory: Any) -> None:
    with pytest.raises(ConfigError):
        SonioxTranscriber.from_settings(settings_factory())


async def test_a_reply_without_text_is_refused(settings_factory: Any) -> None:
    transcriber = _transcriber(settings_factory, _soniox([], transcript={}))
    with pytest.raises(TranscriptionRefusedError):
        await transcriber.transcribe(AUDIO)
