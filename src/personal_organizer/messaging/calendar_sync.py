"""Copy a member's Google events into their calendar (issue #50): on connect, then hourly."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog

from personal_organizer.core.errors import CalendarNotConnectedError, CalendarProviderError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import active_connection, revoke_connection
from personal_organizer.db.repositories.events import IMPORT_AHEAD, mark_synced
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.calendar import (
    ACCOUNT_ACTIVE,
    CalendarProvider,
    ConnectLinker,
)
from personal_organizer.messaging.calendar import TenantCalendar

log = structlog.get_logger(__name__)


async def sync_calendar(
    tenant_id: UUID, *, db: Database, provider: CalendarProvider | None, now: datetime
) -> bool:
    """Import yesterday through ``IMPORT_AHEAD`` from Google; ``True`` once copied (and marked
    synced). On failure the stored rows stay and ``synced_at`` too, so the next tick retries.
    An account Google no longer accepts is revoked, so the person counts as not connected."""
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        account = await active_connection(session, tenant_id)
    if provider is None or tenant is None or account is None:
        return False
    # In their zone: all-day events are stored as midnight in the window's zone.
    zone = ZoneInfo(tenant.timezone or "UTC")
    start, end = (now - timedelta(days=1)).astimezone(zone), (now + IMPORT_AHEAD).astimezone(zone)
    theirs = TenantCalendar(db, tenant_id, provider=provider, account=account)
    # shortcut: one Google page (250 events) per read; follow nextPageToken if a 90-day import
    # is ever cut (refresh keeps what is stored past the last event it got).
    revoke = False
    try:
        copied = await theirs.refresh(start, end, raise_not_connected=True)
    except CalendarNotConnectedError:
        # Composio's 422 is ambiguous: revoke only if it says the account no longer works.
        copied = False
        revoke = await _account_is_dead(provider, account.connected_account_id)
    if revoke:
        log.warning("calendar.sync_disconnected", tenant_id=str(tenant_id))
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await revoke_connection(session, tenant_id, account.id)
        return False
    # Stamped after every try: a failing Google is asked again in an hour, not every tick.
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await mark_synced(session, tenant_id, now)
    return copied


async def _account_is_dead(provider: CalendarProvider, account_id: str) -> bool:
    if not isinstance(provider, ConnectLinker):
        return False
    try:
        return (await provider.get_account(account_id)).status != ACCOUNT_ACTIVE
    except CalendarProviderError:
        return False


__all__ = ["sync_calendar"]
