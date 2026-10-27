"""Family groups (Iteration 07): the group, its rolling context, who is in it, and grants.

Three kinds of call, each in its own session:

* The group and its context (:func:`ensure_group`, :func:`mark_served`, :func:`add_context`,
  :func:`sweep_context`, :func:`recent_context`) are not tenant data:
  ``Database.system_session()``.
* A person's own membership, grants and name (:func:`touch_member`, :func:`set_sharing`,
  :func:`groups_of`, :func:`sharing_in`, :func:`grant`, :func:`revoke`,
  :func:`set_display_name`): their
  ``Database.tenant_session(tenant_id)``. A grant row belongs to its owner.
* Reading across people (:func:`group_tenants`, :func:`peers_of`, :func:`detail_grantors`,
  :func:`due_nudges`): ``system_session``, through the ``SECURITY DEFINER`` functions of
  migration 0009. They return ids only, and whether a member shares.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import Boolean, Uuid, column, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.group import GroupContext, WaGroup
from personal_organizer.db.models.tenant import DetailGrant, GroupMember, Tenant
from personal_organizer.db.repositories.scope import scoped

#: The rolling context: at most this many messages, none older than ``CONTEXT_AGE``.
CONTEXT_LIMIT: Final = 20
CONTEXT_AGE: Final = timedelta(hours=6)
#: How long an ask to okay sharing stays open; after that an "ok" in the group means nothing.
ASK_TTL: Final = timedelta(hours=48)


async def ensure_group(session: AsyncSession, *, channel: str, jid: str) -> WaGroup:
    """The group's row, created the first time it is seen."""
    await session.execute(insert(WaGroup).values(channel=channel, jid=jid).on_conflict_do_nothing())
    stmt = select(WaGroup).where(WaGroup.channel == channel, WaGroup.jid == jid)
    return (await session.scalars(stmt)).one()


async def mark_served(session: AsyncSession, group_id: UUID, now: datetime) -> None:
    """Record when Remi first served the group; later calls leave it."""
    await session.execute(
        update(WaGroup)
        .where(WaGroup.id == group_id, WaGroup.served_at.is_(None))
        .values(served_at=now)
    )


async def add_context(
    session: AsyncSession,
    group_id: UUID,
    *,
    sender_name: str | None,
    body: str,
    sent_at: datetime,
    inbox_id: UUID | None = None,
) -> None:
    """Add one group message, then delete what falls out of the window: older than
    ``CONTEXT_AGE`` before this message, or beyond the newest ``CONTEXT_LIMIT``. A message
    already added under its ``inbox_id`` is not added again."""
    await session.execute(
        insert(GroupContext)
        .values(
            group_id=group_id,
            sender_name=sender_name,
            body=body,
            sent_at=sent_at,
            inbox_id=inbox_id,
        )
        .on_conflict_do_nothing()
    )
    newest = (
        select(GroupContext.id)
        .where(GroupContext.group_id == group_id)
        .order_by(GroupContext.sent_at.desc(), GroupContext.id.desc())
        .limit(CONTEXT_LIMIT)
    )
    await session.execute(
        delete(GroupContext).where(
            GroupContext.group_id == group_id,
            or_(GroupContext.sent_at < sent_at - CONTEXT_AGE, GroupContext.id.not_in(newest)),
        )
    )


async def sweep_context(session: AsyncSession, now: datetime) -> None:
    """Delete every group's messages older than ``CONTEXT_AGE``: a quiet group gets no new
    message to trim it."""
    await session.execute(delete(GroupContext).where(GroupContext.sent_at < now - CONTEXT_AGE))


async def recent_context(session: AsyncSession, group_id: UUID) -> list[GroupContext]:
    """The group's kept messages, oldest first."""
    stmt = (
        select(GroupContext)
        .where(GroupContext.group_id == group_id)
        .order_by(GroupContext.sent_at, GroupContext.id)
    )
    return list(await session.scalars(stmt))


async def touch_member(
    session: AsyncSession, tenant_id: UUID, group_id: UUID, now: datetime
) -> None:
    """The tenant was seen in the group at ``now``; their first time makes them a member."""
    await session.execute(
        insert(GroupMember)
        .values(tenant_id=tenant_id, group_id=group_id, seen_at=now)
        .on_conflict_do_update(index_elements=["tenant_id", "group_id"], set_={"seen_at": now})
    )


async def set_sharing(
    session: AsyncSession, tenant_id: UUID, group_id: UUID, sharing: bool, *, close_ask: bool = True
) -> None:
    """Okay (or stop) showing busy/free in the group; either answers any open ask, unless
    ``close_ask`` is false. Stopping is always the person's own say, so :func:`ask` skips them
    until they share again. :func:`touch_member` comes first."""
    values: dict[str, object] = {"sharing": sharing, "stopped_at": None if sharing else func.now()}
    if close_ask:
        values["asked_at"] = None
    await session.execute(
        scoped(update(GroupMember), GroupMember, tenant_id)
        .where(GroupMember.group_id == group_id)
        .values(values)
    )


async def ask(session: AsyncSession, tenant_id: UUID, group_id: UUID, now: datetime) -> bool:
    """Remi asked this member, in the group, to okay sharing. ``False``, and left alone, if
    they share or said to stop."""
    result = await session.execute(
        scoped(update(GroupMember), GroupMember, tenant_id)
        .where(
            GroupMember.group_id == group_id,
            GroupMember.sharing.is_(False),
            GroupMember.stopped_at.is_(None),
        )
        .values(asked_at=now)
        .returning(GroupMember.id)
    )
    return result.first() is not None


async def clear_ask(session: AsyncSession, tenant_id: UUID, group_id: UUID) -> None:
    """The member's ask is answered; their sharing is left as it is."""
    await session.execute(
        scoped(update(GroupMember), GroupMember, tenant_id)
        .where(GroupMember.group_id == group_id)
        .values(asked_at=None)
    )


async def open_ask(session: AsyncSession, tenant_id: UUID, group_id: UUID, now: datetime) -> bool:
    """Was the member asked, less than ``ASK_TTL`` ago, and not answered yet? An okay read in
    the group keeps it open until Remi's answer is out (``set_sharing(close_ask=False)``)."""
    stmt = scoped(select(GroupMember.id), GroupMember, tenant_id).where(
        GroupMember.group_id == group_id,
        GroupMember.asked_at > now - ASK_TTL,
    )
    return (await session.scalars(stmt)).first() is not None


async def groups_of(session: AsyncSession, tenant_id: UUID) -> list[UUID]:
    """The groups the tenant is a member of."""
    stmt = scoped(select(GroupMember.group_id), GroupMember, tenant_id)
    return list(await session.scalars(stmt))


async def sharing_in(session: AsyncSession, tenant_id: UUID) -> list[UUID]:
    """The groups the tenant okayed sharing in."""
    stmt = scoped(select(GroupMember.group_id), GroupMember, tenant_id).where(GroupMember.sharing)
    return list(await session.scalars(stmt))


async def group_tenants(session: AsyncSession, group_id: UUID) -> list[tuple[UUID, bool]]:
    """``(tenant_id, sharing)`` of the group's active members. In ``system_session``."""
    rows = func.group_tenants(group_id).table_valued(
        column("tenant_id", Uuid()), column("sharing", Boolean())
    )
    result = await session.execute(select(rows.c.tenant_id, rows.c.sharing))
    return [(row[0], row[1]) for row in result]


async def peers_of(session: AsyncSession, tenant_id: UUID) -> list[UUID]:
    """Active tenants who share a group with ``tenant_id``. In ``system_session``."""
    rows = func.peers_of(tenant_id).table_valued(column("tenant_id", Uuid()))
    return list(await session.scalars(select(rows.c.tenant_id)))


async def detail_grantors(session: AsyncSession, viewer: UUID, group_id: UUID) -> list[UUID]:
    """Active owners who granted ``viewer`` details and share in the group. In
    ``system_session``."""
    rows = func.detail_grantors(viewer, group_id).table_valued(column("tenant_id", Uuid()))
    return list(await session.scalars(select(rows.c.tenant_id)))


async def grant(session: AsyncSession, owner: UUID, viewer: UUID) -> bool:
    """``owner`` lets ``viewer`` see their events' details. ``False`` if it already did."""
    result = await session.execute(
        insert(DetailGrant)
        .values(tenant_id=owner, viewer_tenant_id=viewer)
        .on_conflict_do_nothing()
        .returning(DetailGrant.id)
    )
    return result.first() is not None


async def revoke(session: AsyncSession, owner: UUID, viewer: UUID) -> bool:
    """Undo :func:`grant`. ``False`` if there was no grant."""
    result = await session.execute(
        scoped(delete(DetailGrant), DetailGrant, owner)
        .where(DetailGrant.viewer_tenant_id == viewer)
        .returning(DetailGrant.id)
    )
    return result.first() is not None


async def due_nudges(session: AsyncSession, now: datetime) -> list[tuple[UUID, UUID]]:
    """``(poll_id, tenant_id)`` of every follow-up due at ``now``. In ``system_session``."""
    due = func.due_nudges(now).table_valued(column("poll_id", Uuid()), column("tenant_id", Uuid()))
    rows = await session.execute(select(due.c.poll_id, due.c.tenant_id))
    return [(row[0], row[1]) for row in rows]


async def set_display_name(session: AsyncSession, tenant_id: UUID, name: str) -> None:
    """What the person asked to be called."""
    await session.execute(
        scoped(update(Tenant), Tenant, tenant_id).values(display_name=name, updated_at=func.now())
    )


__all__ = [
    "ASK_TTL",
    "CONTEXT_AGE",
    "CONTEXT_LIMIT",
    "add_context",
    "ask",
    "clear_ask",
    "detail_grantors",
    "due_nudges",
    "ensure_group",
    "grant",
    "group_tenants",
    "groups_of",
    "mark_served",
    "open_ask",
    "peers_of",
    "recent_context",
    "revoke",
    "set_display_name",
    "set_sharing",
    "sharing_in",
    "sweep_context",
    "touch_member",
]
