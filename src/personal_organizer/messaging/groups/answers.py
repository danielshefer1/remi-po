"""Answering an outing privately (Iteration 07): the agent's ``Outings`` port, like ``Notes``.

``TenantOutings`` is the worker's. Their groups are read in their own session, each group's
open polls in a system session (a poll is the group's), and their answers in their own again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.group import GroupPoll
from personal_organizer.db.models.tenant import PollAnswer
from personal_organizer.db.repositories.groups import groups_of
from personal_organizer.db.repositories.outings import answer_of, open_polls, set_answer


def unanswered(given: PollAnswer | None) -> bool:
    """No vote and no reason: a row may hold only a cleared vote or a follow-up's time."""
    return given is None or (given.answer is None and given.reason is None)


class Outings(Protocol):
    async def waiting(self) -> list[GroupPoll]: ...
    async def answer(self, poll_id: UUID, *, answer: str, reason: str | None) -> None: ...


class TenantOutings:
    def __init__(self, db: Database, tenant_id: UUID, now: datetime) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._now = now

    async def waiting(self) -> list[GroupPoll]:
        """Open outings in their groups, not started, that they haven't answered, soonest first."""
        tenant = TenantId(self._tenant_id)
        async with self._db.tenant_session(tenant) as session:
            group_ids = await groups_of(session, self._tenant_id)
        polls: list[GroupPoll] = []
        async with self._db.system_session() as session:
            for group_id in group_ids:
                polls += await open_polls(session, group_id, self._now)
        async with self._db.tenant_session(tenant) as session:
            answers = [await answer_of(session, self._tenant_id, poll.id) for poll in polls]
        waiting = [poll for poll, given in zip(polls, answers, strict=True) if unanswered(given)]
        return sorted(waiting, key=lambda poll: (poll.starts_at, poll.id))

    async def answer(self, poll_id: UUID, *, answer: str, reason: str | None) -> None:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await set_answer(session, self._tenant_id, poll_id, answer=answer, reason=reason)


__all__ = ["Outings", "TenantOutings", "unanswered"]
