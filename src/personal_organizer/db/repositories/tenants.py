"""Tenants: resolution, creation, and the onboarding state on the ``tenants`` row.

:func:`resolve_tenant` and :func:`create_tenant` run in a ``system_session`` -- the worker
does not know the tenant yet, which is the whole point -- and call the ``SECURITY DEFINER``
functions of the same names (migration 0004, D1). They get back an id and nothing else.
Everything else here runs in ``Database.tenant_session(tenant_id)``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Uuid, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Tenant, TenantIdentity
from personal_organizer.db.repositories.scope import scoped


async def resolve_tenant(session: AsyncSession, *, network: str, external_id: str) -> UUID | None:
    """The tenant ``(network, external_id)`` belongs to, or ``None``.

    One key per call. Trying ``uid:`` before ``tel:`` (D12) is the caller's order to choose.
    """
    tenant_id: UUID | None = await session.scalar(
        select(func.resolve_tenant(network, external_id, type_=Uuid()))
    )
    return tenant_id


async def create_tenant(
    session: AsyncSession, *, network: str, external_id: str, phone: str | None, language: str
) -> UUID:
    """Create an ``onboarding`` tenant with this identity, or return the one that has it.

    Idempotent and race-safe on ``(network, external_id)``: two concurrent calls for the same
    sender return the same id and leave one tenant behind.
    """
    result = await session.execute(
        select(func.create_tenant(network, external_id, phone, language, type_=Uuid()))
    )
    tenant_id: UUID | None = result.scalar_one()
    if tenant_id is None:
        # The SQL function guards this itself; this keeps a None from ever reaching
        # tenant_session. No identifiers in the message (no PII).
        msg = "create_tenant returned no tenant id"
        raise RuntimeError(msg)
    return tenant_id


async def get_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
    tenant: Tenant | None = await session.scalar(scoped(select(Tenant), Tenant, tenant_id))
    return tenant


async def _update(session: AsyncSession, tenant_id: UUID, **values: object) -> None:
    await session.execute(
        scoped(update(Tenant), Tenant, tenant_id).values(**values, updated_at=func.now())
    )


async def set_timezone(session: AsyncSession, tenant_id: UUID, timezone: str) -> None:
    """Store a *confirmed* IANA zone. A guess is never stored (D10)."""
    await _update(session, tenant_id, timezone=timezone)


async def set_live_calendar(session: AsyncSession, tenant_id: UUID, on: bool) -> None:
    """Read Google on every look (``True``), or serve the hourly copy."""
    await _update(session, tenant_id, live_calendar=on)


async def set_language(session: AsyncSession, tenant_id: UUID, language: str) -> None:
    """The person asked for the other language (D10). ``enrol``'s detection is the default."""
    await _update(session, tenant_id, language=language)


async def set_onboarding_step(session: AsyncSession, tenant_id: UUID, step: str | None) -> None:
    await _update(session, tenant_id, onboarding_step=step)


async def activate(session: AsyncSession, tenant_id: UUID) -> None:
    """Onboarding is over: ``active``, with no open step."""
    await _update(session, tenant_id, status="active", onboarding_step=None)


async def set_status(session: AsyncSession, tenant_id: UUID, status: str) -> None:
    """``suspended`` and back, from ``po-admin``. Onboarding's own move is :func:`activate`."""
    await _update(session, tenant_id, status=status)


async def primary_phone(session: AsyncSession, tenant_id: UUID) -> str | None:
    """The phone of the tenant's earliest identity that has one -- where a send that answers
    no inbound message goes (D10)."""
    phone: str | None = await session.scalar(
        scoped(select(TenantIdentity.phone), TenantIdentity, tenant_id)
        .where(TenantIdentity.phone.is_not(None))
        .order_by(TenantIdentity.created_at, TenantIdentity.id)
        .limit(1)
    )
    return phone


async def add_identity(
    session: AsyncSession, tenant_id: UUID, *, network: str, external_id: str, phone: str | None
) -> bool:
    """Record another key for a known tenant (D12). ``True`` if it was inserted.

    Run inside ``tenant_session(tenant_id)``: the policy's ``WITH CHECK`` refuses a row for a
    different ``tenant_id``. A key already owned by anyone -- this tenant or another -- is
    skipped by the unique constraint and ``DO NOTHING`` and reported as ``False``. Telling
    "mine" from "someone else's" is the caller's job (``resolve_tenant``).
    """
    inserted: UUID | None = await session.scalar(
        insert(TenantIdentity)
        .values(tenant_id=tenant_id, network=network, external_id=external_id, phone=phone)
        .on_conflict_do_nothing(index_elements=["network", "external_id"])
        .returning(TenantIdentity.id)
    )
    return inserted is not None


__all__ = [
    "activate",
    "add_identity",
    "create_tenant",
    "get_tenant",
    "primary_phone",
    "resolve_tenant",
    "set_language",
    "set_live_calendar",
    "set_onboarding_step",
    "set_status",
    "set_timezone",
]
