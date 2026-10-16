"""The OpenAI transcriber over ``httpx.MockTransport``: the request and the error mapping."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from personal_organizer.core.errors import (
    ConfigError,
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
)
from personal_organizer.interfaces.voice import Audio, Transcriber
from personal_organizer.providers.transcription.openai import OpenAITranscriber

AUDIO = Audio(b"OggS\x00fake", "voice.ogg")


def _transcriber(settings_factory: Any, handler: Any) -> OpenAITranscriber:
    settings = settings_factory(
        VOICE__ENABLED="true",
        VOICE__API_KEY="sk-proj-test",
        MODELS__TRANSCRIPTION_MODEL="gpt-4o-mini-transcribe",
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAITranscriber.from_settings(settings, http_client=http)


async def test_it_sends_the_audio_to_the_model(settings_factory: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "  תזכיר לי מחר בשש  "})

    transcriber = _transcriber(settings_factory, handler)
    assert isinstance(transcriber, Transcriber)
    assert await transcriber.transcribe(AUDIO) == "תזכיר לי מחר בשש"
    [request] = seen
    assert request.url.path.endswith("/audio/transcriptions")
    body = request.read()
    assert b"gpt-4o-mini-transcribe" in body
    assert b'filename="voice.ogg"' in body
    assert b"language" not in body


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_statuses(settings_factory: Any, status: int) -> None:
    transcriber = _transcriber(
        settings_factory, lambda r: httpx.Response(status, json={"error": {"message": "x"}})
    )
    with pytest.raises(TranscriptionUnavailableError):
        await transcriber.transcribe(AUDIO)


@pytest.mark.parametrize("status", [400, 401, 413])
async def test_refusals(settings_factory: Any, status: int) -> None:
    transcriber = _transcriber(
        settings_factory, lambda r: httpx.Response(status, json={"error": {"message": "x"}})
    )
    with pytest.raises(TranscriptionRefusedError):
        await transcriber.transcribe(AUDIO)


async def test_unreachable_is_unavailable_and_tried_once(settings_factory: Any) -> None:
    tries: list[httpx.Request] = []

    def boom(request: httpx.Request) -> httpx.Response:
        tries.append(request)
        raise httpx.ConnectError("down", request=request)

    transcriber = _transcriber(settings_factory, boom)
    with pytest.raises(TranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(AUDIO)
    assert len(tries) == 1  # no SDK retries: INBOUND_RETRY is the one layer
    assert excinfo.value.__cause__ is None


def test_no_key_is_a_config_error(settings_factory: Any) -> None:
    with pytest.raises(ConfigError):
        OpenAITranscriber.from_settings(settings_factory())
