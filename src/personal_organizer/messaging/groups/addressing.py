"""Is a group message for Remi? Decided without a model, so chatter costs nothing.

A message is for Remi when it names it ("Remi", "רמי", with a Hebrew prefix such as "ורמי"),
mentions its number, quotes one of its replies in the group, or comes from the person Remi
just answered there, within :data:`FOLLOW_UP_WINDOW`. Remi's replies in a group are the
``channel_outbox`` rows with ``recipient_key`` ``group:<jid>``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import exists, select

from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.messaging.inbox import InboxRow

#: How long after Remi answers someone their next message is still taken as for Remi.
FOLLOW_UP_WINDOW: Final = timedelta(minutes=5)

# A whole word: "remind" and "רמיקס" are not the name. One Hebrew prefix letter is allowed.
_NAME: Final = re.compile(r"(?<!\w)(?:remi|[ושהלב]?רמי)(?!\w)", re.IGNORECASE)


def group_key(jid: str) -> str:
    """The ``recipient_key`` of Remi's sends to a group."""
    return f"group:{jid}"


def names_remi(text: str | None, *, bot_phone: str | None) -> bool:
    if text is None:
        return False
    if _NAME.search(text):
        return True
    if bot_phone is None:
        return False
    return re.search(rf"@{re.escape(bot_phone.removeprefix('+'))}(?!\d)", text) is not None


async def is_addressed(
    db: Database, row: InboxRow, *, group_jid: str, bot_phone: str | None, now: datetime
) -> bool:
    if names_remi(row.body, bot_phone=bot_phone):
        return True
    key = group_key(group_jid)
    async with db.system_session() as session:
        if row.context_message_id is not None and await session.scalar(
            select(
                exists().where(
                    ChannelOutbox.recipient_key == key,
                    ChannelOutbox.provider_message_id == row.context_message_id,
                )
            )
        ):
            return True
        # ``sender_key`` is never purged, so the answered message's sender outlives its body.
        last = (
            await session.execute(
                select(ChannelOutbox.created_at, ChannelInbox.sender_key)
                .outerjoin(ChannelInbox, ChannelInbox.id == ChannelOutbox.inbox_id)
                .where(ChannelOutbox.recipient_key == key)
                .order_by(ChannelOutbox.created_at.desc())
                .limit(1)
            )
        ).first()
    return (
        last is not None
        and last.sender_key == row.sender_key
        and now - last.created_at <= FOLLOW_UP_WINDOW
    )


__all__ = ["FOLLOW_UP_WINDOW", "group_key", "is_addressed", "names_remi"]
