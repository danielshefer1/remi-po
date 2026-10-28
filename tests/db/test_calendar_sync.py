"""Importing Google on connect and syncing it hourly (issue #50)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderUnavailableError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import events as event_rows
from personal_organizer.db.repositories.connections import active_connection, bind_connection
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import ACCOUNT_ACTIVE, CalendarEvent, ConnectedAccount
from personal_organizer.messaging.calendar_sync import sync_calendar
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.connect import TRUNCATE_CONNECT_TABLES, make_active, seed_tenant

pytestmark = [pytest.mark.db]

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
WIDE = (NOW - timedelta(days=2), NOW + timedelta(days=120))


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


def _event(title: str, days: int, ext: str) -> CalendarEvent:
    start = NOW + timedelta(days=days)
    return CalendarEvent(ext, title, start, start + timedelta(hours=1))


async def _tenant(db: Database, zone: str = "UTC") -> UUID:
    tenant_id = await seed_tenant(db, phone="+972500000001")
    await make_active(db, tenant_id)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, zone)
        await bind_connection(session, tenant_id, connected_account_id="ca_1", auth_config_id="ac")
    return tenant_id


async def _stored(db: Database, tenant_id: UUID) -> list[CalendarEvent]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await event_rows.events_between(session, tenant_id, *WIDE)


async def _due(db: Database, now: datetime) -> list[UUID]:
    async with db.system_session() as session:
        return await event_rows.due_syncs(session, now)


async def test_connect_imports_next_90_days(db: Database) -> None:
    tenant_id = await _tenant(db)
    google = InMemoryCalendar(
        [_event("Soon", 3, "g1"), _event("Far", 80, "g2"), _event("Too far", 100, "g3")]
    )
    assert await sync_calendar(tenant_id, db=db, provider=google, now=NOW)
    assert [e.title for e in await _stored(db, tenant_id)] == ["Soon", "Far"]
    assert await _due(db, NOW) == []  # marked synced


async def test_sync_drops_deleted_google_event(db: Database) -> None:
    tenant_id = await _tenant(db)
    google = InMemoryCalendar([_event("Gone", 3, "g1"), _event("Stays", 4, "g2")])
    await sync_calendar(tenant_id, db=db, provider=google, now=NOW)
    google.events = google.events[1:]
    await sync_calendar(tenant_id, db=db, provider=google, now=NOW)
    assert [e.title for e in await _stored(db, tenant_id)] == ["Stays"]


async def test_sync_failure_keeps_rows(db: Database) -> None:
    tenant_id = await _tenant(db)
    google = InMemoryCalendar([_event("Kept", 3, "g1")])
    await sync_calendar(tenant_id, db=db, provider=google, now=NOW)
    google.fail = CalendarProviderUnavailableError("down")
    later = NOW + timedelta(hours=2)
    assert not await sync_calendar(tenant_id, db=db, provider=google, now=later)
    assert [e.title for e in await _stored(db, tenant_id)] == ["Kept"]
    # synced_at means "last tried": not retried every minute, but again in an hour.
    assert await _due(db, later + timedelta(minutes=1)) == []
    assert await _due(db, later + timedelta(hours=1, minutes=1)) == [tenant_id]


async def test_sync_unexpected_error_still_stamps(db: Database) -> None:
    """A non-provider error (a DB error, a repeated external_id) surfaces, but is not retried
    every tick."""
    tenant_id = await _tenant(db)
    google = InMemoryCalendar()
    google.fail = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await sync_calendar(tenant_id, db=db, provider=google, now=NOW)
    assert await _due(db, NOW + timedelta(minutes=1)) == []


async def test_new_account_import_replaces_imported_rows(db: Database) -> None:
    tenant_id = await _tenant(db)
    old = InMemoryCalendar([_event("Old account", 3, "g1")])
    await sync_calendar(tenant_id, db=db, provider=old, now=NOW)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await event_rows.add_event(session, tenant_id, _event("Mine", 5, ""), source="remi")
        await bind_connection(session, tenant_id, connected_account_id="ca_2", auth_config_id="ac")
    new = InMemoryCalendar([_event("New account", 4, "g9")])
    await sync_calendar(tenant_id, db=db, provider=new, now=NOW)
    assert [e.title for e in await _stored(db, tenant_id)] == ["New account", "Mine"]


async def test_sync_window_is_in_the_members_zone(db: Database) -> None:
    """An all-day event is stored as midnight in the window's zone, so a UTC window would
    move it by a day in Jerusalem."""
    jerusalem = ZoneInfo("Asia/Jerusalem")
    tenant_id = await _tenant(db, "Asia/Jerusalem")
    day = datetime(2026, 11, 2, tzinfo=jerusalem)
    google = InMemoryCalendar(
        [CalendarEvent("g1", "Holiday", day, day + timedelta(days=1), all_day=True)]
    )
    await sync_calendar(tenant_id, db=db, provider=google, now=NOW)
    [(_, _, start, end)] = google.calls
    assert start.tzinfo == end.tzinfo == jerusalem
    async with db.tenant_session(TenantId(tenant_id)) as session:
        [got] = await event_rows.events_between(session, tenant_id, day, day + timedelta(days=1))
    assert (got.title, got.start) == ("Holiday", day)


class _Google(InMemoryCalendar):
    """A calendar that is also the linker: ``status`` is what Composio says of the account."""

    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status
        self.fail = CalendarNotConnectedError(401)

    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        return callback_url

    async def get_account(self, connected_account_id: str) -> ConnectedAccount:
        return ConnectedAccount(connected_account_id, "u", "ac", self.status)


async def test_sync_revoked_account_disconnects(db: Database) -> None:
    tenant_id = await _tenant(db)
    await sync_calendar(
        tenant_id, db=db, provider=InMemoryCalendar([_event("Copied", 3, "g1")]), now=NOW
    )
    google = _Google("EXPIRED")
    assert not await sync_calendar(tenant_id, db=db, provider=google, now=NOW + timedelta(hours=2))
    async with db.tenant_session(TenantId(tenant_id)) as session:
        assert await active_connection(session, tenant_id) is None
    assert [e.title for e in await _stored(db, tenant_id)] == ["Copied"]
    assert await _due(db, NOW + timedelta(days=1)) == []  # nothing to sync any more


async def test_sync_not_connected_but_account_active_is_not_revoked(db: Database) -> None:
    tenant_id = await _tenant(db)
    assert not await sync_calendar(tenant_id, db=db, provider=_Google(ACCOUNT_ACTIVE), now=NOW)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        assert await active_connection(session, tenant_id) is not None
    assert await _due(db, NOW + timedelta(minutes=1)) == []  # tried; not again for an hour


async def test_sync_does_not_revoke_a_connection_bound_meanwhile(db: Database) -> None:
    tenant_id = await _tenant(db)

    class Reconnects(_Google):
        async def list_events(self, *args: Any, **kwargs: Any) -> Any:
            async with db.tenant_session(TenantId(tenant_id)) as session:
                await bind_connection(
                    session, tenant_id, connected_account_id="ca_2", auth_config_id="ac"
                )
            return await super().list_events(*args, **kwargs)

    await sync_calendar(tenant_id, db=db, provider=Reconnects("EXPIRED"), now=NOW)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        new = await active_connection(session, tenant_id)
    assert new is not None
    assert new.connected_account_id == "ca_2"
