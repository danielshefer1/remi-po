"""``hear``: a voice note's words, or ``None`` with the reason logged, never its text."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from personal_organizer.core.errors import (
    MediaUnavailableError,
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
    TransientChannelError,
)
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.voice import hear
from tests.fixtures.channels import FakeOutbound
from tests.fixtures.voice import FakeTranscriber, ListeningOutbound

ROW = InboxRow(
    id=uuid4(),
    channel="gowa",
    provider_message_id="3EB0ABC",
    sender_key="tel:+972521234567",
    sender_user_id=None,
    sender_phone="+972521234567",
    message_type="audio",
    body=None,
    reply_id=None,
    sent_at=datetime(2026, 10, 16, tzinfo=UTC),
    processed_at=None,
    disposition=None,
)


async def test_the_transcript_becomes_the_body() -> None:
    channel = ListeningOutbound(name="gowa")
    heard = await hear(ROW, channel, FakeTranscriber(" remind me at six "), last_attempt=False)
    assert heard is not None
    assert (heard.body, heard.message_type, heard.id) == ("remind me at six", "audio", ROW.id)
    assert channel.downloads == [("3EB0ABC", "+972521234567", None)]


async def test_no_transcriber_or_no_download_is_unheard() -> None:
    assert await hear(ROW, ListeningOutbound(name="gowa"), None, last_attempt=False) is None
    meta = FakeOutbound(name="whatsapp")  # has no download
    assert await hear(ROW, meta, FakeTranscriber("hi"), last_attempt=False) is None


@pytest.mark.parametrize("text", ["", "   "])
async def test_silence_is_unheard(text: str) -> None:
    assert await hear(ROW, ListeningOutbound(), FakeTranscriber(text), last_attempt=False) is None


async def test_a_download_refused_is_unheard_at_once() -> None:
    channel = ListeningOutbound(download_error=MediaUnavailableError("gone"))
    transcriber = FakeTranscriber("x")
    assert await hear(ROW, channel, transcriber, last_attempt=False) is None
    assert transcriber.calls == []


async def test_a_transcription_refused_is_unheard_at_once() -> None:
    transcriber = FakeTranscriber(error=TranscriptionRefusedError("no"))
    assert await hear(ROW, ListeningOutbound(), transcriber, last_attempt=False) is None


@pytest.mark.parametrize(
    "error", [TransientChannelError("down"), TranscriptionUnavailableError("down")]
)
async def test_an_outage_raises_until_the_last_attempt(error: Exception) -> None:
    def setup() -> tuple[ListeningOutbound, FakeTranscriber]:
        if isinstance(error, TransientChannelError):
            return ListeningOutbound(download_error=error), FakeTranscriber("x")
        return ListeningOutbound(), FakeTranscriber(error=error)

    channel, transcriber = setup()
    with pytest.raises(type(error)):
        await hear(ROW, channel, transcriber, last_attempt=False)
    channel, transcriber = setup()
    assert await hear(ROW, channel, transcriber, last_attempt=True) is None
