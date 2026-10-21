"""The group gate: every group message passes here before anything can cost a model call.

A group is *served* once an active tenant has written in it (``wa_groups.served_at``); being
added to a group proves nothing. Until then every message there is purged, unread and
unkept (``group_unserved``). In a served group:

* a stale message is purged (``group_stale``), as in a direct chat;
* otherwise the message joins the group's short rolling context (a poll vote does not: it is
  an answer, not conversation), and :data:`OnGroup` gets it with who sent it, if they are an
  active tenant, and whether it was for Remi (:mod:`.addressing`). The turn decides the rest;
* the row is then purged (``group``). Nothing said in a group is copied into anyone's
  ``messages``: a group is not a person's private conversation with Remi.

An active tenant's message also makes them a member of the group, and their WhatsApp name
their ``display_name`` if they have none yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.groups import (
    add_context,
    ensure_group,
    mark_served,
    set_display_name,
    touch_member,
)
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.groups.addressing import is_addressed
from personal_organizer.messaging.inbox import InboxRow, finish_row, is_stale
from personal_organizer.messaging.tenancy import resolve_sender

#: The outbox kind of Remi's reply in a group, sent to ``addressing.group_key(jid)``.
GROUP_REPLY: Final = "group:reply"
#: The ``message_type`` of a poll vote, as the GOWA parser stores it.
POLL_VOTE: Final = "poll_vote"
#: Remi's own lines in the group context, the only ones with no ``inbox_id``.
REMI: Final = "Remi"


@dataclass(frozen=True, slots=True)
class GroupTurn:
    group_id: UUID
    jid: str
    #: The sender, if they are an active tenant; ``None`` for anyone else in the group.
    tenant_id: UUID | None
    #: Whether the message was for Remi (:func:`.addressing.is_addressed`).
    addressed: bool
    now: datetime


#: The handoff for a served group's message, answering on the channel it came in on.
OnGroup = Callable[[InboxRow, OutboundChannel, GroupTurn], Awaitable[None]]


async def _member(db: Database, row: InboxRow, group_id: UUID, now: datetime) -> UUID | None:
    """The sender as an active tenant, now a member of the group; ``None`` otherwise."""
    tenant_id = await resolve_sender(db, row)
    if tenant_id is None:
        return None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.status != "active":
            return None
        await touch_member(session, tenant_id, group_id, now)
        if tenant.display_name is None and row.sender_name is not None:
            await set_display_name(session, tenant_id, row.sender_name)
    return tenant_id


async def handle_group(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    current: datetime,
    *,
    on_group: OnGroup,
    bot_phone: str | None,
) -> str:
    """Purge, or keep as context and hand to ``on_group``. Returns the disposition."""
    jid = row.group_id
    assert jid is not None, "handle_group is for group messages"
    async with db.system_session() as session:
        group = await ensure_group(session, channel=row.channel, jid=jid)
    tenant_id = await _member(db, row, group.id, current)
    if tenant_id is not None:
        async with db.system_session() as session:
            await mark_served(session, group.id, current)
    elif group.served_at is None:
        await finish_row(db, row, "group_unserved", purge=True)
        return "group_unserved"
    if is_stale(row, current):
        await finish_row(db, row, "group_stale", purge=True)
        return "group_stale"
    if row.message_type != POLL_VOTE:
        async with db.system_session() as session:
            await add_context(
                session,
                group.id,
                sender_name=row.sender_name,
                body=row.body or f"[{row.message_type}]",
                sent_at=row.sent_at,
                inbox_id=row.id,
            )
    addressed = await is_addressed(db, row, group_jid=jid, bot_phone=bot_phone, now=current)
    await on_group(
        row,
        channel,
        GroupTurn(
            group_id=group.id, jid=jid, tenant_id=tenant_id, addressed=addressed, now=current
        ),
    )
    await finish_row(db, row, "group", purge=True)
    return "group"


__all__ = ["GROUP_REPLY", "POLL_VOTE", "REMI", "GroupTurn", "OnGroup", "handle_group"]
