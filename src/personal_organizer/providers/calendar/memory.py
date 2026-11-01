"""A :class:`~personal_organizer.interfaces.calendar.CalendarProvider` held in memory.

For ``po-chat --fake-calendar`` and the tests: every account sees the same events, and each
``list_events`` call is kept in :attr:`InMemoryCalendar.calls` as ``(account, user, start,
end)``, and each update or delete in :attr:`InMemoryCalendar.writes` as ``(op, event id,
arguments)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, tzinfo

from personal_organizer.core.errors import CalendarProviderRejectedError
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.recurrence import local_rule, occurrences


class InMemoryCalendar:
    def __init__(self, events: Iterable[CalendarEvent] = ()) -> None:
        self.events = list(events)
        self.fail: Exception | None = None  # raised by every call, like a provider that is down
        self.calls: list[tuple[str, str, datetime, datetime]] = []
        self.writes: list[tuple[str, str, dict[str, object]]] = []

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

    def _index(self, event_id: str) -> int | None:
        return next((i for i, e in enumerate(self.events) if e.external_id == event_id), None)

    async def update_event(
        self,
        account_id: str,
        user_id: str,
        event_id: str,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        all_day: bool = False,
        location: str | None = None,
        recurrence: Sequence[str] | None = None,
    ) -> None:
        del account_id, user_id  # every account shares the one set of events
        if self.fail:
            raise self.fail
        args: dict[str, object] = {
            "title": title,
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": location,
            "recurrence": recurrence,
        }
        self.writes.append(("update", event_id, args))
        # shortcut: an instance id (not in the list) is recorded only; apply it if a test needs it.
        i = self._index(event_id)
        if i is None:
            return
        old = self.events[i]
        rule = old.rule
        if recurrence is not None:
            rule = local_rule(recurrence, old.start.tzinfo or UTC, old.all_day)
        self.events[i] = replace(
            old,
            title=old.title if title is None else title,
            start=start or old.start,
            end=end or old.end,
            location=old.location if location is None else location or None,
            rule=rule,
        )

    async def delete_event(self, account_id: str, user_id: str, event_id: str) -> None:
        del account_id, user_id
        if self.fail:
            raise self.fail
        self.writes.append(("delete", event_id, {}))
        i = self._index(event_id)
        if i is not None:
            del self.events[i]

    async def get_series(
        self, account_id: str, user_id: str, event_id: str, zone: tzinfo
    ) -> tuple[CalendarEvent, list[str]]:
        del account_id, user_id, zone
        if self.fail:
            raise self.fail
        i = self._index(event_id)
        if i is None:
            raise CalendarProviderRejectedError(404)
        event = self.events[i]
        return replace(event, rule=None), [f"RRULE:{event.rule}"] if event.rule else []


__all__ = ["InMemoryCalendar"]
