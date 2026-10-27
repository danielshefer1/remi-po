"""Reminders and the morning-briefing setting (Iteration 05).

A reminder is ``pending`` until it is ``sent``, ``cancelled``, or ``failed`` (the channel
refused it, or there was nowhere to send it). Each move out of ``pending`` is one ``UPDATE``
guarded on ``pending``, so it happens once. ``tenants.briefing_at`` is the local time of the
briefing (``NULL`` is off), and ``tenants.briefed_on`` the last local day one went out.

Run everything here in ``Database.tenant_session(tenant_id)``, except :func:`due_reminders`
and :func:`due_briefings`: those are the tick's cross-tenant scan, through the two
``SECURITY DEFINER`` functions of migration 0007, in ``Database.system_session()``. They
return ids only.
"""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Date, Uuid, column, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Reminder, Tenant
from personal_organizer.db.repositories.scope import scoped
from personal_organizer.messaging.recurrence import next_after


async def add_reminder(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    text: str,
    due_at: datetime,
    rule: str | None = None,
) -> UUID:
    """Add a reminder, or return the identical pending one: a turn retried after its
    ``set_reminder`` must not set it twice. The sender lock serialises a tenant's turns."""
    same: UUID | None = await session.scalar(
        scoped(select(Reminder.id), Reminder, tenant_id).where(
            Reminder.status == "pending",
            Reminder.text == text,
            Reminder.due_at == due_at,
            Reminder.rule.is_not_distinct_from(rule),
        )
    )
    if same is not None:
        return same
    result = await session.execute(
        insert(Reminder)
        .values(tenant_id=tenant_id, text=text, due_at=due_at, rule=rule)
        .returning(Reminder.id)
    )
    reminder_id: UUID = result.scalar_one()
    return reminder_id


async def upcoming_reminders(
    session: AsyncSession, tenant_id: UUID, *, after: datetime, before: datetime | None = None
) -> list[Reminder]:
    """``pending`` reminders due after ``after`` (and before ``before``), soonest first."""
    stmt = scoped(select(Reminder), Reminder, tenant_id).where(
        Reminder.status == "pending", Reminder.due_at > after
    )
    if before is not None:
        stmt = stmt.where(Reminder.due_at < before)
    return list(await session.scalars(stmt.order_by(Reminder.due_at)))


async def get_reminder(
    session: AsyncSession, tenant_id: UUID, reminder_id: UUID
) -> Reminder | None:
    reminder: Reminder | None = await session.scalar(
        scoped(select(Reminder), Reminder, tenant_id).where(Reminder.id == reminder_id)
    )
    return reminder


async def schedule_next(
    session: AsyncSession,
    tenant_id: UUID,
    reminder: Reminder,
    zone: ZoneInfo,
    *,
    after: datetime,
) -> UUID | None:
    """Insert the next pending occurrence of a repeating ``reminder``, strictly after both its
    own time and ``after``. ``None`` for a one-off, or once the rule's ``UNTIL`` has passed.

    Steps from the occurrence's own local time, so a skipped one keeps its 08:00 rather than
    taking the clock time of ``after``. Rules are at least daily, so the loop is short."""
    if reminder.rule is None:
        return None
    floor = max(reminder.due_at, after)
    start = reminder.due_at.astimezone(zone)
    nxt = next_after(reminder.rule, start)
    while nxt is not None and nxt <= floor:
        nxt = next_after(reminder.rule, nxt)
    if nxt is None:
        return None
    result = await session.execute(
        insert(Reminder)
        .values(
            tenant_id=tenant_id,
            text=reminder.text,
            due_at=nxt,
            rule=reminder.rule,
            series_id=reminder.series_id or reminder.id,
        )
        .returning(Reminder.id)
    )
    next_id: UUID = result.scalar_one()
    return next_id


async def recent_sent(session: AsyncSession, tenant_id: UUID, *, limit: int = 5) -> list[Reminder]:
    """The last ``limit`` reminders that went off, newest first, of any age. A repeating
    series counts once, by its latest, so a daily one doesn't push the rest out."""
    series = func.coalesce(Reminder.series_id, Reminder.id)  # the first row's id is the key
    latest = (
        scoped(select(Reminder.id), Reminder, tenant_id)
        .where(Reminder.status == "sent")
        .ext(distinct_on(series))
        .order_by(series, Reminder.due_at.desc())
    )
    return list(
        await session.scalars(
            scoped(select(Reminder), Reminder, tenant_id)
            .where(Reminder.id.in_(latest))
            .order_by(Reminder.due_at.desc(), Reminder.id.desc())
            .limit(limit)
        )
    )


async def _leave_pending(
    session: AsyncSession, tenant_id: UUID, reminder_id: UUID, status: str
) -> bool:
    result = await session.execute(
        scoped(update(Reminder), Reminder, tenant_id)
        .where(Reminder.id == reminder_id, Reminder.status == "pending")
        .values(status=status)
        .returning(Reminder.id)
    )
    return result.first() is not None


async def cancel_reminder(session: AsyncSession, tenant_id: UUID, reminder_id: UUID) -> bool:
    """``pending`` → ``cancelled``. ``False`` if it was not pending (or is not theirs)."""
    return await _leave_pending(session, tenant_id, reminder_id, "cancelled")


async def finish_reminder(
    session: AsyncSession, tenant_id: UUID, reminder_id: UUID, *, status: str
) -> bool:
    """``pending`` → ``sent`` or ``failed``. ``False`` (left alone) if it was not pending."""
    return await _leave_pending(session, tenant_id, reminder_id, status)


async def set_briefing(session: AsyncSession, tenant_id: UUID, at: time | None) -> None:
    """Move the morning briefing to local time ``at``, or stop it with ``None``."""
    await session.execute(
        scoped(update(Tenant), Tenant, tenant_id).values(briefing_at=at, updated_at=func.now())
    )


async def mark_briefed(session: AsyncSession, tenant_id: UUID, day: date) -> bool:
    """Local ``day``'s briefing went out: a re-run or a later tick skips it. ``False`` if the
    day was already marked."""
    result = await session.execute(
        scoped(update(Tenant), Tenant, tenant_id)
        .where(Tenant.briefed_on.is_distinct_from(day))
        .values(briefed_on=day)
        .returning(Tenant.id)
    )
    return result.first() is not None


async def due_reminders(session: AsyncSession, now: datetime) -> list[tuple[UUID, UUID]]:
    """``(reminder_id, tenant_id)`` of every reminder due at ``now``. In ``system_session``."""
    due = func.due_reminders(now).table_valued(
        column("reminder_id", Uuid()), column("tenant_id", Uuid())
    )
    rows = await session.execute(select(due.c.reminder_id, due.c.tenant_id))
    return [(row[0], row[1]) for row in rows]


async def due_briefings(session: AsyncSession, now: datetime) -> list[tuple[UUID, date]]:
    """``(tenant_id, local_day)`` of every briefing due at ``now``. In ``system_session``."""
    due = func.due_briefings(now).table_valued(
        column("tenant_id", Uuid()), column("local_day", Date())
    )
    rows = await session.execute(select(due.c.tenant_id, due.c.local_day))
    return [(row[0], row[1]) for row in rows]


__all__ = [
    "add_reminder",
    "cancel_reminder",
    "due_briefings",
    "due_reminders",
    "finish_reminder",
    "get_reminder",
    "mark_briefed",
    "recent_sent",
    "schedule_next",
    "set_briefing",
    "upcoming_reminders",
]
