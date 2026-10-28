"""Onboarding tasks.

``onboarding:connected`` is deferred by the api's connect callback once the connection is bound
and the tenant active. Its kwargs are ids only (docs/adr/0001). It runs on ``webhooks``, the
queue that already sends replies, which every worker takes by default.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import procrastinate

from personal_organizer.db.engine import get_database
from personal_organizer.messaging.runtime import get_outbound_channel
from personal_organizer.onboarding.connected import announce_connected
from personal_organizer.settings import get_settings
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks.channel import INBOUND_RETRY, agent_calendar

ONBOARDING_CONNECTED_TASK: Final = "onboarding:connected"


def register(app: procrastinate.App) -> None:
    @app.task(queue=Queue.WEBHOOKS.value, name=ONBOARDING_CONNECTED_TASK, retry=INBOUND_RETRY)
    async def onboarding_connected(tenant_id: str, connection_id: str) -> None:
        await announce_connected(
            UUID(tenant_id),
            UUID(connection_id),
            db=get_database(),
            channels=get_outbound_channel,
            calendar=agent_calendar() if get_settings().composio.enabled else None,
        )


__all__ = ["ONBOARDING_CONNECTED_TASK", "register"]
