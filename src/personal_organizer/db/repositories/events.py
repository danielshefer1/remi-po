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
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Final
from uuid import UUID

from sqlalchemy import Uuid, and_, bindparam, column, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.core.errors import EventChangedError
from personal_organizer.db.models.tenant import CalendarConnection, Event
from personal_organizer.db.repositories.scope import scoped
from personal_organizer.interfaces.calendar import CalendarEvent, EventChange
from personal_organizer.messaging.recurrence import first_at, occurrences, shift_until, until_before

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
        rule=row.rule,
        series_id=row.series_id,
        invited=row.invited,
    )


async def add_event(
    session: AsyncSession,
    tenant_id: UUID,
    event: CalendarEvent,
    *,
    source: str,
    external_id: str | None = None,
    original_start: datetime | None = None,
) -> CalendarEvent:
    """Store ``event``; its ``external_id`` is the given one, or the new row's id.
    ``original_start`` is the series date a changed single date replaces.

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
            rule=event.rule,
            series_id=event.series_id,
            original_start=original_start,
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
        rule=event.rule,
        series_id=event.series_id,
    )


async def events_between(
    session: AsyncSession,
    tenant_id: UUID,
    start: datetime,
    end: datetime,
    *,
    zone: tzinfo | None = None,
) -> list[CalendarEvent]:
    """Events overlapping ``[start, end)``, by start, in ``start``'s zone (so an all-day event
    reads back on its own date). One ending exactly at ``start`` is out. A repeating event comes
    back as its occurrences, worked out in ``zone`` (the owner's; ``start``'s when not given)."""
    zone = zone or start.tzinfo or UTC
    rows = await session.scalars(
        scoped(select(Event), Event, tenant_id).where(
            or_(
                and_(Event.rule.is_(None), Event.starts_at < end, Event.ends_at > start),
                and_(Event.rule.is_not(None), Event.starts_at < end),
            )
        )
    )
    found = [o for row in rows for o in occurrences(_to_event(row), start, end, zone, row.skipped)]
    found = [
        replace(e, start=e.start.astimezone(start.tzinfo), end=e.end.astimezone(start.tzinfo))
        for e in found
    ]
    return sorted(found, key=lambda e: (e.start, e.external_id))


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
                "series_id": e.series_id,
                "invited": e.invited,
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
                "series_id": stmt.excluded.series_id,
                "invited": stmt.excluded.invited,
            },
        )
    )


async def find_row(session: AsyncSession, tenant_id: UUID, external_id: str) -> Event | None:
    """The row with ``external_id``; failing that, the ``remi`` series row a native occurrence id
    ``<row id>:<YYYYMMDDTHHMM>`` names. Exact first: Google's own ids may hold ':' too."""
    row: Event | None = await session.scalar(
        scoped(select(Event), Event, tenant_id).where(Event.external_id == external_id)
    )
    if row is None and ":" in external_id:
        row = await session.scalar(
            scoped(select(Event), Event, tenant_id).where(
                Event.external_id == external_id.rpartition(":")[0],
                Event.source == "remi",
                Event.rule.is_not(None),
            )
        )
    return row


async def change_event(
    session: AsyncSession, tenant_id: UUID, change: EventChange, zone: tzinfo
) -> None:
    """Apply ``change`` to Remi's own rows. Raises :class:`EventChangedError`, writing nothing,
    when the target is no longer there as it was listed (gone, renamed, moved or skipped).

    A series is changed on the wall clock in ``zone``, as :func:`occurrences` reads it: one date
    is skipped (and, if changed, stored as its own row), "following" ends the series just before
    the date and starts a new one there, "all" shifts the whole series and its skips.
    """
    target = change.target
    row = await find_row(session, tenant_id, target.external_id)
    if row is None or row.source != "remi" or row.title != target.title:
        raise EventChangedError
    if row.rule is None:
        if row.starts_at != target.start:
            raise EventChangedError
        if row.series_id is None or change.which == "this":
            await _change_row(session, row, change, zone)
            return
        # One changed date of a series: "following" and "all" act on the series from its date.
        series, at = await find_row(session, tenant_id, row.series_id), row.original_start
        if series is None or series.source != "remi" or series.rule is None or at is None:
            raise EventChangedError
        row = series
    else:
        at = target.start
        near = occurrences(_to_event(row), at - _MINUTE, at + _MINUTE, zone, row.skipped)
        if not any(o.start == at for o in near):
            raise EventChangedError
    at = at.astimezone(zone)
    if change.which == "this":
        await _change_date(session, tenant_id, row, change, at, zone)
    elif change.which == "following" and at > row.starts_at:
        await _change_following(session, tenant_id, row, change, at, zone)
    else:
        await _change_all(session, tenant_id, row, change, at, zone)


_MINUTE: Final = timedelta(minutes=1)


def _moved(
    change: EventChange, start: datetime, end: datetime, zone: tzinfo
) -> tuple[datetime, datetime]:
    """The new start and end of a ``start``-``end`` occurrence; an end not given moves with the
    start (on the wall clock, so an hour stays an hour across DST)."""
    start, end = start.astimezone(zone), end.astimezone(zone)
    new_start = change.start.astimezone(zone) if change.start else start
    return new_start, change.end or end + (new_start - start)


def _occurrence_end(row: Event, at: datetime, zone: tzinfo) -> datetime:
    return at + (row.ends_at.astimezone(zone) - row.starts_at.astimezone(zone))


def _changed(row: Event, change: EventChange, start: datetime, end: datetime) -> CalendarEvent:
    """``row``'s title, location and all-day flag, changed where ``change`` gives them."""
    location = row.location if change.location is None else change.location or None
    title = change.title or row.title
    return CalendarEvent("", title, start, end, all_day=row.all_day, location=location)


def _set(row: Event, new: CalendarEvent) -> None:
    row.title, row.location = new.title, new.location
    row.starts_at, row.ends_at = new.start, new.end


async def _change_row(session: AsyncSession, row: Event, change: EventChange, zone: tzinfo) -> None:
    """An event that doesn't repeat, or one changed date of a series."""
    if change.delete:
        await session.delete(row)
        return
    _set(row, _changed(row, change, *_moved(change, row.starts_at, row.ends_at, zone)))


async def _change_date(
    session: AsyncSession,
    tenant_id: UUID,
    row: Event,
    change: EventChange,
    at: datetime,
    zone: tzinfo,
) -> None:
    row.skipped = [*row.skipped, at]
    if not change.delete:
        times = _moved(change, at, _occurrence_end(row, at, zone), zone)
        date = replace(_changed(row, change, *times), series_id=row.external_id)
        await add_event(session, tenant_id, date, source="remi", original_start=at)


async def _change_following(
    session: AsyncSession,
    tenant_id: UUID,
    row: Event,
    change: EventChange,
    at: datetime,
    zone: tzinfo,
) -> None:
    """End the series just before ``at``; the dates after it are replaced, skips and changed
    single dates included, by the new series (none for a delete)."""
    assert row.rule is not None
    rule, row.rule = row.rule, until_before(row.rule, at)
    row.skipped = [s for s in row.skipped if s < at]
    await session.execute(
        scoped(delete(Event), Event, tenant_id).where(
            Event.series_id == row.external_id,
            Event.source == "remi",
            Event.original_start >= at,
        )
    )
    if change.delete:
        return
    start, end = _moved(change, at, _occurrence_end(row, at, zone), zone)
    if change.rule:
        first = first_at(change.rule, start)
        start, end, rule = first, end + (first - start), change.rule
    else:
        rule = shift_until(rule, start - at)
    await add_event(
        session, tenant_id, replace(_changed(row, change, start, end), rule=rule), source="remi"
    )


async def _change_all(
    session: AsyncSession,
    tenant_id: UUID,
    row: Event,
    change: EventChange,
    at: datetime,
    zone: tzinfo,
) -> None:
    """Delete the series and its changed dates, or change it: a new time for ``at`` shifts every
    date, skip and ``UNTIL`` by as much; changed single dates keep their own times."""
    of_series = scoped(select(Event), Event, tenant_id).where(
        Event.series_id == row.external_id, Event.source == "remi"
    )
    if change.delete:
        await session.execute(
            scoped(delete(Event), Event, tenant_id).where(
                or_(Event.id == row.id, Event.id.in_(of_series.with_only_columns(Event.id)))
            )
        )
        return
    assert row.rule is not None
    end = _occurrence_end(row, at, zone)
    start, new_end = _moved(change, at, end, zone)
    by = start - at
    new_ends_at = row.ends_at.astimezone(zone) + (new_end - end)
    _set(row, _changed(row, change, row.starts_at.astimezone(zone) + by, new_ends_at))
    row.rule = shift_until(row.rule, by)
    row.skipped = [s.astimezone(zone) + by for s in row.skipped]
    if by:
        # Keep each changed date pointing at the (still skipped) date it replaces.
        for date in await session.scalars(of_series):
            if date.original_start is not None:
                date.original_start = date.original_start.astimezone(zone) + by


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
    "change_event",
    "due_syncs",
    "events_between",
    "find_row",
    "mark_synced",
    "replace_imported",
    "upsert_google",
]
