"""Letting someone you share a group with see your event details (Iteration 07).

The agent's two tools go through the ``Grants`` port, like ``Notes``. ``TenantGrants`` is the
worker's. The details themselves are only ever sent to that person privately.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.tenants import get_tenant


class Grants(Protocol):
    async def let_see(self, name: str) -> str: ...
    async def stop(self, name: str) -> str: ...


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
    def __init__(self, db: Database, tenant_id: UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

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


__all__ = ["Grants", "TenantGrants", "resolve_peer"]
