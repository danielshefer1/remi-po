"""The worker's snapshot of one ``channel_inbox`` row.

Read once at the start of the job and passed down, so every decision the job makes is made
from the same values. In its own module so the tenant lookup and the onboarding steps can
take it without importing the gate (``messaging.inbound``) that calls them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: UUID
    channel: str
    provider_message_id: str
    #: ``SenderRef.key``: ``uid:<BSUID>`` when the provider gave one, ``tel:<E.164>`` otherwise.
    sender_key: str
    sender_user_id: str | None
    sender_phone: str | None
    message_type: str
    body: str | None
    #: The id of the button, list row or GOWA selection the user picked, if any (D9).
    reply_id: str | None
    sent_at: datetime
    processed_at: datetime | None
    disposition: str | None
    group_id: str | None = None
    sender_name: str | None = None

    @classmethod
    def of(cls, row: ChannelInbox) -> InboxRow:
        return cls(
            id=row.id,
            channel=row.channel,
            provider_message_id=row.provider_message_id,
            sender_key=row.sender_key,
            sender_user_id=row.sender_user_id,
            sender_phone=row.sender_phone,
            message_type=row.message_type,
            body=row.body,
            reply_id=row.reply_id,
            sent_at=row.sent_at,
            processed_at=row.processed_at,
            disposition=row.disposition,
            group_id=row.group_id,
            sender_name=row.sender_name,
        )


async def load_row(db: Database, inbox_id: UUID) -> InboxRow | None:
    async with db.system_session() as session:
        row = await session.get(ChannelInbox, inbox_id)
        return InboxRow.of(row) if row is not None else None


__all__ = ["InboxRow", "load_row"]
