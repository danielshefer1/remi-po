"""The agent's reminders and morning-message setting, behind one small port (Iteration 05).

``TenantSchedule`` is the worker's: each call is its own ``tenant_session``.
``InMemorySchedule`` is ``po-chat``'s, which touches no database, and the tests'.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Protocol
from uuid import UUID, uuid4

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Reminder
from personal_organizer.db.repositories import reminders


class Schedule(Protocol):
    async def add(self, text: str, due_at: datetime) -> None: ...
    async def upcoming(self) -> list[Reminder]: ...
    async def cancel(self, reminder_id: UUID) -> bool: ...
    async def set_briefing(self, at: time | None) -> None: ...


class TenantSchedule:
    """One tenant's reminders in Postgres. ``now`` is the turn's clock: "upcoming" is after it."""

    def __init__(self, db: Database, tenant_id: UUID, now: datetime) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._now = now

    async def add(self, text: str, due_at: datetime) -> None:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await reminders.add_reminder(session, self._tenant_id, text=text, due_at=due_at)

    async def upcoming(self) -> list[Reminder]:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await reminders.upcoming_reminders(session, self._tenant_id, after=self._now)

    async def cancel(self, reminder_id: UUID) -> bool:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await reminders.cancel_reminder(session, self._tenant_id, reminder_id)

    async def set_briefing(self, at: time | None) -> None:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await reminders.set_briefing(session, self._tenant_id, at)


class InMemorySchedule:
    """The same behaviour in a list. ``reminders`` and ``briefing_at`` are there to assert on."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.reminders: list[Reminder] = []
        self.briefing_at: time | None = time(7, 30)

    async def add(self, text: str, due_at: datetime) -> None:
        if any((r.status, r.text, r.due_at) == ("pending", text, due_at) for r in self.reminders):
            return
        self.reminders.append(Reminder(id=uuid4(), text=text, due_at=due_at, status="pending"))

    async def upcoming(self) -> list[Reminder]:
        pending = [r for r in self.reminders if r.status == "pending" and r.due_at > self.now]
        return sorted(pending, key=lambda r: r.due_at)

    async def cancel(self, reminder_id: UUID) -> bool:
        for reminder in self.reminders:
            if reminder.id == reminder_id and reminder.status == "pending":
                reminder.status = "cancelled"
                return True
        return False

    async def set_briefing(self, at: time | None) -> None:
        self.briefing_at = at


__all__ = ["InMemorySchedule", "Schedule", "TenantSchedule"]
