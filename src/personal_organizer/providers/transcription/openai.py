"""OpenAI speech to text as the :class:`~personal_organizer.interfaces.voice.Transcriber`.

The model is ``MODELS__TRANSCRIPTION_MODEL`` (``gpt-4o-mini-transcribe``). No ``language`` is
sent: the model hears Hebrew or English itself, and a mixed note stays mixed. A short context
``prompt`` steers its word choice, since a 3-second note gives it little else (a Hebrew "voice
messages" once came back as "both kidneys"). The SDK's own
retries are off (``INBOUND_RETRY`` is the one layer), and its errors are translated without
chaining: their messages can carry the response body.
"""

from __future__ import annotations

from typing import Final

import httpx
import openai

from personal_organizer.core.errors import (
    ConfigError,
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
)
from personal_organizer.interfaces.voice import Audio
from personal_organizer.settings import Settings

#: What notes are about, in both languages, so a short note is heard as an organizer request.
_CONTEXT: Final = (
    "A WhatsApp voice note to a personal assistant, in Hebrew or English, about the calendar, "
    "meetings, reminders or things to remember. "
    "הודעה קולית לעוזר אישי: יומן, פגישה, תזכורת, הודעות קוליות, מחר, היום."
)

#: Statuses worth another attempt later. Any 5xx is too.
_TRANSIENT_STATUSES: Final = frozenset({408, 429})


def _translate(
    exc: openai.OpenAIError,
) -> TranscriptionRefusedError | TranscriptionUnavailableError:
    if isinstance(exc, openai.APIConnectionError):  # APITimeoutError is one too
        return TranscriptionUnavailableError("transcription provider unreachable")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return TranscriptionUnavailableError(f"transcription provider answered {status}")
        return TranscriptionRefusedError(f"transcription provider refused ({status})")
    return TranscriptionRefusedError("transcription response unusable")


class OpenAITranscriber:
    def __init__(self, client: openai.AsyncOpenAI, *, model: str) -> None:
        self._client = client
        self._model = model

    @classmethod
    def from_settings(
        cls, settings: Settings, *, http_client: httpx.AsyncClient | None = None
    ) -> OpenAITranscriber:
        """``http_client`` is for tests: the SDK sends every request through it."""
        if settings.voice.api_key is None:
            msg = "OpenAITranscriber needs VOICE__API_KEY"
            raise ConfigError(msg)
        client = openai.AsyncOpenAI(
            api_key=settings.voice.api_key.get_secret_value(),
            timeout=settings.voice.request_timeout_s,
            max_retries=0,
            http_client=http_client,
        )
        return cls(client, model=settings.models.transcription_model)

    async def transcribe(self, audio: Audio) -> str:
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model, file=(audio.filename, audio.content), prompt=_CONTEXT
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from None
        if not isinstance(result.text, str):
            raise TranscriptionRefusedError("transcription response unusable")
        return result.text.strip()


__all__ = ["OpenAITranscriber"]
