"""Where a send that answers no inbound message goes: ADR 0004's "proactive sends need a
channel choice" seam.

The tenant's primary phone, on the channel they last wrote on, and only while they are
active. Shared by "You're all set", reminders and the morning briefing.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from personal_organizer.core.errors import ChannelNotConfiguredError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Tenant
from personal_organizer.db.repositories.messages import latest_inbound_channel
from personal_organizer.db.repositories.tenants import get_tenant, primary_phone
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.inbound import ChannelResolver

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Target:
    tenant: Tenant
    channel: OutboundChannel
    phone: str


async def proactive_target(
    db: Database, tenant_id: UUID, *, channels: ChannelResolver, event: str
) -> Target | None:
    """The tenant, channel and phone to send to, or ``None`` when there is nowhere to send.

    ``None`` means no inbound message, no phone, a channel this worker does not run, or a
    tenant no longer active. Each is logged as ``event`` with its reason: a retry would find
    the same thing.
    """
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        channel_name = await latest_inbound_channel(session, tenant_id)
        phone = await primary_phone(session, tenant_id)
    if tenant is None or tenant.status != "active":
        log.warning(event, tenant_id=str(tenant_id), reason="not_active")
        return None
    if channel_name is None or phone is None:
        reason = "no_channel" if channel_name is None else "no_phone"
        log.warning(event, tenant_id=str(tenant_id), reason=reason)
        return None
    try:
        channel = channels(channel_name)
    except ChannelNotConfiguredError:
        log.error(
            event, tenant_id=str(tenant_id), channel=channel_name, reason="channel_not_configured"
        )
        return None
    return Target(tenant=tenant, channel=channel, phone=phone)


__all__ = ["Target", "proactive_target"]
