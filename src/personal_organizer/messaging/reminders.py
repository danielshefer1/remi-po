"""Sending a due reminder: the logic of the reminder job (Iteration 05).

The send answers no inbound message, so it is keyed ``reminder:<reminder_id>`` (ADR 0003):
however often the job runs, the line goes out at most once. It goes to the tenant's phone on
the channel they last wrote on, through ``messaging.proactive``. Then the reminder leaves
``pending``: ``sent`` and kept in the history as an ``out`` message, or ``failed`` when there
is nowhere to send it or the channel refused it. A re-run that finds the key already settled
(the last run died after the send) still finishes the reminder, without sending again.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import structlog

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.messages import record_outbound
from personal_organizer.db.repositories.reminders import finish_reminder, get_reminder
from personal_organizer.messaging.inbound import ChannelResolver
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.proactive import proactive_target

log = structlog.get_logger(__name__)

REMINDER_KIND: Final = "reminder"


def reminder_key(reminder_id: UUID) -> str:
    """ADR 0003's key: one send per reminder, however many jobs run for it."""
    return f"reminder:{reminder_id}"


async def send_reminder(
    reminder_id: UUID, tenant_id: UUID, *, db: Database, channels: ChannelResolver
) -> str | None:
    """Send the reminder's line at most once, and move it out of ``pending``.

    Returns the outbox status, or ``None`` when nothing was sent: the reminder is gone or no
    longer pending, or there is nowhere to send it (then it is marked ``failed``). Re-raises
    ``TransientChannelError`` from :func:`send_once` with the reminder still ``pending``, which
    the task's retry strategy retries.
    """
    async with db.tenant_session(TenantId(tenant_id)) as session:
        reminder = await get_reminder(session, tenant_id, reminder_id)
    if reminder is None or reminder.status != "pending":
        return None
    target = await proactive_target(db, tenant_id, channels=channels, event="reminder.skipped")
    if target is None:
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await finish_reminder(session, tenant_id, reminder_id, status="failed")
        return None
    text = t("reminder", target.tenant.language, text=reminder.text)
    status = await send_once(
        db,
        target.channel,
        inbox_id=None,
        kind=REMINDER_KIND,
        recipient_key=f"tel:{target.phone}",
        to=target.phone,
        text=text,
        idempotency_key=reminder_key(reminder_id),
    )
    async with db.tenant_session(TenantId(tenant_id)) as session:
        if status == "failed":
            await finish_reminder(session, tenant_id, reminder_id, status="failed")
        else:  # accepted, or settled by an earlier attempt: it went out, or may have
            await finish_reminder(session, tenant_id, reminder_id, status="sent")
            await record_outbound(
                session, tenant_id, inbox_id=None, channel=target.channel.name, body=text
            )
    log.info("reminder.sent", reminder_id=str(reminder_id), status=status)
    return status


__all__ = ["REMINDER_KIND", "reminder_key", "send_reminder"]
