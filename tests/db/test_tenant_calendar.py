"""``TenantCalendar``: one calendar per member, Google optional (issue #50)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
    EventChangedError,
    SeriesSplitError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import events as event_rows
from personal_organizer.db.repositories.connections import active_connection, bind_connection
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent, EventChange
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


JLM = ZoneInfo("Asia/Jerusalem")
CLASS_AT = datetime(2026, 10, 28, 18, tzinfo=JLM)  # a Wednesday, weekly


async def _google_series(
    db: Database, *, rule: str = "FREQ=WEEKLY"
) -> tuple[TenantCalendar, InMemoryCalendar, list[CalendarEvent]]:
    """A connected Jerusalem member with Google's weekly "Class" ("ser") copied in."""
    google = InMemoryCalendar(
        [CalendarEvent("ser", "Class", CLASS_AT, CLASS_AT + timedelta(hours=1), rule=rule)]
    )
    cal = await _calendar(db, google)
    async with db.tenant_session(TenantId(cal._tenant_id)) as session:
        await set_timezone(session, cal._tenant_id, "Asia/Jerusalem")
    await cal.refresh(*WIDE)
    return cal, google, list(await cal.events(*WIDE))


async def test_change_native_routes_to_rows(db: Database) -> None:
    google = InMemoryCalendar()
    cal = await _calendar(db, google, connected=False)
    await cal.add(_event("Dentist"))
    [dentist] = await cal.events(*WIDE)
    await cal.change(EventChange(dentist, title="Doctor"))
    assert [e.title for e in await cal.events(*WIDE)] == ["Doctor"]
    assert google.writes == []


async def test_google_this_patches_instance(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    target = listed[1]
    await cal.change(EventChange(target, which="this", title="Moved"))
    await cal.change(EventChange(listed[2], which="this", delete=True))
    assert [(op, event_id) for op, event_id, _ in google.writes] == [
        ("update", target.external_id),
        ("delete", listed[2].external_id),
    ]
    assert google.writes[0][2]["title"] == "Moved"
    assert google.writes[0][2]["start"] is None


async def test_google_all_patches_master(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    target = listed[2]  # the master is two weeks earlier: it moves by the same hour
    await cal.change(EventChange(target, which="all", start=target.start + timedelta(hours=1)))
    [(op, event_id, args)] = google.writes
    assert (op, event_id) == ("update", "ser")
    assert (args["start"], args["end"]) == (
        CLASS_AT + timedelta(hours=1),
        CLASS_AT + timedelta(hours=2),
    )
    assert args["start"].tzinfo is JLM  # so Google keeps the series in the member's zone
    # The copy is refreshed from the day before the changed date; the sync gets the rest.
    later = await cal.events(listed[2].start - timedelta(days=1), WIDE[1])
    assert [e.start.astimezone(JLM).hour for e in later] == [19, 19, 19]


async def test_google_all_delete_deletes_master(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    await cal.change(EventChange(listed[1], which="all", delete=True))
    assert [(op, event_id) for op, event_id, _ in google.writes] == [("delete", "ser")]
    # The copy is refreshed from the day before the changed date; the sync gets the rest.
    assert await cal.events(*WIDE) == listed[:1]


async def test_google_following_ends_and_creates(db: Database) -> None:
    cal, google, listed = await _google_series(db, rule="FREQ=WEEKLY;UNTIL=20261231T235959")
    target = listed[2]
    await cal.change(EventChange(target, which="following", title="New class"))
    [(op, event_id, args)] = google.writes
    assert (op, event_id) == ("update", "ser")
    # One second before 18:00 on Nov 11 in Jerusalem (+02:00).
    assert args["recurrence"] == ["RRULE:FREQ=WEEKLY;UNTIL=20261111T155959Z"]
    created = google.events[-1]
    assert (created.title, created.start) == ("New class", target.start)
    assert created.rule == "FREQ=WEEKLY;UNTIL=20261231T235959"
    assert [e.title for e in await cal.events(*WIDE)] == ["Class"] * 2 + ["New class"] * 3


async def test_google_following_new_pattern_starts_on_its_first_day(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    await cal.change(EventChange(listed[2], which="following", rule="FREQ=WEEKLY;BYDAY=TH"))
    created = google.events[-1]
    assert created.start == listed[2].start + timedelta(days=1)
    assert created.end - created.start == timedelta(hours=1)


async def test_google_following_from_the_first_is_all(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    await cal.change(EventChange(listed[0], which="following", delete=True))
    assert [(op, event_id) for op, event_id, _ in google.writes] == [("delete", "ser")]


async def test_google_following_delete_only_ends(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    await cal.change(EventChange(listed[2], which="following", delete=True))
    assert [op for op, _, _ in google.writes] == ["update"]
    assert len(google.events) == 1
    assert [e.start for e in await cal.events(*WIDE)] == [e.start for e in listed[:2]]


async def test_google_following_create_fails_is_split(db: Database) -> None:
    cal, google, listed = await _google_series(db)

    async def down(*args: Any) -> CalendarEvent:
        raise CalendarProviderUnavailableError("down")

    google.create_event = down  # type: ignore[method-assign]
    with pytest.raises(SeriesSplitError):
        await cal.change(EventChange(listed[2], which="following", title="New class"))
    assert [op for op, _, _ in google.writes] == ["update"]


async def test_google_stale_raises(db: Database) -> None:
    cal, google, listed = await _google_series(db)
    for target in (
        replace(listed[1], title="Renamed"),
        replace(listed[1], start=listed[1].start + timedelta(minutes=30)),
        replace(listed[1], external_id="gone"),
    ):
        with pytest.raises(EventChangedError):
            await cal.change(EventChange(target, delete=True))
    assert google.writes == []


async def test_google_without_connection_raises_not_connected(db: Database) -> None:
    cal, _, listed = await _google_series(db)
    offline = TenantCalendar(db, cal._tenant_id, provider=None, account=None)
    with pytest.raises(CalendarNotConnectedError):
        await offline.change(EventChange(listed[0], delete=True))
    assert len(await offline.events(*WIDE)) == len(listed)  # the copy is left alone


async def test_change_refreshes_copy(db: Database) -> None:
    google = InMemoryCalendar([_event("Lunch", ext="g1")])
    cal = await _calendar(db, google)
    await cal.refresh(*WIDE)
    [lunch] = await cal.events(*WIDE)
    await cal.change(EventChange(lunch, title="Brunch"))
    assert [e.title for e in await cal.events(*WIDE)] == ["Brunch"]


async def test_a_failed_refresh_after_a_change_keeps_it(db: Database) -> None:
    google = InMemoryCalendar([_event("Lunch", ext="g1")])
    cal = await _calendar(db, google)
    await cal.refresh(*WIDE)
    [lunch] = await cal.events(*WIDE)

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise CalendarProviderUnavailableError("down")

    google.list_events = down  # type: ignore[method-assign]
    await cal.change(EventChange(lunch, delete=True))
    assert google.events == []


async def test_series_rule(db: Database) -> None:
    cal, google, listed = await _google_series(db, rule="FREQ=WEEKLY;UNTIL=20261231T235959")
    assert await cal.series_rule(listed[0]) == "FREQ=WEEKLY;UNTIL=20261231T235959"
    single = _event("Once", ext="g9")
    assert await cal.series_rule(single) is None
    native = _weekly(NOW)
    assert await cal.series_rule(native) == "FREQ=WEEKLY"
    google.fail = CalendarProviderUnavailableError("down")
    assert await cal.series_rule(listed[0]) is None


async def test_google_all_day_dates_are_the_members(db: Database) -> None:
    google = InMemoryCalendar(
        [
            CalendarEvent(
                "fast",
                "Fast",
                datetime(2026, 10, 29, tzinfo=JLM),
                datetime(2026, 10, 30, tzinfo=JLM),
                all_day=True,
                rule="FREQ=WEEKLY",
            )
        ]
    )
    cal = await _calendar(db, google)
    async with db.tenant_session(TenantId(cal._tenant_id)) as session:
        await set_timezone(session, cal._tenant_id, "Asia/Jerusalem")
    await cal.refresh(*WIDE)
    listed = await cal.events(*WIDE)  # read in UTC: Nov 5 is 22:00 on the 4th
    await cal.change(EventChange(listed[1], which="following", delete=True))
    await cal.change(EventChange(listed[0], which="this", start=listed[0].start, end=listed[0].end))
    [(_, _, ended), (_, _, moved)] = google.writes
    assert ended["recurrence"] == ["RRULE:FREQ=WEEKLY;UNTIL=20261104"]
    assert (moved["start"].date().isoformat(), moved["all_day"]) == ("2026-10-29", True)
