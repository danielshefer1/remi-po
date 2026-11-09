"""Letting someone you share a group with see your event details (Iteration 07).

The agent's three tools go through the ``Grants`` port, like ``Notes``. ``TenantGrants`` is the
worker's. The details themselves are only ever sent to that person privately.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.tenants import get_tenant, primary_phone
from personal_organizer.interfaces.channel import GroupReader, GroupRoster, OutboundChannel


class Grants(Protocol):
    async def let_see(self, name: str) -> str: ...
    async def stop(self, name: str) -> str: ...
    async def stop_sharing(self) -> str: ...
    async def show_in_group(self, group: str, on: bool) -> str: ...


async def resolve_peer(db: Database, owner: UUID, name: str) -> tuple[UUID, str] | list[str]:
    """The peer ``name`` means and their stored display name (whole name or first word, any
    case), else the names of everyone ``owner`` shares a group with, when none or several match."""
    wanted = name.strip().casefold()
    async with db.system_session() as session:
        peers = await groups.peers_of(session, owner)
    named: list[tuple[UUID, str]] = []
    for peer in peers:
        async with db.tenant_session(TenantId(peer)) as session:
            tenant = await get_tenant(session, peer)
        if tenant is not None and tenant.display_name and tenant.display_name.strip():
            named.append((peer, tenant.display_name.strip()))
    hits = [
        (peer, display)
        for peer, display in named
        if wanted and wanted in (display.casefold(), display.split()[0].casefold())
    ]
    if len(hits) == 1:
        return hits[0]
    return [display for _, display in named]


class TenantGrants:
    def __init__(
        self, db: Database, tenant_id: UUID, channel: OutboundChannel | None = None
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._channel = channel

    async def _peer(self, name: str) -> tuple[UUID, str] | str:
        found = await resolve_peer(self._db, self._tenant_id, name)
        if isinstance(found, tuple):
            return found
        return f"error: who do they mean? people you share a group with: {', '.join(found)}"

    async def let_see(self, name: str) -> str:
        found = await self._peer(name)
        if isinstance(found, str):
            return found
        peer, who = found
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await groups.grant(session, self._tenant_id, peer)
        return f"ok: {who} can now see your event details"

    async def stop(self, name: str) -> str:
        found = await self._peer(name)
        if isinstance(found, str):
            return found
        peer, who = found
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            changed = await groups.revoke(session, self._tenant_id, peer)
        if changed:
            return f"ok: {who} can no longer see your event details"
        return f"ok: {who} couldn't see your event details anyway"

    async def stop_sharing(self) -> str:
        """Stop showing busy/free in every group they okayed it in: groups have no name to
        pick one by, and one they left can't be reached from inside."""
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            shared = await groups.sharing_in(session, self._tenant_id)
            for group in shared:
                await groups.set_sharing(session, self._tenant_id, group, False)
        if not shared:
            return "ok: they weren't sharing in any group"
        return f"ok: stopped sharing in {len(shared)} group{'s' if len(shared) > 1 else ''}"

    async def show_in_group(self, group: str, on: bool) -> str:
        """Open (or close) one group's view of the person's event titles. The group is only ever
        one of their own (``groups_of``, or ``details_in`` to close), so ``set_details`` always
        has a row to change. Group names are the group's words: they only go to the model."""
        none = "error: they aren't in a group with you yet; they can say it in the group"
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            chosen = await (groups.groups_of if on else groups.details_in)(session, self._tenant_id)
            phone = await primary_phone(session, self._tenant_id)
        if not chosen:
            return none
        async with self._db.system_session() as session:
            jids = await groups.group_jids(session, chosen)
        rosters: dict[UUID, GroupRoster | None] = {}
        reader: object = self._channel  # not every channel can list a group
        if isinstance(reader, GroupReader):
            for group_id in chosen:
                rosters[group_id] = await reader.group_roster(jids[group_id])
        if on:  # a group they left can't be what they mean
            chosen = [
                g
                for g in chosen
                if not ((r := rosters.get(g)) and r.complete and phone and phone not in r.phones)
            ]
        names = {g: r.name for g, r in rosters.items() if r and r.name and g in chosen}
        wanted = group.strip().casefold()
        named = [g for g, n in names.items() if wanted and n.casefold() == wanted]
        if len(named) == 1:
            picked = named[0]
        elif len(chosen) == 1 and (not wanted or chosen[0] not in names):
            picked = chosen[0]
        elif not chosen:
            return none
        elif names:
            return (
                f"error: which group? they are in: {', '.join(names.values())}; ask once which "
                "they mean, or call again with its exact name"
            )
        else:
            return "error: can't tell which group; ask them to say it in that group"
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            await groups.set_details(session, self._tenant_id, picked, on)
        name = names.get(picked) or "their group"
        if on:
            return (
                f"ok: {name} can now see the title, time and place of their events, except "
                "private ones"
            )
        return f"ok: {name} only sees when they're busy now"


__all__ = ["Grants", "TenantGrants", "resolve_peer"]
