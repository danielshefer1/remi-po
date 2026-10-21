"""Outing polls and each person's answer to them (Iteration 07).

A poll is the group's, not a person's: :func:`create_poll`, :func:`set_poll_message`,
:func:`poll_by_message` and :func:`open_polls` run in ``Database.system_session()``. An answer
is its person's: :func:`answer_of`, :func:`set_answer`, :func:`claim_event` and
:func:`mark_nudged` run in their ``Database.tenant_session(tenant_id)``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.group import GroupPoll
from personal_organizer.db.models.tenant import PollAnswer
from personal_organizer.db.repositories.scope import scoped
from personal_organizer.interfaces.calendar import CalendarEvent

_KEY = ["tenant_id", "poll_id"]


async def create_poll(
    session: AsyncSession,
    *,
    group_id: UUID,
    inbox_id: UUID,
    event: CalendarEvent,
    language: str,
    in_option: str,
    out_option: str,
) -> GroupPoll:
    """The poll asked for by ``inbox_id``: stored now, or as an earlier run stored it."""
    await session.execute(
        insert(GroupPoll)
        .values(
            group_id=group_id,
            inbox_id=inbox_id,
            title=event.title,
            starts_at=event.start,
            ends_at=event.end,
            all_day=event.all_day,
            location=event.location,
            language=language,
            in_option=in_option,
            out_option=out_option,
        )
        .on_conflict_do_nothing(index_elements=["inbox_id"])
    )
    return (await session.scalars(select(GroupPoll).where(GroupPoll.inbox_id == inbox_id))).one()


async def set_poll_message(session: AsyncSession, poll_id: UUID, provider_message_id: str) -> None:
    """The poll message's provider id: what its votes refer to."""
    await session.execute(
        update(GroupPoll)
        .where(GroupPoll.id == poll_id)
        .values(provider_message_id=provider_message_id)
    )


async def poll_by_message(
    session: AsyncSession, group_id: UUID, provider_message_id: str
) -> GroupPoll | None:
    stmt = select(GroupPoll).where(
        GroupPoll.group_id == group_id, GroupPoll.provider_message_id == provider_message_id
    )
    return (await session.scalars(stmt)).one_or_none()


async def open_polls(session: AsyncSession, group_id: UUID, now: datetime) -> list[GroupPoll]:
    """The group's open polls whose event hasn't started, soonest first."""
    stmt = (
        select(GroupPoll)
        .where(GroupPoll.group_id == group_id, GroupPoll.status == "open")
        .where(GroupPoll.starts_at > now)
        .order_by(GroupPoll.starts_at, GroupPoll.id)
    )
    return list(await session.scalars(stmt))


async def answer_of(session: AsyncSession, tenant_id: UUID, poll_id: UUID) -> PollAnswer | None:
    stmt = scoped(select(PollAnswer), PollAnswer, tenant_id).where(PollAnswer.poll_id == poll_id)
    return (await session.scalars(stmt)).one_or_none()


async def set_answer(
    session: AsyncSession, tenant_id: UUID, poll_id: UUID, *, answer: str | None, reason: str | None
) -> None:
    """Their answer (``'in'``, ``'out'`` or ``None``) and reason, replacing any earlier one."""
    values = {"answer": answer, "reason": reason}
    await session.execute(
        insert(PollAnswer)
        .values(tenant_id=tenant_id, poll_id=poll_id, **values)
        .on_conflict_do_update(index_elements=_KEY, set_={**values, "updated_at": func.now()})
    )


async def claim_event(session: AsyncSession, tenant_id: UUID, poll_id: UUID, now: datetime) -> bool:
    """Take the one event creation of this poll for them. Commit the session before creating
    it: a re-run or a second "in" then finds it taken. Their answer row exists already."""
    result = await session.execute(
        scoped(update(PollAnswer), PollAnswer, tenant_id)
        .where(PollAnswer.poll_id == poll_id, PollAnswer.claimed_at.is_(None))
        .values(claimed_at=now, updated_at=func.now())
        .returning(PollAnswer.id)
    )
    return result.first() is not None


async def mark_nudged(session: AsyncSession, tenant_id: UUID, poll_id: UUID, now: datetime) -> None:
    """They were sent a follow-up at ``now``."""
    await session.execute(
        insert(PollAnswer)
        .values(tenant_id=tenant_id, poll_id=poll_id, nudged_at=now)
        .on_conflict_do_update(index_elements=_KEY, set_={"nudged_at": now})
    )


__all__ = [
    "answer_of",
    "claim_event",
    "create_poll",
    "mark_nudged",
    "open_polls",
    "poll_by_message",
    "set_answer",
    "set_poll_message",
]
