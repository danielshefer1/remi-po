"""Reminders and the morning briefing (Iteration 05): the tick and the two jobs it defers.

``schedule:tick`` runs every minute. It reads the two ``SECURITY DEFINER`` scans, which return
ids only, and defers one job per due reminder or briefing on ``webhooks``, the queue that
already sends. Each job is deferred with its idempotency key as both ``queueing_lock`` and
``lock``: a later tick cannot queue a second one while the first waits, and two can never run
at once (finishing a reminder and recording the message are not safe to race). The send
itself is keyed too (ADR 0003), so a re-run sends nothing new. Kwargs are ids only (ADR 0001).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID

import procrastinate
import structlog
from procrastinate.exceptions import AlreadyEnqueued

from personal_organizer.db.engine import get_database
from personal_organizer.db.repositories.reminders import due_briefings, due_reminders
from personal_organizer.messaging.briefing import briefing_key, send_briefing
from personal_organizer.messaging.reminders import reminder_key, send_reminder
from personal_organizer.messaging.runtime import get_outbound_channel
from personal_organizer.settings import get_settings
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks.channel import (
    INBOUND_RETRY,
    agent_calendar,
    agent_llm,
    is_last_attempt,
)

log = structlog.get_logger(__name__)

TICK_TASK: Final = "schedule:tick"
REMINDER_TASK: Final = "schedule:reminder"
BRIEFING_TASK: Final = "schedule:briefing"


async def _defer(app: procrastinate.App, task: str, key: str, **kwargs: str) -> int:
    """Defer one job under ``key``; 0 when one is already queued for it."""
    try:
        await app.configure_task(task, lock=key, queueing_lock=key).defer_async(**kwargs)
    except AlreadyEnqueued:
        return 0
    return 1


async def run_tick(app: procrastinate.App, now: datetime, *, briefings: bool) -> tuple[int, int]:
    """Defer what is due at ``now``. Returns how many reminder and briefing jobs were new."""
    async with get_database().system_session() as session:
        reminders = await due_reminders(session, now)
        days = await due_briefings(session, now) if briefings else []
    sent_reminders = 0
    for reminder_id, tenant_id in reminders:
        sent_reminders += await _defer(
            app,
            REMINDER_TASK,
            reminder_key(reminder_id),
            reminder_id=str(reminder_id),
            tenant_id=str(tenant_id),
        )
    sent_briefings = 0
    for tenant_id, day in days:
        sent_briefings += await _defer(
            app,
            BRIEFING_TASK,
            briefing_key(tenant_id, day),
            tenant_id=str(tenant_id),
            day=day.isoformat(),
        )
    if sent_reminders or sent_briefings:
        log.info("schedule.tick", reminders=sent_reminders, briefings=sent_briefings)
    return sent_reminders, sent_briefings


def register(app: procrastinate.App) -> None:
    @app.periodic(cron="* * * * *")
    @app.task(
        queue=Queue.MAINTENANCE.value,
        name=TICK_TASK,
        queueing_lock=TICK_TASK,
        pass_context=True,
    )
    async def tick(context: procrastinate.JobContext, timestamp: int) -> None:
        # Briefings read the calendar through Composio; without it there is no day to describe.
        await run_tick(
            context.app,
            datetime.fromtimestamp(timestamp, UTC),
            briefings=get_settings().composio.enabled,
        )

    @app.task(queue=Queue.WEBHOOKS.value, name=REMINDER_TASK, retry=INBOUND_RETRY)
    async def reminder(reminder_id: str, tenant_id: str) -> None:
        await send_reminder(
            UUID(reminder_id), UUID(tenant_id), db=get_database(), channels=get_outbound_channel
        )

    @app.task(
        queue=Queue.WEBHOOKS.value, name=BRIEFING_TASK, retry=INBOUND_RETRY, pass_context=True
    )
    async def briefing(context: procrastinate.JobContext, tenant_id: str, day: str) -> None:
        await send_briefing(
            UUID(tenant_id),
            date.fromisoformat(day),
            db=get_database(),
            channels=get_outbound_channel,
            llm=agent_llm(),
            calendar=agent_calendar(),
            now=datetime.now(UTC),
            last_attempt=is_last_attempt(context.job.attempts),
        )


__all__ = ["BRIEFING_TASK", "REMINDER_TASK", "TICK_TASK", "register", "run_tick"]
