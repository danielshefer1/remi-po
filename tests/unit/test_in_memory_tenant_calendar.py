from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.interfaces.calendar import CalendarEvent, EventChange
from personal_organizer.messaging.calendar import InMemoryTenantCalendar

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
EVENT = CalendarEvent("", "Dentist", NOW, NOW + timedelta(hours=1))


async def test_add_records_and_reads_back() -> None:
    cal = InMemoryTenantCalendar()
    added = await cal.add(EVENT)
    assert added.external_id
    assert cal.added == [added]
    assert await cal.events(NOW, NOW + timedelta(days=1)) == [added]
    assert await cal.events(NOW + timedelta(hours=1), NOW + timedelta(days=1)) == []


async def test_add_can_fail_like_the_provider() -> None:
    cal = InMemoryTenantCalendar()
    cal.fail = CalendarProviderUnavailableError("down")
    with pytest.raises(CalendarProviderUnavailableError):
        await cal.add(EVENT)
    assert cal.added == []


async def test_change_moves_a_one_off_and_a_whole_series() -> None:
    weekly = CalendarEvent("s1", "Class", NOW, NOW + timedelta(hours=1), rule="FREQ=WEEKLY")
    cal = InMemoryTenantCalendar([replace(EVENT, external_id="e1"), weekly])
    week = (NOW, NOW + timedelta(days=8))
    dentist, first, second = await cal.events(*week)
    later = timedelta(hours=2)
    await cal.change(EventChange(dentist, title="Doctor", start=dentist.start + later))
    await cal.change(EventChange(second, which="all", start=second.start + later))
    assert [(e.title, e.start - NOW) for e in await cal.events(*week)] == [
        ("Doctor", later),
        ("Class", later),
        ("Class", timedelta(days=7) + later),
    ]
    assert await cal.series_rule(first) == "FREQ=WEEKLY"
    await cal.change(EventChange(first, which="this", delete=True))  # recorded, not applied
    await cal.change(EventChange(first, which="all", delete=True))
    assert [e.title for e in await cal.events(*week)] == ["Doctor"]
    assert len(cal.changed) == 4


async def test_change_can_fail_like_the_provider() -> None:
    cal = InMemoryTenantCalendar([replace(EVENT, external_id="e1")])
    cal.fail = CalendarProviderUnavailableError("down")
    with pytest.raises(CalendarProviderUnavailableError):
        await cal.change(EventChange(replace(EVENT, external_id="e1"), delete=True))
