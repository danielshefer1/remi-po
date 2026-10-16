"""Fakes for voice notes: a channel that can download, and a transcriber."""

from __future__ import annotations

from personal_organizer.interfaces.voice import Audio
from tests.fixtures.channels import FakeOutbound


class ListeningOutbound(FakeOutbound):
    """A ``FakeOutbound`` that is a ``MediaSource``, like GOWA."""

    def __init__(self, *, download_error: Exception | None = None, name: str = "gowa") -> None:
        super().__init__(name=name)
        self.download_error = download_error
        self.downloads: list[tuple[str, str | None, str | None]] = []

    async def download(
        self, provider_message_id: str, *, phone: str | None, user_id: str | None
    ) -> Audio:
        self.downloads.append((provider_message_id, phone, user_id))
        if self.download_error is not None:
            raise self.download_error
        return Audio(b"OggS\x00fake", "voice.ogg")


class FakeTranscriber:
    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[Audio] = []

    async def transcribe(self, audio: Audio) -> str:
        self.calls.append(audio)
        if self.error is not None:
            raise self.error
        return self.text
