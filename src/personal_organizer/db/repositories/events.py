"""Remi's own calendar (issue #50): ``events``, and the sync bookkeeping around it.

``remi`` rows are made here and never touched by an import. ``google`` rows are copies:
:func:`replace_imported` swaps exactly the ones overlapping the window that was read, so an
event deleted in Google drops out of its window.

Run everything here in ``Database.tenant_session(tenant_id)``, except :func:`due_syncs`: the
sync tick's cross-tenant scan, through the ``SECURITY DEFINER`` function of migration 0012, in
``Database.system_session()``. It returns tenant ids only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Final
from uuid import UUID

from sqlalchemy import Uuid, bindparam, column, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import CalendarConnection, Event
from personal_organizer.db.repositories.scope import scoped
from personal_organizer.interfaces.calendar import CalendarEvent

#: How far ahead a sync, an import or a live refresh copies Google's events.
IMPORT_AHEAD: Final = timedelta(days=90)
#: How stale the copy may get before the sync tick refreshes it.
SYNC_EVERY: Final = timedelta(hours=1)


def _to_event(row: Event, zone: tzinfo | None = None) -> CalendarEvent:
    return CalendarEvent(
        external_id=row.external_id or str(row.id),
        title=row.title,
        start=row.starts_at.astimezone(zone),
        end=row.ends_at.astimezone(zone),
        all_day=row.all_day,
        location=row.location,
    )


async def add_event(
    session: AsyncSession,
    tenant_id: UUID,
    event: CalendarEvent,
    *,
    source: str,
    external_id: str | None = None,
) -> CalendarEvent:
    """Store ``event``; its ``external_id`` is the given one, or the new row's id.

    ``None`` and ``""`` both mean "no id" (the codebase's ``CalendarEvent("", ...)``).
    """
    external_id = external_id or None
    result = await session.execute(
        insert(Event)
        .values(
            tenant_id=tenant_id,
            title=event.title,
            starts_at=event.start,
            ends_at=event.end,
            all_day=event.all_day,
            location=event.location,
            source=source,
            external_id=external_id,
        )
        .returning(Event.id)
    )
    row_id: UUID = result.scalar_one()
    if external_id is None:
        await session.execute(
            update(Event).where(Event.id == row_id).values(external_id=str(row_id))
        )
    return CalendarEvent(
        external_id=external_id or str(row_id),
        title=event.title,
        start=event.start,
        end=event.end,
        all_day=event.all_day,
        location=event.location,
    )


async def events_between(
    session: AsyncSession, tenant_id: UUID, start: datetime, end: datetime
) -> list[CalendarEvent]:
    """Events overlapping ``[start, end)``, by start, in ``start``'s zone (so an all-day event
    reads back on its own date). One ending exactly at ``start`` is out."""
    rows = await session.scalars(
        scoped(select(Event), Event, tenant_id)
        .where(Event.starts_at < end, Event.ends_at > start)
        .order_by(Event.starts_at, Event.id)
    )
    return [_to_event(row, start.tzinfo) for row in rows]


async def replace_imported(
    session: AsyncSession,
    tenant_id: UUID,
    start: datetime,
    end: datetime,
    events: Sequence[CalendarEvent],
) -> None:
    """Swap the ``google`` rows overlapping ``[start, end)`` for ``events``."""
    await session.execute(
        scoped(delete(Event), Event, tenant_id).where(
            Event.source == "google", Event.starts_at < end, Event.ends_at > start
        )
    )
    await _upsert_google(session, tenant_id, events)


async def upsert_google(session: AsyncSession, tenant_id: UUID, event: CalendarEvent) -> None:
    """Store Google's copy of one event, replacing the row with its id. Unlike
    :func:`replace_imported`, it deletes nothing: other copies in its span stay."""
    await _upsert_google(session, tenant_id, [event])


async def _upsert_google(
    session: AsyncSession, tenant_id: UUID, events: Sequence[CalendarEvent]
) -> None:
    """Insert-or-update by Google id; ``source`` always follows the incoming row."""
    if not events:
        return
    stmt = pg_insert(Event).values(
        [
            {
                "tenant_id": tenant_id,
                "title": e.title,
                "starts_at": e.start,
                "ends_at": e.end,
                "all_day": e.all_day,
                "location": e.location,
                "source": "google",
                "external_id": e.external_id,
            }
            for e in events
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "external_id"],
            set_={
                "title": stmt.excluded.title,
                "starts_at": stmt.excluded.starts_at,
                "ends_at": stmt.excluded.ends_at,
                "all_day": stmt.excluded.all_day,
                "location": stmt.excluded.location,
                "source": stmt.excluded.source,
            },
        )
    )


async def mark_synced(session: AsyncSession, tenant_id: UUID, now: datetime) -> None:
    """A sync of the tenant's active connection was tried at ``now``, whether or not it worked:
    ``synced_at`` is the last try, so a failing Google is asked again in an hour, not every tick."""
    await session.execute(
        scoped(update(CalendarConnection), CalendarConnection, tenant_id)
        .where(CalendarConnection.status == "active")
        .values(synced_at=now)
    )


async def due_syncs(session: AsyncSession, now: datetime) -> list[UUID]:
    """Tenant ids whose copy of Google is older than ``SYNC_EVERY``. In ``system_session``."""
    due = func.due_syncs(now, bindparam("every", SYNC_EVERY, type_=INTERVAL())).table_valued(
        column("tenant_id", Uuid())
    )
    return list(await session.scalars(select(due.c.tenant_id)))


__all__ = [
    "IMPORT_AHEAD",
    "SYNC_EVERY",
    "add_event",
    "due_syncs",
    "events_between",
    "mark_synced",
    "replace_imported",
    "upsert_google",
]
