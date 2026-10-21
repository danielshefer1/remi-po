"""Reminders, the morning briefing (Iteration 05) and outing follow-ups (Iteration 07): the
tick and the three jobs it defers.

``schedule:tick`` runs every minute. It reads the three ``SECURITY DEFINER`` scans, which
return ids only, and defers one job per due reminder, briefing or follow-up on ``webhooks``,
the queue that already sends. Each job is deferred with its idempotency key as its ``lock``, so
two never run at once, and only when no job with that lock is waiting or running. Not a
``queueing_lock``: it is unique over waiting jobs only, so a job queued while the first runs
would make retrying the first (its worker died) fail, leaving it ``doing`` and the second stuck
behind its lock. Two near-simultaneous ticks can still both queue one; the bodies are safe to
run twice, and the send itself is keyed (ADR 0003), so a re-run sends nothing new. Kwargs are
ids only (ADR 0001). A follow-up's lock takes the UTC day, as the scan gives no time zone; its
send is keyed by the person's own day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID

import procrastinate
import structlog

from personal_organizer.db.engine import get_database
from personal_organizer.db.repositories.groups import due_nudges
from personal_organizer.db.repositories.reminders import due_briefings, due_reminders
from personal_organizer.messaging.briefing import briefing_key, send_briefing
from personal_organizer.messaging.groups.nudge import nudge_key, send_nudge
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
NUDGE_TASK: Final = "schedule:nudge"


async def _defer(app: procrastinate.App, task: str, key: str, **kwargs: str) -> int:
    """Defer one job under ``key``; 0 when one is already waiting or running for it."""
    jobs = await app.job_manager.list_jobs_async(lock=key)
    if any(job.status in ("todo", "doing") for job in jobs):
        return 0
    await app.configure_task(task, lock=key).defer_async(**kwargs)
    return 1


async def run_tick(
    app: procrastinate.App, now: datetime, *, briefings: bool
) -> tuple[int, int, int]:
    """Defer what is due at ``now``. Returns how many reminder, briefing and follow-up jobs
    were new.

    Reminders are scanned and deferred first, on their own, so a failing briefing scan never
    holds them back. Follow-ups come last: they need no calendar, so not ``briefings``.
    """
    async with get_database().system_session() as session:
        reminders = await due_reminders(session, now)
    sent_reminders = 0
    for reminder_id, tenant_id in reminders:
        sent_reminders += await _defer(
            app,
            REMINDER_TASK,
            reminder_key(reminder_id),
            reminder_id=str(reminder_id),
            tenant_id=str(tenant_id),
        )
    days = []
    if briefings:
        async with get_database().system_session() as session:
            days = await due_briefings(session, now)
    sent_briefings = 0
    for tenant_id, day in days:
        sent_briefings += await _defer(
            app,
            BRIEFING_TASK,
            briefing_key(tenant_id, day),
            tenant_id=str(tenant_id),
            day=day.isoformat(),
        )
    async with get_database().system_session() as session:
        nudges = await due_nudges(session, now)
    sent_nudges = 0
    for poll_id, tenant_id in nudges:
        sent_nudges += await _defer(
            app,
            NUDGE_TASK,
            nudge_key(poll_id, tenant_id, now.date()),
            poll_id=str(poll_id),
            tenant_id=str(tenant_id),
        )
    if sent_reminders or sent_briefings or sent_nudges:
        log.info(
            "schedule.tick", reminders=sent_reminders, briefings=sent_briefings, nudges=sent_nudges
        )
    return sent_reminders, sent_briefings, sent_nudges


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

    @app.task(queue=Queue.WEBHOOKS.value, name=NUDGE_TASK, retry=INBOUND_RETRY, pass_context=True)
    async def nudge(context: procrastinate.JobContext, poll_id: str, tenant_id: str) -> None:
        await send_nudge(
            UUID(poll_id),
            UUID(tenant_id),
            db=get_database(),
            channels=get_outbound_channel,
            llm=agent_llm(),
            now=datetime.now(UTC),
            last_attempt=is_last_attempt(context.job.attempts),
        )


__all__ = ["BRIEFING_TASK", "NUDGE_TASK", "REMINDER_TASK", "TICK_TASK", "register", "run_tick"]
