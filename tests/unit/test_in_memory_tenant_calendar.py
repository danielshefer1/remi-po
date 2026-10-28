from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.interfaces.calendar import CalendarEvent
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
