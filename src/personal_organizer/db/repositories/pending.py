"""The one write a tenant has been asked to confirm, on ``tenants.pending_action`` (D5).

:func:`propose` stores it, replacing any earlier one. :func:`claim` takes it in one
statement that clears the column, so of two claims -- a retry, a double "1" -- exactly one
gets it back. The caller commits the claim *before* it runs the write: a crash in between
loses the write rather than doing it twice (ADR 0003). The claim leaves only the inbox row
that took it, for :func:`claimed_by`: a re-run of that message then knows the write was its
own, and runs nothing. Run everything here in ``Database.tenant_session(tenant_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, DateTime, func, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Tenant
from personal_organizer.db.repositories.scope import scoped

TTL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: str
    #: What to run on confirm, e.g. ``create_event``. ``payload`` is its JSON arguments.
    kind: str
    payload: dict[str, Any]
    expires_at: datetime

    @classmethod
    def _from_json(cls, value: dict[str, Any]) -> PendingAction:
        return cls(
            id=value["id"],
            kind=value["kind"],
            payload=value["payload"],
            expires_at=datetime.fromisoformat(value["expires_at"]),
        )


def _unexpired(now: datetime) -> ColumnElement[bool]:
    expires_at = Tenant.pending_action["expires_at"].astext.cast(DateTime(timezone=True))
    unexpired: ColumnElement[bool] = expires_at > now
    return unexpired


async def propose(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    kind: str,
    payload: dict[str, Any],
    now: datetime,
    ttl: timedelta = TTL,
) -> PendingAction:
    """Store a new pending action, replacing whatever was pending."""
    action = PendingAction(id=str(uuid4()), kind=kind, payload=payload, expires_at=now + ttl)
    stored = {
        "id": action.id,
        "kind": kind,
        "payload": payload,
        "expires_at": action.expires_at.isoformat(),
    }
    await session.execute(
        scoped(update(Tenant), Tenant, tenant_id).values(
            pending_action=stored, updated_at=func.now()
        )
    )
    return action


async def current(session: AsyncSession, tenant_id: UUID, *, now: datetime) -> PendingAction | None:
    """The pending action, unless there is none or it has expired."""
    value: dict[str, Any] | None = await session.scalar(
        scoped(select(Tenant.pending_action), Tenant, tenant_id).where(_unexpired(now))
    )
    return PendingAction._from_json(value) if value is not None else None


async def claim(
    session: AsyncSession, tenant_id: UUID, action_id: str, *, now: datetime, by: UUID
) -> PendingAction | None:
    """Take action ``action_id`` for inbox row ``by``, clearing it; ``None`` if it was already
    claimed, replaced, dropped or has expired.

    ``RETURNING`` sees the row after the update, so the old value comes from a
    ``FOR UPDATE`` subquery. A concurrent claim waits on that lock, then re-checks the
    subquery's conditions against the cleared row, matches nothing, and gets ``None``.
    """
    old = (
        scoped(select(Tenant.id, Tenant.pending_action), Tenant, tenant_id)
        .where(Tenant.pending_action["id"].astext == action_id, _unexpired(now))
        .with_for_update()
        .subquery("old")
    )
    value: dict[str, Any] | None = await session.scalar(
        scoped(update(Tenant), Tenant, tenant_id)
        .where(Tenant.id == old.c.id)
        .values(pending_action={"claimed_by": str(by)}, updated_at=func.now())
        .returning(old.c.pending_action)
    )
    return PendingAction._from_json(value) if value is not None else None


async def claimed_by(session: AsyncSession, tenant_id: UUID) -> UUID | None:
    """The inbox row of the last claim, until the next :func:`propose` or :func:`drop`."""
    value: str | None = await session.scalar(
        scoped(select(Tenant.pending_action["claimed_by"].astext), Tenant, tenant_id)
    )
    return UUID(value) if value is not None else None


async def drop(session: AsyncSession, tenant_id: UUID) -> None:
    """Clear the pending action, if any: the person said something else (D5)."""
    await session.execute(
        scoped(update(Tenant), Tenant, tenant_id)
        .where(Tenant.pending_action.is_not(None))
        .values(pending_action=null(), updated_at=func.now())
    )


__all__ = ["TTL", "PendingAction", "claim", "claimed_by", "current", "drop", "propose"]
