"""Voice notes: where the audio comes from and what turns it into text (Iteration 06).

A channel that can hand over a message's audio is a :class:`MediaSource` (GOWA is; Meta is
not yet). ``messaging.voice.hear`` checks for it with ``isinstance``, so a channel without
``download`` simply gets the "couldn't hear that" line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

#: OpenAI takes up to 25 MB; a minute of voice note is roughly 100 KB.
MAX_AUDIO_BYTES: Final = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Audio:
    content: bytes
    #: With its extension: the transcription API reads the format from it.
    filename: str


@runtime_checkable
class MediaSource(Protocol):
    async def download(
        self, provider_message_id: str, *, phone: str | None, user_id: str | None
    ) -> Audio: ...


@runtime_checkable
class Transcriber(Protocol):
    async def transcribe(self, audio: Audio) -> str: ...


__all__ = ["MAX_AUDIO_BYTES", "Audio", "MediaSource", "Transcriber"]
