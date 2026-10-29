"""Remi's own events and the sync bookkeeping (issue #50), against Postgres as ``app_user``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Event
from personal_organizer.db.repositories import events
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.tenants import get_tenant, set_live_calendar
from personal_organizer.interfaces.calendar import CalendarEvent
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


def _event(title: str, start: datetime, *, hours: int = 1, ext: str = "") -> CalendarEvent:
    return CalendarEvent(
        external_id=ext, title=title, start=start, end=start + timedelta(hours=hours)
    )


async def _tenant(db: Database, phone: str = "+972500000001") -> UUID:
    tenant_id = await seed_tenant(db, phone=phone)
    await make_active(db, tenant_id)
    return tenant_id


async def test_add_and_read_native(db: Database) -> None:
    tenant_id = await _tenant(db)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        added = await events.add_event(
            session, tenant_id, _event("Dentist", NOW + timedelta(hours=2)), source="remi"
        )
        assert added.external_id
        got = await events.events_between(session, tenant_id, *WIDE)
    assert [(e.title, e.external_id) for e in got] == [("Dentist", added.external_id)]


async def test_replace_imported_keeps_native(db: Database) -> None:
    tenant_id = await _tenant(db)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.add_event(session, tenant_id, _event("Mine", NOW), source="remi")
        await events.add_event(
            session, tenant_id, _event("Old", NOW), source="google", external_id="g1"
        )
        await events.replace_imported(
            session, tenant_id, *WIDE, [_event("New", NOW + timedelta(hours=3), ext="g2")]
        )
        got = await events.events_between(session, tenant_id, *WIDE)
    assert sorted(e.title for e in got) == ["Mine", "New"]


async def test_replace_imported_upserts_same_id(db: Database) -> None:
    tenant_id = await _tenant(db)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        # Outside the window, so the delete leaves it and the insert must upsert.
        far = NOW + timedelta(days=60)
        await events.add_event(
            session, tenant_id, _event("Before", far), source="google", external_id="g1"
        )
        await events.replace_imported(
            session, tenant_id, *WIDE, [_event("After", NOW + timedelta(hours=1), ext="g1")]
        )
        got = await events.events_between(
            session, tenant_id, NOW - timedelta(days=1), far + timedelta(days=1)
        )
    assert [(e.title, e.external_id) for e in got] == [("After", "g1")]


async def test_events_between_overlap_edges(db: Database) -> None:
    tenant_id = await _tenant(db)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.add_event(
            session, tenant_id, _event("Ends at start", NOW - timedelta(hours=1)), source="remi"
        )
        await events.add_event(
            session, tenant_id, _event("Starts at end", NOW + timedelta(hours=1)), source="remi"
        )
        await events.add_event(
            session, tenant_id, _event("Straddles", NOW - timedelta(minutes=30)), source="remi"
        )
        got = await events.events_between(session, tenant_id, NOW, NOW + timedelta(hours=1))
    assert [e.title for e in got] == ["Straddles"]


async def test_due_syncs_returns_ids_only(db: Database) -> None:
    tenant_id = await _tenant(db)
    no_connection = await _tenant(db, "+972500000002")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await bind_connection(session, tenant_id, connected_account_id="ca_1", auth_config_id="ac")

    async def due() -> list[UUID]:
        async with db.system_session() as session:
            return await events.due_syncs(session, NOW)

    assert await due() == [tenant_id]  # never synced
    assert no_connection not in await due()
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.mark_synced(session, tenant_id, NOW - timedelta(minutes=30))
    assert await due() == []
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.mark_synced(session, tenant_id, NOW - timedelta(minutes=61))
    assert await due() == [tenant_id]


async def test_live_calendar_defaults_off(db: Database) -> None:
    tenant_id = await _tenant(db)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        assert tenant is not None
        assert tenant.live_calendar is False
        await set_live_calendar(session, tenant_id, True)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        assert tenant is not None
        assert tenant.live_calendar is True


async def test_empty_external_id_means_no_id(db: Database) -> None:
    tenant_id = await _tenant(db)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        a = await events.add_event(
            session, tenant_id, _event("A", NOW), source="remi", external_id=""
        )
        b = await events.add_event(
            session, tenant_id, _event("B", NOW), source="remi", external_id=""
        )
    assert a.external_id
    assert b.external_id
    assert a.external_id != b.external_id


async def test_replace_imported_sets_source_from_incoming(db: Database) -> None:
    tenant_id = await _tenant(db)
    far = NOW + timedelta(days=60)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.add_event(
            session, tenant_id, _event("X", far), source="remi", external_id="g1"
        )
        await events.replace_imported(session, tenant_id, *WIDE, [_event("X", far, ext="g1")])
        # It is a google copy now, so the next replace over its span drops it.
        await events.replace_imported(
            session, tenant_id, far - timedelta(days=1), far + timedelta(days=1), []
        )
        got = await events.events_between(session, tenant_id, NOW, far + timedelta(days=1))
    assert got == []


async def test_events_between_reads_in_callers_zone(db: Database) -> None:
    tenant_id = await _tenant(db)
    jerusalem = ZoneInfo("Asia/Jerusalem")
    day = datetime(2026, 11, 2, tzinfo=jerusalem)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.add_event(
            session,
            tenant_id,
            CalendarEvent("", "Holiday", day, day + timedelta(days=1), all_day=True),
            source="remi",
        )
        got = await events.events_between(
            session, tenant_id, day - timedelta(days=1), day + timedelta(days=2)
        )
    assert got[0].start.date() == day.date()
    assert got[0].start == day


JLM = ZoneInfo("Asia/Jerusalem")
CLASS_START = datetime(2026, 10, 19, 18, tzinfo=JLM)


async def _series(db: Database, tenant_id: UUID, rule: str) -> CalendarEvent:
    event = CalendarEvent("", "Class", CLASS_START, CLASS_START + timedelta(hours=1), rule=rule)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await events.add_event(session, tenant_id, event, source="remi")


async def _read(
    db: Database, tenant_id: UUID, start: datetime, end: datetime
) -> list[CalendarEvent]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await events.events_between(session, tenant_id, start, end, zone=JLM)


async def test_native_series_expands(db: Database) -> None:
    tenant_id = await _tenant(db)
    series = await _series(db, tenant_id, "FREQ=WEEKLY;BYDAY=MO")
    assert series.rule == "FREQ=WEEKLY;BYDAY=MO"
    got = await _read(
        db, tenant_id, CLASS_START - timedelta(days=1), CLASS_START + timedelta(days=15)
    )
    assert [e.start.day for e in got] == [19, 26, 2]
    assert got[0].external_id == f"{series.external_id}:20261019T1800"
    assert all(e.rule == series.rule and e.end - e.start == timedelta(hours=1) for e in got)


async def test_native_series_keeps_wall_time_across_dst(db: Database) -> None:
    tenant_id = await _tenant(db)
    await _series(db, tenant_id, "FREQ=WEEKLY;BYDAY=MO")
    got = await _read(
        db, tenant_id, CLASS_START - timedelta(days=1), CLASS_START + timedelta(days=15)
    )
    assert {e.start.astimezone(JLM).hour for e in got} == {18}  # change was 2026-10-25


async def test_open_series_expands_only_window(db: Database) -> None:
    tenant_id = await _tenant(db)
    await _series(db, tenant_id, "FREQ=DAILY")
    start = datetime(2027, 3, 1, tzinfo=JLM)
    got = await _read(db, tenant_id, start, start + timedelta(days=3))
    assert [e.start.day for e in got] == [1, 2, 3]


async def test_series_until_respected(db: Database) -> None:
    tenant_id = await _tenant(db)
    await _series(db, tenant_id, "FREQ=DAILY;UNTIL=20261021T180000")
    got = await _read(db, tenant_id, CLASS_START, CLASS_START + timedelta(days=30))
    assert [e.start.day for e in got] == [19, 20, 21]


async def test_google_copy_keeps_series_and_invited(db: Database) -> None:
    tenant_id = await _tenant(db)
    inst = CalendarEvent(
        "g1_20261103", "Sync", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1),
        series_id="g1", invited=True,
    )  # fmt: skip
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await events.replace_imported(session, tenant_id, *WIDE, [inst])
        got = await events.events_between(session, tenant_id, *WIDE)
    assert [(e.series_id, e.invited) for e in got] == [("g1", True)]


async def test_skipped_occurrence_not_read(db: Database) -> None:
    tenant_id = await _tenant(db)
    series = await _series(db, tenant_id, "FREQ=WEEKLY;BYDAY=MO")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await session.execute(
            update(Event)
            .where(Event.external_id == series.external_id)
            .values(skipped=[CLASS_START + timedelta(days=7)])
        )
    got = await _read(
        db, tenant_id, CLASS_START - timedelta(days=1), CLASS_START + timedelta(days=15)
    )
    assert [e.start.day for e in got] == [19, 2]
