"""The agent's reminders, morning-message setting and time zone, behind one small port
(Iteration 05).

``TenantSchedule`` is the worker's: each call is its own ``tenant_session``.
``InMemorySchedule`` is ``po-chat``'s, which touches no database, and the tests'.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Reminder
from personal_organizer.db.repositories import reminders, tenants
from personal_organizer.messaging.recurrence import next_after


class Schedule(Protocol):
    async def add(self, text: str, due_at: datetime, rule: str | None = None) -> None: ...
    async def upcoming(self) -> list[Reminder]: ...
    async def recent_sent(self) -> list[Reminder]: ...
    async def cancel(self, reminder_id: UUID, *, just_once: bool = False) -> bool: ...
    async def set_briefing(self, at: time | None) -> None: ...
    async def set_timezone(self, zone: str) -> None: ...


class TenantSchedule:
    """One tenant's reminders in Postgres. ``now`` is the turn's clock: "upcoming" is after it."""

    def __init__(self, db: Database, tenant_id: UUID, now: datetime) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._now = now

    async def add(self, text: str, due_at: datetime, rule: str | None = None) -> None:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await reminders.add_reminder(
                session, self._tenant_id, text=text, due_at=due_at, rule=rule
            )

    async def upcoming(self) -> list[Reminder]:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await reminders.upcoming_reminders(session, self._tenant_id, after=self._now)

    async def recent_sent(self) -> list[Reminder]:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await reminders.recent_sent(session, self._tenant_id)

    async def cancel(self, reminder_id: UUID, *, just_once: bool = False) -> bool:
        """Cancel the pending occurrence, which stops its series (a series has one pending row).
        ``just_once`` skips only this one: the series goes on at its next time."""
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            reminder = await reminders.get_reminder(session, self._tenant_id, reminder_id)
            if not await reminders.cancel_reminder(session, self._tenant_id, reminder_id):
                return False
            if just_once and reminder is not None:
                tenant = await tenants.get_tenant(session, self._tenant_id)
                zone = ZoneInfo(tenant.timezone if tenant and tenant.timezone else "UTC")
                await reminders.schedule_next(
                    session, self._tenant_id, reminder, zone, after=self._now
                )
            return True

    async def set_briefing(self, at: time | None) -> None:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await reminders.set_briefing(session, self._tenant_id, at)

    async def set_timezone(self, zone: str) -> None:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await tenants.set_timezone(session, self._tenant_id, zone)


class InMemorySchedule:
    """The same behaviour in a list. ``reminders``, ``briefing_at`` and ``timezone`` are there
    to assert on."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.reminders: list[Reminder] = []
        self.briefing_at: time | None = time(7, 30)
        self.timezone: str | None = None

    async def add(self, text: str, due_at: datetime, rule: str | None = None) -> None:
        same = ("pending", text, due_at, rule)
        if any((r.status, r.text, r.due_at, r.rule) == same for r in self.reminders):
            return
        self.reminders.append(
            Reminder(id=uuid4(), text=text, due_at=due_at, status="pending", rule=rule)
        )

    async def upcoming(self) -> list[Reminder]:
        pending = [r for r in self.reminders if r.status == "pending" and r.due_at > self.now]
        return sorted(pending, key=lambda r: r.due_at)

    async def recent_sent(self) -> list[Reminder]:
        sent = [r for r in self.reminders if r.status == "sent"]
        return sorted(sent, key=lambda r: r.due_at, reverse=True)[:5]

    async def cancel(self, reminder_id: UUID, *, just_once: bool = False) -> bool:
        for reminder in self.reminders:
            if reminder.id == reminder_id and reminder.status == "pending":
                reminder.status = "cancelled"
                if just_once and reminder.rule is not None:
                    # Like ``reminders.schedule_next``, stepping in the time's own zone.
                    nxt = next_after(reminder.rule, reminder.due_at)
                    while nxt is not None and nxt <= self.now:
                        nxt = next_after(reminder.rule, nxt)
                    if nxt is not None:
                        series = reminder.series_id or reminder.id
                        self.reminders.append(
                            Reminder(
                                id=uuid4(),
                                text=reminder.text,
                                due_at=nxt,
                                status="pending",
                                rule=reminder.rule,
                                series_id=series,
                            )
                        )
                return True
        return False

    async def set_briefing(self, at: time | None) -> None:
        self.briefing_at = at

    async def set_timezone(self, zone: str) -> None:
        self.timezone = zone


__all__ = ["InMemorySchedule", "Schedule", "TenantSchedule"]
