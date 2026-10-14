"""Conversation content under RLS (D7), and which channel a tenant last wrote from.

Replies are recorded too (``direction="out"``), in the transaction that records the message
they answer. Both rows then share ``now()``, so the history orders by
``(created_at, direction = 'out')``: the message before its reply.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Message
from personal_organizer.db.repositories.scope import scoped


async def record_inbound(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    inbox_id: UUID,
    channel: str,
    message_type: str,
    body: str | None,
    sent_at: datetime,
) -> UUID:
    """Copy one inbound message into ``messages``. Returns the new row's id.

    The caller nulls the inbox row's content in the same transaction (D7).
    """
    result = await session.execute(
        insert(Message)
        .values(
            tenant_id=tenant_id,
            direction="in",
            channel=channel,
            inbox_id=inbox_id,
            message_type=message_type,
            body=body,
            sent_at=sent_at,
        )
        .returning(Message.id)
    )
    message_id: UUID = result.scalar_one()
    return message_id


async def record_outbound(
    session: AsyncSession, tenant_id: UUID, *, inbox_id: UUID, channel: str, body: str
) -> None:
    """Record the reply to ``inbox_id``, so the next turn's history has it (D4)."""
    await session.execute(
        insert(Message).values(
            tenant_id=tenant_id,
            direction="out",
            channel=channel,
            inbox_id=inbox_id,
            message_type="text",
            body=body,
            sent_at=func.now(),
        )
    )


async def recent_messages(session: AsyncSession, tenant_id: UUID, *, limit: int) -> list[Message]:
    """The tenant's last ``limit`` messages with text, both directions, oldest first."""
    newest_first = (
        await session.scalars(
            scoped(select(Message), Message, tenant_id)
            # Some text, not just none: the model's API refuses a blank turn.
            .where(Message.body.regexp_match(r"\S"))
            .order_by(Message.created_at.desc(), (Message.direction == "out").desc())
            .limit(limit)
        )
    ).all()
    return list(reversed(newest_first))


async def latest_inbound_channel(session: AsyncSession, tenant_id: UUID) -> str | None:
    """The channel name of the tenant's most recent inbound message: where a send that
    answers nothing goes (ADR 0004's "proactive sends need a channel choice")."""
    channel: str | None = await session.scalar(
        scoped(select(Message.channel), Message, tenant_id)
        .where(Message.direction == "in")
        .order_by(Message.sent_at.desc(), Message.created_at.desc())
        .limit(1)
    )
    return channel


__all__ = ["latest_inbound_channel", "recent_messages", "record_inbound", "record_outbound"]
