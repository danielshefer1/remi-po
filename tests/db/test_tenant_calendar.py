"""``TenantCalendar``: one calendar per member, Google optional (issue #50)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.errors import (
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import events as event_rows
from personal_organizer.db.repositories.connections import active_connection, bind_connection
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.calendar import TenantCalendar
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.connect import TRUNCATE_CONNECT_TABLES, make_active, seed_tenant

pytestmark = [pytest.mark.db]

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
WIDE = (NOW - timedelta(days=1), NOW + timedelta(days=30))


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


def _event(title: str, hours: int = 2, ext: str = "") -> CalendarEvent:
    start = NOW + timedelta(hours=hours)
    return CalendarEvent(ext, title, start, start + timedelta(hours=1))


async def _tenant(db: Database, *, connected: bool) -> tuple[UUID, Any]:
    tenant_id = await seed_tenant(db, phone="+972500000001")
    await make_active(db, tenant_id)
    if not connected:
        return tenant_id, None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await bind_connection(session, tenant_id, connected_account_id="ca_1", auth_config_id="ac")
        return tenant_id, await active_connection(session, tenant_id)


async def _calendar(
    db: Database, google: InMemoryCalendar, *, connected: bool = True, live: bool = False
) -> TenantCalendar:
    tenant_id, account = await _tenant(db, connected=connected)
    return TenantCalendar(db, tenant_id, provider=google, account=account, live=live)


async def test_native_only_when_not_connected(db: Database) -> None:
    google = InMemoryCalendar()
    cal = await _calendar(db, google, connected=False)
    assert not cal.connected
    added = await cal.add(_event("Dentist"))
    assert [e.title for e in await cal.events(*WIDE)] == ["Dentist"]
    assert added.external_id
    assert google.events == []
    assert google.calls == []
    assert await cal.refresh(*WIDE) is False


async def test_default_reads_stored_without_calling_google(db: Database) -> None:
    google = InMemoryCalendar([_event("In Google", ext="g1")])
    cal = await _calendar(db, google)
    assert cal.connected
    assert not cal.live
    assert await cal.refresh(*WIDE) is True
    google.calls.clear()
    assert [e.title for e in await cal.events(*WIDE)] == ["In Google"]
    assert google.calls == []


async def test_live_refresh_drops_deleted_google_event(db: Database) -> None:
    google = InMemoryCalendar([_event("Gone soon", ext="g1"), _event("Stays", 4, "g2")])
    cal = await _calendar(db, google, live=True)
    assert len(await cal.events(*WIDE)) == 2
    google.events.pop(0)
    assert [e.title for e in await cal.events(*WIDE)] == ["Stays"]


async def test_live_google_down_serves_stored(db: Database) -> None:
    google = InMemoryCalendar([_event("Copy", ext="g1")])
    cal = await _calendar(db, google, live=True)
    await cal.events(*WIDE)
    google.fail = CalendarProviderUnavailableError("down")
    assert [e.title for e in await cal.events(*WIDE)] == ["Copy"]
    assert await cal.refresh(*WIDE) is False


async def test_add_without_account_is_native(db: Database) -> None:
    google = InMemoryCalendar()
    tenant_id, _ = await _tenant(db, connected=False)
    cal = TenantCalendar(db, tenant_id, provider=google, account=None)
    await cal.add(_event("Mine"))
    assert google.events == []
    assert [e.title for e in await cal.events(*WIDE)] == ["Mine"]


async def test_add_while_connected_is_not_duplicated_by_refresh(db: Database) -> None:
    google = InMemoryCalendar()
    cal = await _calendar(db, google)
    added = await cal.add(_event("Lunch"))
    assert [e.external_id for e in google.events] == [added.external_id]
    assert [e.title for e in await cal.events(*WIDE)] == ["Lunch"]
    await cal.refresh(*WIDE)
    assert [e.title for e in await cal.events(*WIDE)] == ["Lunch"]


async def test_add_failure_reraises_and_stores_nothing(db: Database) -> None:
    google = InMemoryCalendar()
    cal = await _calendar(db, google)
    for error in (CalendarProviderUnavailableError("x"), CalendarProviderRejectedError(400)):
        google.fail = error
        with pytest.raises(type(error)):
            await cal.add(_event("Nope"))
    assert await cal.events(*WIDE) == []


async def test_full_page_refresh_keeps_rows_past_the_cut(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("personal_organizer.messaging.calendar.PAGE_SIZE", 2)
    google = InMemoryCalendar(
        [_event("A", 1, "g1"), _event("B", 2, "g2"), _event("Late", 200, "g3")]
    )
    cal = await _calendar(db, google)
    # A first page that holds only A and B; Late is past the cut.
    page = InMemoryCalendar(google.events[:2])
    cal._provider = page
    await cal.refresh(*WIDE)
    full = (NOW, NOW + timedelta(days=30))
    async with db.tenant_session(TenantId(cal._tenant_id)) as session:
        await event_rows.upsert_google(session, cal._tenant_id, google.events[2])
    page.events = [_event("A2", 1, "g1"), _event("B", 2, "g2")]
    await cal.refresh(*WIDE)
    assert [e.title for e in await cal.events(*full)] == ["A2", "B", "Late"]


def _weekly(start: datetime) -> CalendarEvent:
    return CalendarEvent("", "Class", start, start + timedelta(hours=1), rule="FREQ=WEEKLY")


async def test_add_recurring_to_google_shows_the_series_at_once(db: Database) -> None:
    google = InMemoryCalendar()
    cal = await _calendar(db, google)
    added = await cal.add(_weekly(NOW + timedelta(hours=2)))
    assert added.rule == "FREQ=WEEKLY"
    later = (NOW + timedelta(days=14), NOW + timedelta(days=22))
    assert [e.title for e in await cal.events(*later)] == ["Class", "Class"]
    assert len(await cal.events(*WIDE)) == 5  # not just the first one


async def test_a_failed_refresh_after_a_recurring_create_still_returns_it(db: Database) -> None:
    class Flaky(InMemoryCalendar):
        async def list_events(self, *args: Any, **kwargs: Any) -> Any:
            raise CalendarProviderUnavailableError("down")

    cal = await _calendar(db, Flaky())
    added = await cal.add(_weekly(NOW + timedelta(hours=2)))
    assert added.external_id
    assert [e.title for e in await cal.events(*WIDE)] == ["Class"]


async def test_native_series_reads_at_the_same_wall_time_in_any_window_zone(db: Database) -> None:
    tenant_id, _ = await _tenant(db, connected=False)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
    cal = TenantCalendar(db, tenant_id, provider=None, account=None)
    jerusalem = ZoneInfo("Asia/Jerusalem")
    first = datetime(2026, 10, 19, 18, tzinfo=jerusalem)  # a Monday; DST ends on the 25th
    await cal.add(_weekly(first))
    for zone in (UTC, ZoneInfo("Europe/London")):
        found = await cal.events(
            datetime(2026, 10, 18, tzinfo=zone), datetime(2026, 11, 3, tzinfo=zone)
        )
        assert [e.start.astimezone(jerusalem).hour for e in found] == [18, 18, 18]
        assert all(e.start.tzinfo is zone for e in found)
