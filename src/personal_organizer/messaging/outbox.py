"""Sending a reply at most once.

WhatsApp's Cloud API has no idempotency key, and a job can run more than once: Procrastinate
retries a transient failure, and a job interrupted by a deploy is retried from the top. So
each send is claimed in ``channel_outbox`` before it is sent. A reply to an inbound message
is keyed on ``(inbox_id, kind)``. A send that answers nothing ("You're all set", reminders
later) is keyed on an ``idempotency_key`` that names the event, e.g.
``connected:<connection_id>`` (D6). The key is unique across kinds and must never contain
message content. Either way the row moves through::

    pending -> sending -> accepted            (Meta took it; its statuses follow)
                       -> failed              (Meta refused; not retried)
                       -> unknown             (may or may not have arrived; not retried)
                       -> pending             (definitely not sent; the job retries)

A retry that finds the row still ``sending`` knows the previous attempt died mid-send and
cannot know whether the message arrived -- so it marks the row ``unknown`` and does *not*
send again. Missing one acknowledgement is better than sending two. See docs/adr/0003.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelOutbox
from personal_organizer.interfaces.channel import OutboundChannel, OutboundMessage

log = structlog.get_logger(__name__)

#: States in which the reply is finished with, one way or another.
SETTLED: Final = frozenset({"unknown", "accepted", "sent", "delivered", "read", "failed"})


async def _set(db: Database, outbox_id: UUID, **values: object) -> None:
    async with db.system_session() as session:
        await session.execute(
            update(ChannelOutbox)
            .where(ChannelOutbox.id == outbox_id)
            .values(**values, updated_at=func.now())
        )


async def _claim(
    db: Database,
    *,
    channel: str,
    inbox_id: UUID | None,
    kind: str,
    recipient_key: str,
    idempotency_key: str | None,
) -> tuple[UUID, str]:
    """Create or lock this send's row and move it to ``sending`` if it is ours to send.

    Returns the row id and the status the caller should act on: ``sending`` means send now;
    anything in :data:`SETTLED` means a previous attempt already decided the outcome.
    """
    if idempotency_key is not None:
        conflict = ["idempotency_key"]
        this_send = ChannelOutbox.idempotency_key == idempotency_key
    else:
        conflict = ["inbox_id", "kind"]
        this_send = and_(ChannelOutbox.inbox_id == inbox_id, ChannelOutbox.kind == kind)
    async with db.system_session() as session:
        await session.execute(
            insert(ChannelOutbox)
            .values(
                channel=channel,
                inbox_id=inbox_id,
                kind=kind,
                recipient_key=recipient_key,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=conflict)
        )
        row = (
            await session.execute(select(ChannelOutbox).where(this_send).with_for_update())
        ).scalar_one()
        if row.status == "sending":
            # The previous attempt died between claiming and recording the outcome.
            row.status, row.updated_at = "unknown", datetime.now(UTC)
            log.warning("outbox.outcome_unknown", outbox_id=str(row.id), reason="interrupted")
        elif row.status == "pending":
            row.status, row.updated_at = "sending", datetime.now(UTC)
            return row.id, "sending"
        return row.id, row.status


async def already_sent(db: Database, inbox_id: UUID, kind: str) -> bool:
    """Whether an earlier attempt claimed the ``kind`` reply to ``inbox_id``: it went out, or
    may have. A caller can then skip working out a reply :func:`send_once` would not send."""
    async with db.system_session() as session:
        status = await session.scalar(
            select(ChannelOutbox.status).where(
                ChannelOutbox.inbox_id == inbox_id, ChannelOutbox.kind == kind
            )
        )
    return status is not None and status != "pending"


async def sent_message_id(db: Database, inbox_id: UUID, kind: str) -> str | None:
    """The provider's id for the ``kind`` send to ``inbox_id``, if it was accepted."""
    async with db.system_session() as session:
        return await session.scalar(
            select(ChannelOutbox.provider_message_id).where(
                ChannelOutbox.inbox_id == inbox_id, ChannelOutbox.kind == kind
            )
        )


async def send_once(
    db: Database,
    channel: OutboundChannel,
    *,
    inbox_id: UUID | None,
    kind: str,
    recipient_key: str,
    to: str,
    text: str,
    idempotency_key: str | None = None,
    poll_options: tuple[str, ...] = (),
) -> str:
    """Send ``text`` to ``to`` at most once: as the ``kind`` reply to ``inbox_id``, or as the
    send named by ``idempotency_key``. Exactly one of the two is given.

    Returns the send's resulting status. Re-raises :class:`TransientChannelError` -- and only
    that -- after putting the row back to ``pending``, so the job's retry sends it again.
    """
    if (inbox_id is None) == (idempotency_key is None):
        msg = "send_once needs exactly one of inbox_id and idempotency_key"
        raise ValueError(msg)
    outbox_id, status = await _claim(
        db,
        channel=channel.name,
        inbox_id=inbox_id,
        kind=kind,
        recipient_key=recipient_key,
        idempotency_key=idempotency_key,
    )
    if status != "sending":
        return status

    try:
        provider_message_id = await channel.send_text(
            OutboundMessage(recipient=to, body=text, poll_options=poll_options)
        )
    except TransientChannelError:
        await _set(db, outbox_id, status="pending")
        raise
    except RejectedChannelError as exc:
        await _set(db, outbox_id, status="failed", error_code=exc.provider_code)
        log.warning("outbox.rejected", outbox_id=str(outbox_id), error_code=exc.provider_code)
        return "failed"
    except AmbiguousDeliveryError:
        await _set(db, outbox_id, status="unknown")
        log.warning("outbox.outcome_unknown", outbox_id=str(outbox_id), reason="no_response")
        return "unknown"

    await _set(db, outbox_id, status="accepted", provider_message_id=provider_message_id)
    log.info("outbox.accepted", outbox_id=str(outbox_id), channel=channel.name)
    return "accepted"


__all__ = ["SETTLED", "already_sent", "send_once", "sent_message_id"]
