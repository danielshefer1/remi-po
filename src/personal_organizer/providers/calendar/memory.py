"""A :class:`~personal_organizer.interfaces.calendar.CalendarProvider` held in memory.

For ``po-chat --fake-calendar`` and the tests: every account sees the same events, and each
``list_events`` call is kept in :attr:`InMemoryCalendar.calls` as ``(account, user, start,
end)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.recurrence import occurrences


class InMemoryCalendar:
    def __init__(self, events: Iterable[CalendarEvent] = ()) -> None:
        self.events = list(events)
        self.fail: Exception | None = None  # raised by every call, like a provider that is down
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    async def list_events(
        self, account_id: str, user_id: str, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]:
        self.calls.append((account_id, user_id, start, end))
        if self.fail:
            raise self.fail
        # Like singleEvents=True: a series comes back as rule-less instances.
        found = [
            replace(o, rule=None)
            for event in self.events
            for o in occurrences(event, start, end, event.start.tzinfo or UTC)
            if o.start < end and o.end > start
        ]
        return sorted(found, key=lambda event: event.start)

    async def create_event(
        self, account_id: str, user_id: str, event: CalendarEvent
    ) -> CalendarEvent:
        del account_id, user_id  # every account shares the one set of events
        if self.fail:
            raise self.fail
        stored = replace(event, external_id=f"memory-{len(self.events) + 1}")
        self.events.append(stored)
        return stored


__all__ = ["InMemoryCalendar"]
