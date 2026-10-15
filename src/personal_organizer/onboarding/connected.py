"""Telling a tenant they are connected: the logic of the ``onboarding:connected`` task.

This send answers no inbound message, so it has no inbox row to key on and no channel to
follow. It is keyed instead (D6, ``connected:<connection_id>``), which makes a refreshed
callback, and so two jobs, one message. It goes to the tenant's phone on the channel they last
wrote on, through ``messaging.proactive``: ADR 0004's "proactive sends need a channel choice".
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from personal_organizer.db.engine import Database
from personal_organizer.messaging.inbound import ChannelResolver
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.proactive import proactive_target

ALL_SET_KIND: Final = "onboarding:all_set"


def all_set_key(connection_id: UUID) -> str:
    """D6's key: one "all set" per connection, however many callbacks bound it."""
    return f"connected:{connection_id}"


async def announce_connected(
    tenant_id: UUID, connection_id: UUID, *, db: Database, channels: ChannelResolver
) -> str | None:
    """Send "You're all set" at most once.

    Returns the outbox status, or ``None`` when there is nowhere to send it: no inbound message,
    no phone, a channel this worker does not run, or a tenant no longer active. Each of those is
    logged and finishes the job, because a retry would find the same thing. Re-raises
    ``TransientChannelError`` from :func:`send_once`, which the task's retry strategy retries.
    """
    target = await proactive_target(
        db, tenant_id, channels=channels, event="onboarding.all_set_skipped"
    )
    if target is None:
        return None
    return await send_once(
        db,
        target.channel,
        inbox_id=None,
        kind=ALL_SET_KIND,
        recipient_key=f"tel:{target.phone}",
        to=target.phone,
        text=t("all_set", target.tenant.language),
        idempotency_key=all_set_key(connection_id),
    )


__all__ = ["ALL_SET_KIND", "all_set_key", "announce_connected"]
