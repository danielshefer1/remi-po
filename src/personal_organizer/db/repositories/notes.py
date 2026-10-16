"""Notes (Iteration 06). Run everything here in ``Database.tenant_session(tenant_id)``."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Note
from personal_organizer.db.repositories.scope import scoped

Added = Literal["added", "exists", "full"]


async def add_note(session: AsyncSession, tenant_id: UUID, content: str, *, limit: int) -> Added:
    """Save ``content`` unless the same note (case and outer spaces aside) is there, or the
    tenant has ``limit`` notes. A turn retried after its ``remember`` saves once; the sender
    lock serialises a tenant's turns."""
    key = content.strip().lower()
    same = await session.scalar(
        scoped(select(Note.id), Note, tenant_id).where(func.lower(func.trim(Note.content)) == key)
    )
    if same is not None:
        return "exists"
    count = await session.scalar(scoped(select(func.count(Note.id)), Note, tenant_id)) or 0
    if count >= limit:
        return "full"
    await session.execute(insert(Note).values(tenant_id=tenant_id, content=content.strip()))
    return "added"


async def list_notes(session: AsyncSession, tenant_id: UUID) -> list[Note]:
    """Newest first: what the profile keeps when it is cut."""
    stmt = scoped(select(Note), Note, tenant_id).order_by(Note.created_at.desc(), Note.id)
    return list(await session.scalars(stmt))


async def remove_note(session: AsyncSession, tenant_id: UUID, note_id: UUID) -> bool:
    result = await session.execute(
        scoped(delete(Note), Note, tenant_id).where(Note.id == note_id).returning(Note.id)
    )
    return result.first() is not None


__all__ = ["Added", "add_note", "list_notes", "remove_note"]
