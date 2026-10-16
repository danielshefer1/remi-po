"""Voice notes: what a member said, as text, before anything else reads the message
(Iteration 06).

The tenant gate calls :func:`hear` for audio from someone it serves, never a stranger. The
row it returns has the transcript as ``body`` and keeps ``message_type="audio"``, so language
detection, onboarding, the confirm, the agent and ``messages`` all just read text. ``None``
means the note can't be heard; the gate decides what to say. The transcript is never logged,
and the audio lives only in this job's memory.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import structlog

from personal_organizer.core.errors import (
    MediaUnavailableError,
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
    TransientChannelError,
)
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.interfaces.voice import MediaSource, Transcriber
from personal_organizer.messaging.inbox import InboxRow

log = structlog.get_logger(__name__)

AUDIO: Final = "audio"
#: The outbox kind of the "couldn't catch that" line.
VOICE_UNHEARD: Final = "voice:unheard"


async def hear(
    row: InboxRow,
    channel: OutboundChannel,
    transcriber: Transcriber | None,
    *,
    last_attempt: bool,
) -> InboxRow | None:
    """``row`` with its voice note's words as ``body``, or ``None`` when it can't be heard.

    An outage (gateway or transcriber) raises for the job to retry, until ``last_attempt``.
    """
    if transcriber is None or not isinstance(channel, MediaSource):
        log.info("voice.unheard", inbox_id=str(row.id), reason="unsupported")
        return None
    try:
        audio = await channel.download(
            row.provider_message_id, phone=row.sender_phone, user_id=row.sender_user_id
        )
        text = (await transcriber.transcribe(audio)).strip()
    except (MediaUnavailableError, TranscriptionRefusedError) as exc:
        log.warning("voice.unheard", inbox_id=str(row.id), reason=exc.code)
        return None
    except (TransientChannelError, TranscriptionUnavailableError) as exc:
        if not last_attempt:
            raise
        log.warning("voice.unheard", inbox_id=str(row.id), reason=exc.code)
        return None
    if not text:
        log.info("voice.unheard", inbox_id=str(row.id), reason="silent")
        return None
    log.info("voice.heard", inbox_id=str(row.id), chars=len(text))
    return replace(row, body=text)


__all__ = ["AUDIO", "VOICE_UNHEARD", "hear"]
