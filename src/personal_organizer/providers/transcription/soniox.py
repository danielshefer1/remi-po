"""Soniox speech to text as the :class:`~personal_organizer.interfaces.voice.Transcriber`.

The model is ``MODELS__TRANSCRIPTION_MODEL`` (``stt-async-v5``). Soniox's file API is
asynchronous: upload the audio, start a transcription, poll it, read the text, then delete
both so no voice note stays on their side. Hebrew and English are sent as hints, not as a
forced language: an English note in a Hebrew chat stays English, and a mixed note stays
mixed. There are no retries here (``INBOUND_RETRY`` is the one layer), and errors are
translated without chaining: their messages can carry the response body.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Final

import httpx
import structlog

from personal_organizer.core.errors import (
    ConfigError,
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
)
from personal_organizer.interfaces.voice import Audio
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

BASE_URL: Final = "https://api.soniox.com"
LANGUAGE_HINTS: Final = ("he", "en")
#: Statuses worth another attempt later. Any 5xx is too.
_TRANSIENT_STATUSES: Final = frozenset({408, 429})
_FAILED: Final = frozenset({"error", "failed"})


def _translate(exc: httpx.HTTPError) -> TranscriptionRefusedError | TranscriptionUnavailableError:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return TranscriptionUnavailableError(f"transcription provider answered {status}")
        return TranscriptionRefusedError(f"transcription provider refused ({status})")
    return TranscriptionUnavailableError("transcription provider unreachable")


class SonioxTranscriber:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        deadline_s: float,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._client = client
        self._model = model
        self._deadline_s = deadline_s
        self._poll_interval_s = poll_interval_s

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 0.5,
    ) -> SonioxTranscriber:
        """``transport`` and ``poll_interval_s`` are for tests."""
        if settings.voice.api_key is None:
            msg = "SonioxTranscriber needs VOICE__API_KEY"
            raise ConfigError(msg)
        timeout = settings.voice.request_timeout_s
        client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {settings.voice.api_key.get_secret_value()}"},
            timeout=timeout,
            transport=transport,
        )
        return cls(
            client,
            model=settings.models.transcription_model,
            deadline_s=timeout,
            poll_interval_s=poll_interval_s,
        )

    async def transcribe(self, audio: Audio) -> str:
        try:
            return await self._transcribe(audio)
        except httpx.HTTPError as exc:
            raise _translate(exc) from None
        except KeyError, TypeError, ValueError:  # ValueError covers a body that is not JSON
            raise TranscriptionRefusedError("transcription response unusable") from None

    async def _transcribe(self, audio: Audio) -> str:
        uploaded = await self._call(
            "POST", "/v1/files", files={"file": (audio.filename, audio.content)}
        )
        file_id = uploaded["id"]
        try:
            created = await self._call(
                "POST",
                "/v1/transcriptions",
                json={
                    "model": self._model,
                    "file_id": file_id,
                    "language_hints": list(LANGUAGE_HINTS),
                },
            )
            transcription_id = created["id"]
            try:
                await self._wait(transcription_id)
                result = await self._call(
                    "GET", f"/v1/transcriptions/{transcription_id}/transcript"
                )
            finally:
                await self._forget(f"/v1/transcriptions/{transcription_id}")
        finally:
            await self._forget(f"/v1/files/{file_id}")
        text = result["text"]
        if not isinstance(text, str):
            raise TranscriptionRefusedError("transcription response unusable")
        return text.strip()

    async def _wait(self, transcription_id: str) -> None:
        give_up = time.monotonic() + self._deadline_s
        while True:
            state = await self._call("GET", f"/v1/transcriptions/{transcription_id}")
            if state["status"] == "completed":
                return
            if state["status"] in _FAILED:
                raise TranscriptionRefusedError("transcription failed at the provider")
            if time.monotonic() >= give_up:
                raise TranscriptionUnavailableError("transcription took too long")
            await asyncio.sleep(self._poll_interval_s)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _forget(self, path: str) -> None:
        """Best effort: a delete that fails must not lose a transcript already heard."""
        with contextlib.suppress(httpx.HTTPError):
            response = await self._client.delete(path)
            if response.is_success or response.status_code == 404:
                return
        log.warning("voice.cleanup_failed", table=path.split("/")[2], provider="soniox")


__all__ = ["SonioxTranscriber"]
