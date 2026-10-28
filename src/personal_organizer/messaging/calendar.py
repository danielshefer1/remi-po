"""One calendar per member (issue #50): Remi's own, with Google's events copied in.

``TenantCalendar`` is the worker's, over Postgres. ``InMemoryTenantCalendar`` is ``po-chat``'s
and the tests' (no database). With Google connected, ``add`` writes to Google first and keeps
the copy it returns; ``events`` reads the stored copy, refreshing it from Google first only
when ``live``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog

from personal_organizer.core.errors import CalendarNotConnectedError, CalendarProviderError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.db.repositories import events as event_rows
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.calendar import CalendarEvent, CalendarProvider
from personal_organizer.messaging.recurrence import occurrences
from personal_organizer.providers.calendar.composio import PAGE_SIZE

log = structlog.get_logger(__name__)


class Calendar(Protocol):
    connected: bool
    live: bool

    async def events(self, start: datetime, end: datetime) -> Sequence[CalendarEvent]: ...
    async def add(self, event: CalendarEvent) -> CalendarEvent: ...


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
            tenant = await get_tenant(session, self._tenant_id)
            zone = ZoneInfo((tenant.timezone if tenant else None) or "UTC")
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


class InMemoryTenantCalendar:
    """The same behaviour in a list. ``added`` and ``calls`` (each window read) are there to
    assert on; ``fail`` makes ``add`` raise, like a provider that is down."""

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


__all__ = ["Calendar", "InMemoryTenantCalendar", "TenantCalendar"]
