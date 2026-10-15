"""``InMemorySchedule``: the ``Schedule`` that ``po-chat`` and the agent-tool tests use."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from personal_organizer.messaging.schedule import InMemorySchedule, Schedule

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


async def test_upcoming_lists_pending_future_reminders_soonest_first() -> None:
    schedule = InMemorySchedule(NOW)
    await schedule.add("later", NOW + timedelta(hours=2))
    await schedule.add("sooner", NOW + timedelta(hours=1))
    await schedule.add("past", NOW - timedelta(hours=1))
    assert [r.text for r in await schedule.upcoming()] == ["sooner", "later"]
    assert all(r.status == "pending" for r in schedule.reminders)


async def test_cancel_once() -> None:
    schedule = InMemorySchedule(NOW)
    await schedule.add("call mom", NOW + timedelta(hours=1))
    (reminder,) = await schedule.upcoming()
    assert await schedule.cancel(reminder.id) is True
    assert await schedule.cancel(reminder.id) is False
    assert await schedule.upcoming() == []
    assert reminder.status == "cancelled"


async def test_set_briefing() -> None:
    schedule: Schedule = InMemorySchedule(NOW)
    assert isinstance(schedule, InMemorySchedule)
    assert schedule.briefing_at == time(7, 30)
    await schedule.set_briefing(time(8, 0))
    assert schedule.briefing_at == time(8, 0)
    await schedule.set_briefing(None)
    assert schedule.briefing_at is None
