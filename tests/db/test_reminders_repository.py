"""Reminders and the briefing setting (Iteration 05), against Postgres as ``app_user``.

What the tick relies on: the two definer scans return exactly what is due -- in the tenant's
own zone and local day, inside the 2-hour briefing window, never a stale or cancelled
reminder, never an inactive tenant -- and the repository moves a reminder out of ``pending``
once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import messages, reminders
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.settings import Settings
from tests.fixtures.connect import TRUNCATE_CONNECT_TABLES, make_active, seed_tenant

pytestmark = [pytest.mark.db]

NOW = datetime(2026, 10, 15, 4, 30, tzinfo=UTC)  # 07:30 in Jerusalem (UTC+3 in October)
TODAY = date(2026, 10, 15)


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


async def _tenant(
    db: Database, phone: str, *, zone: str | None = "Asia/Jerusalem", active: bool = True
) -> UUID:
    tenant_id = await seed_tenant(db, phone=phone)
    if active:
        await make_active(db, tenant_id)
    if zone is not None:
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await set_timezone(session, tenant_id, zone)
    return tenant_id


async def _add(db: Database, tenant_id: UUID, due_at: datetime, text: str = "call mom") -> UUID:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await reminders.add_reminder(session, tenant_id, text=text, due_at=due_at)


async def _due_reminders(db: Database, now: datetime = NOW) -> list[tuple[UUID, UUID]]:
    async with db.system_session() as session:
        return await reminders.due_reminders(session, now)


async def _due_briefings(db: Database, now: datetime = NOW) -> list[tuple[UUID, date]]:
    async with db.system_session() as session:
        return await reminders.due_briefings(session, now)


async def _set_briefing(db: Database, tenant_id: UUID, at: time | None) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await reminders.set_briefing(session, tenant_id, at)


async def test_due_reminders_returns_due_pending_reminders_of_active_tenants(
    db: Database,
) -> None:
    active = await _tenant(db, "+972500000001")
    onboarding = await _tenant(db, "+972500000002", active=False)
    due = await _add(db, active, NOW - timedelta(minutes=1))
    await _add(db, active, NOW + timedelta(minutes=1))
    cancelled = await _add(db, active, NOW - timedelta(minutes=1), "cancelled")
    async with db.tenant_session(TenantId(active)) as session:
        assert await reminders.cancel_reminder(session, active, cancelled)
    await _add(db, active, NOW - timedelta(hours=13))
    await _add(db, onboarding, NOW - timedelta(minutes=1))

    assert await _due_reminders(db) == [(due, active)]


async def test_due_briefings_uses_the_local_day_and_window(db: Database) -> None:
    a = await _tenant(db, "+972500000001")
    b = await _tenant(db, "+972500000002")
    async with db.tenant_session(TenantId(b)) as session:
        await reminders.mark_briefed(session, b, TODAY)
    c = await _tenant(db, "+972500000003")
    await _set_briefing(db, c, None)
    await _tenant(db, "+972500000004", zone="Europe/London")  # 05:30 there

    assert await _due_briefings(db) == [(a, TODAY)]


async def test_the_local_day_is_not_the_utc_day(db: Database) -> None:
    """07:30 in Auckland (UTC+13) is 18:30 UTC the day before."""
    tenant_id = await _tenant(db, "+972500000001", zone="Pacific/Auckland")
    assert await _due_briefings(db, datetime(2026, 10, 14, 18, 30, tzinfo=UTC)) == [
        (tenant_id, TODAY)
    ]


async def test_due_briefings_window_closes_after_two_hours(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    assert await _due_briefings(db, NOW + timedelta(hours=1, minutes=59)) == [(tenant_id, TODAY)]
    assert await _due_briefings(db, NOW + timedelta(hours=2, minutes=1)) == []
    assert await _due_briefings(db, NOW - timedelta(minutes=1)) == []


async def test_a_late_evening_briefing_does_not_wrap(db: Database) -> None:
    """22:00 + 2 hours is 00:00 in ``time`` arithmetic: 23:30 would look past the window."""
    tenant_id = await _tenant(db, "+972500000001")
    await _set_briefing(db, tenant_id, time(22, 0))
    late = datetime(2026, 10, 15, 20, 30, tzinfo=UTC)  # 23:30 in Jerusalem
    assert await _due_briefings(db, late) == [(tenant_id, TODAY)]


async def test_add_list_cancel_finish(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    later = await _add(db, tenant_id, NOW + timedelta(hours=2), "later")
    sooner = await _add(db, tenant_id, NOW + timedelta(hours=1), "sooner")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        upcoming = await reminders.upcoming_reminders(session, tenant_id, after=NOW)
        assert [(r.id, r.text) for r in upcoming] == [(sooner, "sooner"), (later, "later")]
        before = await reminders.upcoming_reminders(
            session, tenant_id, after=NOW, before=NOW + timedelta(hours=1, minutes=30)
        )
        assert [r.id for r in before] == [sooner]

        assert await reminders.cancel_reminder(session, tenant_id, later) is True
        assert await reminders.cancel_reminder(session, tenant_id, later) is False

        assert await reminders.finish_reminder(session, tenant_id, sooner, status="sent")
        assert not await reminders.finish_reminder(session, tenant_id, sooner, status="sent")
        assert await reminders.upcoming_reminders(session, tenant_id, after=NOW) == []
        reminder = await reminders.get_reminder(session, tenant_id, sooner)
        assert reminder is not None
        assert reminder.status == "sent"
        assert await reminders.cancel_reminder(session, tenant_id, sooner) is False


async def test_mark_briefed_marks_a_day_once(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        assert await reminders.mark_briefed(session, tenant_id, TODAY) is True
        assert await reminders.mark_briefed(session, tenant_id, TODAY) is False
        assert await reminders.mark_briefed(session, tenant_id, TODAY + timedelta(days=1)) is True


async def test_adding_the_same_pending_reminder_again_is_a_no_op(db: Database) -> None:
    """A turn retried after its set_reminder must not set it twice."""
    tenant_id = await _tenant(db, "+972500000001")
    first = await _add(db, tenant_id, NOW + timedelta(hours=1), "call mom")
    again = await _add(db, tenant_id, NOW + timedelta(hours=1), "call mom")
    other = await _add(db, tenant_id, NOW + timedelta(hours=2), "call mom")
    assert again == first
    assert other != first


async def test_record_outbound_without_an_inbox_row(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await messages.record_outbound(
            session, tenant_id, inbox_id=None, channel="gowa", body="Reminder: call mom"
        )
    async with db.tenant_session(TenantId(tenant_id)) as session:
        rows = await messages.recent_messages(session, tenant_id, limit=5)
    assert [(row.direction, row.inbox_id, row.body) for row in rows] == [
        ("out", None, "Reminder: call mom")
    ]


JERUSALEM = ZoneInfo("Asia/Jerusalem")
DAILY = "FREQ=DAILY"


async def _add_rule(db: Database, tenant_id: UUID, due_at: datetime, rule: str | None) -> UUID:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await reminders.add_reminder(
            session, tenant_id, text="trash", due_at=due_at, rule=rule
        )


async def _next(db: Database, tenant_id: UUID, reminder_id: UUID, after: datetime) -> UUID | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        reminder = await reminders.get_reminder(session, tenant_id, reminder_id)
        assert reminder is not None
        return await reminders.schedule_next(session, tenant_id, reminder, JERUSALEM, after=after)


async def test_schedule_next_inserts_following_occurrence(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    due = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)  # 08:00 in Jerusalem
    first = await _add_rule(db, tenant_id, due, DAILY)
    next_id = await _next(db, tenant_id, first, due)
    assert next_id is not None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        nxt = await reminders.get_reminder(session, tenant_id, next_id)
    assert nxt is not None
    assert (nxt.text, nxt.rule, nxt.status) == ("trash", DAILY, "pending")
    assert nxt.series_id == first
    assert nxt.due_at == due + timedelta(days=1)


async def test_schedule_next_keeps_the_time_of_day_after_downtime(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    due = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)  # 08:00 in Jerusalem
    first = await _add_rule(db, tenant_id, due, DAILY)
    restart = datetime(2026, 10, 18, 11, 37, tzinfo=UTC)  # 14:37 in Jerusalem
    next_id = await _next(db, tenant_id, first, restart)
    assert next_id is not None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        nxt = await reminders.get_reminder(session, tenant_id, next_id)
    assert nxt is not None
    assert nxt.due_at == datetime(2026, 10, 19, 5, 0, tzinfo=UTC)


async def test_schedule_next_stops_at_until(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    due = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)
    first = await _add_rule(db, tenant_id, due, "FREQ=DAILY;UNTIL=20261015T235959")
    assert await _next(db, tenant_id, first, due) is None


async def test_one_off_has_no_next(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    due = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)
    one_off = await _add_rule(db, tenant_id, due, None)
    assert await _next(db, tenant_id, one_off, due) is None


async def test_recent_sent_is_count_based_not_age_based(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    old = await _add(db, tenant_id, NOW - timedelta(days=10), "old")
    newer = await _add(db, tenant_id, NOW - timedelta(days=2), "newer")
    pending = await _add(db, tenant_id, NOW + timedelta(days=1), "pending")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        for rid in (old, newer):
            assert await reminders.finish_reminder(session, tenant_id, rid, status="sent")
        sent = await reminders.recent_sent(session, tenant_id)
        assert [r.id for r in sent] == [newer, old]
        assert pending not in [r.id for r in sent]
        assert [r.id for r in await reminders.recent_sent(session, tenant_id, limit=1)] == [newer]


async def test_due_reminders_keeps_stale_repeating_rows(db: Database) -> None:
    tenant_id = await _tenant(db, "+972500000001")
    stale = NOW - timedelta(hours=20)
    repeating = await _add_rule(db, tenant_id, stale, DAILY)
    await _add_rule(db, tenant_id, stale, None)
    assert await _due_reminders(db) == [(repeating, tenant_id)]
