"""Calendar connections: which Composio account a tenant's calendar is reached through.

At most one ``active`` row per tenant (a partial unique index). Reconnecting revokes the old
row and inserts the new one in the caller's transaction, so there is never a moment with two
active rows or, for anyone reading through RLS, none.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.db.repositories.scope import scoped

#: The partial unique index behind "at most one active row per tenant".
ONE_ACTIVE_INDEX: Final = "uq_calendar_connections_tenant_id_active"


class ConcurrentBindError(Exception):
    """Another bind for the same tenant, of a different account, committed its active row
    while this one was inserting. Nothing of this bind is left; trying again revokes that row
    and binds cleanly."""


def _violates(exc: IntegrityError, name: str) -> bool:
    """Whether ``exc`` is a violation of the constraint or index ``name``. asyncpg's error,
    which carries the name, is chained beneath SQLAlchemy's adapted one."""
    seen: set[int] = set()
    current: BaseException | None = exc.orig
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "constraint_name", None) == name:
            return True
        current = current.__cause__
    return False


async def _by_account(
    session: AsyncSession, tenant_id: UUID, connected_account_id: str
) -> CalendarConnection | None:
    row: CalendarConnection | None = await session.scalar(
        scoped(select(CalendarConnection), CalendarConnection, tenant_id).where(
            CalendarConnection.connected_account_id == connected_account_id
        )
    )
    return row


async def bind_connection(
    session: AsyncSession, tenant_id: UUID, *, connected_account_id: str, auth_config_id: str
) -> CalendarConnection:
    """Make ``connected_account_id`` the tenant's active connection.

    Idempotent on ``connected_account_id``: a callback delivered twice finds its row and
    returns it unchanged, whatever its status, without revoking anything. A *new* account
    revokes the previous active one first. ``composio_user_id`` is the tenant id, never a
    phone number. ``ON CONFLICT DO NOTHING`` covers the race of two callbacks for the same
    account: the loser waits for the winner's commit and then reads its row.

    Raises :class:`ConcurrentBindError` when another account's bind for this tenant committed
    first (two callbacks at the same moment).

    Raises :class:`LookupError` when the account is already bound to *another* tenant. The
    callback's ``user_id`` check (D5) makes that unreachable in practice; if it ever happens,
    the savepoint rolls back the revoke and nothing of either tenant changes, whether or
    not the caller then rolls back.
    """
    existing = await _by_account(session, tenant_id, connected_account_id)
    if existing is not None:
        return existing
    # One savepoint around revoke + insert: the errors below leave it, so the revoke is undone
    # even if the caller catches the error and commits the outer transaction.
    try:
        async with session.begin_nested():
            await session.execute(
                scoped(update(CalendarConnection), CalendarConnection, tenant_id)
                .where(CalendarConnection.status == "active")
                .values(status="revoked")
            )
            await session.execute(
                insert(CalendarConnection)
                .values(
                    tenant_id=tenant_id,
                    composio_user_id=str(tenant_id),
                    connected_account_id=connected_account_id,
                    auth_config_id=auth_config_id,
                )
                .on_conflict_do_nothing(index_elements=["connected_account_id"])
            )
            bound = await _by_account(session, tenant_id, connected_account_id)
            if bound is None:
                # The insert conflicted with a row RLS will not show us: another tenant's.
                msg = "connected account is bound to another tenant"
                raise LookupError(msg)
    except IntegrityError as exc:
        # The revoke saw no uncommitted active row of a concurrent bind; the insert waited on
        # it and then collided. Only that index: any other violation is a fault.
        if _violates(exc, ONE_ACTIVE_INDEX):
            raise ConcurrentBindError from None
        raise
    return bound


async def active_connection(session: AsyncSession, tenant_id: UUID) -> CalendarConnection | None:
    """The tenant's active connection, if it has one."""
    row: CalendarConnection | None = await session.scalar(
        scoped(select(CalendarConnection), CalendarConnection, tenant_id).where(
            CalendarConnection.status == "active"
        )
    )
    return row


async def revoke_connection(session: AsyncSession, tenant_id: UUID) -> None:
    """The tenant's active account stopped working: it counts as not connected until they
    connect again (:func:`bind_connection` then inserts a fresh row)."""
    await session.execute(
        scoped(update(CalendarConnection), CalendarConnection, tenant_id)
        .where(CalendarConnection.status == "active")
        .values(status="revoked")
    )


__all__ = [
    "ONE_ACTIVE_INDEX",
    "ConcurrentBindError",
    "active_connection",
    "bind_connection",
    "revoke_connection",
]
