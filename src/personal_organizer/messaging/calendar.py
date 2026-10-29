"""One calendar per member (issue #50): Remi's own, with Google's events copied in.

``TenantCalendar`` is the worker's, over Postgres. ``InMemoryTenantCalendar`` is ``po-chat``'s
and the tests' (no database). With Google connected, ``add`` writes to Google first and keeps
the copy it returns; ``events`` reads the stored copy, refreshing it from Google first only
when ``live``. ``change`` edits or deletes Remi's own rows in place, and Google's events in
Google (never only the copy), then refreshes the copy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderError,
    CalendarProviderRejectedError,
    EventChangedError,
    SeriesSplitError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.db.repositories import events as event_rows
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.calendar import CalendarEvent, CalendarProvider, EventChange
from personal_organizer.messaging.recurrence import end_lines, first_at, local_rule, occurrences
from personal_organizer.providers.calendar.composio import PAGE_SIZE

log = structlog.get_logger(__name__)


class Calendar(Protocol):
    connected: bool
    live: bool

    async def events(self, start: datetime, end: datetime) -> Sequence[CalendarEvent]: ...
    async def add(self, event: CalendarEvent) -> CalendarEvent: ...
    async def change(self, change: EventChange) -> None: ...
    async def series_rule(self, event: CalendarEvent) -> str | None: ...


def _moved(event: CalendarEvent, change: EventChange) -> tuple[datetime, datetime]:
    """``event``'s times moved as ``change`` moves its target; an end not given moves with the
    start."""
    by = change.start - change.target.start if change.start else timedelta()
    end_by = change.end - change.target.end if change.end else by
    return event.start + by, event.end + end_by


async def _zone(session: AsyncSession, tenant_id: UUID) -> ZoneInfo:
    tenant = await get_tenant(session, tenant_id)
    return ZoneInfo((tenant.timezone if tenant else None) or "UTC")


class TenantCalendar:
    def __init__(
        self,
        db: Database,
        tenant_id: UUID,
        *,
        provider: CalendarProvider | None,
        account: CalendarConnection | None,
        live: bool = False,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._provider = provider
        self._account = account
        self.connected = provider is not None and account is not None
        self.live = live

    async def refresh(
        self, start: datetime, end: datetime, *, raise_not_connected: bool = False
    ) -> bool:
        """Copy Google's ``[start, end)`` into the stored events. ``False`` when there is no
        Google, or it failed (the stored copy stays as it was). With ``raise_not_connected``, an
        account that no longer works raises ``CalendarNotConnectedError`` instead (the sync
        disconnects it)."""
        if self._provider is None or self._account is None:
            return False
        try:
            found = await self._provider.list_events(
                self._account.connected_account_id,
                self._account.composio_user_id,
                start=start,
                end=end,
            )
        except CalendarProviderError as exc:
            if raise_not_connected and isinstance(exc, CalendarNotConnectedError):
                raise
            log.warning(
                "calendar.refresh_failed",
                tenant_id=str(self._tenant_id),
                error=type(exc).__name__,
            )
            return False
        if len(found) >= PAGE_SIZE:
            # A full page: the rest of the window was not read, so keep what is stored past it.
            end = found[-1].start
            log.warning(
                "calendar.refresh_truncated", tenant_id=str(self._tenant_id), count=len(found)
            )
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await event_rows.replace_imported(session, self._tenant_id, start, end, found)
        return True

    async def events(self, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        if self.live and self.connected:
            await self.refresh(start, end)
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            zone = await _zone(session, self._tenant_id)
            return await event_rows.events_between(session, self._tenant_id, start, end, zone=zone)

    async def add(self, event: CalendarEvent) -> CalendarEvent:
        """Add ``event``. Provider errors (not added to Google) are re-raised."""
        if self._provider is None or self._account is None:
            async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
                return await event_rows.add_event(session, self._tenant_id, event, source="remi")
        # CalendarProviderUnavailableError / RejectedError propagate: nothing was added.
        stored = await self._provider.create_event(
            self._account.connected_account_id, self._account.composio_user_id, event
        )
        # Keep Google's copy under its own id, so the next refresh replaces it, not doubles it.
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await event_rows.upsert_google(session, self._tenant_id, stored)
        if stored.rule:
            # Google's instances replace the stored master, so the whole series shows now, not
            # at the next hourly sync. A failed refresh leaves the master row; the add stands.
            await self.refresh(stored.start, stored.start + event_rows.IMPORT_AHEAD)
        return stored

    async def change(self, change: EventChange) -> None:
        """Apply ``change`` where its event lives. Raises :class:`EventChangedError` when it is
        no longer there as listed, :class:`CalendarNotConnectedError` for a Google event with
        Google gone, :class:`SeriesSplitError` when a series was ended but its continuation not
        created; other provider errors propagate (nothing was written)."""
        target = change.target
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            row = await event_rows.find_row(session, self._tenant_id, target.external_id)
            if row is None:
                raise EventChangedError
            zone = await _zone(session, self._tenant_id)
            if row.source == "remi":
                await event_rows.change_event(session, self._tenant_id, change, zone)
                return
            stale = row.starts_at != target.start or row.title != target.title
        if self._provider is None or self._account is None:
            raise CalendarNotConnectedError(None)
        if stale:
            raise EventChangedError
        await self._change_google(self._provider, self._account, change, zone)
        # In their zone, as the sync does: all-day events are stored as midnight in the window's.
        # A failed refresh leaves the stored copy behind until the next sync; the write stands.
        start = (target.start - timedelta(days=1)).astimezone(zone)
        await self.refresh(start, (target.start + event_rows.IMPORT_AHEAD).astimezone(zone))

    async def _change_google(
        self,
        provider: CalendarProvider,
        account: CalendarConnection,
        change: EventChange,
        zone: ZoneInfo,
    ) -> None:
        ids = (account.connected_account_id, account.composio_user_id)
        target, which = change.target, change.which
        # In the member's zone: an all-day event is sent by its date there, and a series shifts
        # on their wall clock, which Google keeps.
        event_id = target.external_id
        start, end = (
            change.start and change.start.astimezone(zone),
            change.end and change.end.astimezone(zone),
        )
        if target.series_id and which != "this":
            master, lines = await provider.get_series(*ids, target.series_id, zone)
            if which == "following" and target.start > master.start:
                await self._split_google(provider, ids, change, master, lines, zone)
                return
            event_id = master.external_id
            if start or end:
                start, end = _moved(master, change)
                start, end = start.astimezone(zone), end.astimezone(zone)
        if change.delete:
            await provider.delete_event(*ids, event_id)
            return
        await provider.update_event(
            *ids,
            event_id,
            title=change.title,
            start=start,
            end=end,
            all_day=target.all_day,
            location=change.location,
        )

    async def _split_google(
        self,
        provider: CalendarProvider,
        ids: tuple[str, str],
        change: EventChange,
        master: CalendarEvent,
        lines: list[str],
        zone: ZoneInfo,
    ) -> None:
        """End Google's series just before the target; for a change, start a new one there."""
        target = change.target
        rule = change.rule or local_rule(lines, zone, target.all_day)
        if not change.delete and rule is None:
            # propose_change refuses a pattern we can't copy; never end a series we can't continue.
            raise CalendarProviderRejectedError(None)
        ended = end_lines(lines, target.start.astimezone(zone), target.all_day)
        await provider.update_event(*ids, master.external_id, recurrence=ended)
        if change.delete:
            return
        start, end = _moved(target, change)
        start, end = start.astimezone(zone), end.astimezone(zone)
        if change.rule:
            first = first_at(change.rule, start)
            start, end = first, end + (first - start)
        location = target.location if change.location is None else change.location or None
        title = change.title or target.title
        new = CalendarEvent("", title, start, end, all_day=target.all_day, location=location)
        try:
            await provider.create_event(*ids, replace(new, rule=rule))
        except CalendarProviderError as exc:
            raise SeriesSplitError from exc

    async def series_rule(self, event: CalendarEvent) -> str | None:
        """The pattern of the series ``event`` belongs to, in our rule form; ``None`` when it
        doesn't repeat or the pattern can't be read (Google down, or outside our subset)."""
        if event.rule or not event.series_id:
            return event.rule
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            series = await event_rows.find_row(session, self._tenant_id, event.series_id)
            zone = await _zone(session, self._tenant_id)
        if series is not None and series.source == "remi":
            return series.rule  # a changed date of Remi's own series
        if self._provider is None or self._account is None:
            return None
        try:
            _, lines = await self._provider.get_series(
                self._account.connected_account_id,
                self._account.composio_user_id,
                event.series_id,
                zone,
            )
        except CalendarProviderError as exc:
            log.warning(
                "calendar.series_read_failed",
                tenant_id=str(self._tenant_id),
                error=type(exc).__name__,
            )
            return None
        return local_rule(lines, zone, event.all_day)


class InMemoryTenantCalendar:
    """The same behaviour in a list. ``added``, ``changed`` and ``calls`` (each window read) are
    there to assert on; ``fail`` makes ``add`` and ``change`` raise, like a provider that is
    down."""

    def __init__(
        self,
        events: Sequence[CalendarEvent] = (),
        *,
        connected: bool = False,
        live: bool = False,
    ) -> None:
        self._events = list(events)
        self.connected = connected
        self.live = live
        self.added: list[CalendarEvent] = []
        self.changed: list[EventChange] = []
        self.calls: list[tuple[datetime, datetime]] = []
        self.fail: Exception | None = None

    async def events(self, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        self.calls.append((start, end))
        zone = start.tzinfo or UTC
        found = [
            o
            for e in self._events
            for o in occurrences(e, start, end, zone)
            if o.start < end and o.end > start
        ]
        found = [
            replace(e, start=e.start.astimezone(zone), end=e.end.astimezone(zone)) for e in found
        ]
        return sorted(found, key=lambda e: e.start)

    async def add(self, event: CalendarEvent) -> CalendarEvent:
        if self.fail:
            raise self.fail
        stored = (
            event
            if event.external_id
            else replace(event, external_id=f"memory-{len(self._events) + 1}")
        )
        self._events.append(stored)
        self.added.append(stored)
        return stored

    async def change(self, change: EventChange) -> None:
        if self.fail:
            raise self.fail
        self.changed.append(change)
        target = change.target
        if target.series_id and change.which != "all":
            # shortcut: a series' "this"/"following" is recorded, not applied; apply it if
            # po-chat needs it
            return
        key = target.series_id or target.external_id
        i = next((i for i, e in enumerate(self._events) if e.external_id == key), None)
        if i is None:
            raise EventChangedError
        if change.delete:
            del self._events[i]
            return
        event = self._events[i]
        start, end = _moved(event, change)
        location = event.location if change.location is None else change.location or None
        title = change.title or event.title
        self._events[i] = replace(event, title=title, start=start, end=end, location=location)

    async def series_rule(self, event: CalendarEvent) -> str | None:
        return event.rule


__all__ = ["Calendar", "InMemoryTenantCalendar", "TenantCalendar"]
