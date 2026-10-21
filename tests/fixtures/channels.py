"""Fakes for the outbound half of a channel, and a raw inbox insert, for worker DB tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from personal_organizer.core.errors import ChannelError, TransientChannelError
from personal_organizer.interfaces.channel import OutboundChannel, OutboundMessage
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.onboarding import Advance
from personal_organizer.messaging.tenancy import TenantState
from tests.fixtures.payloads import SENDER_PHONE


class FakeOutbound:
    name = "whatsapp"

    def __init__(
        self, *failures: ChannelError, mark_read_fails: bool = False, name: str = "whatsapp"
    ) -> None:
        self.name = name
        self.failures = list(failures)
        self.mark_read_fails = mark_read_fails
        self.attempts: list[OutboundMessage] = []
        self.sent: list[OutboundMessage] = []
        self.read: list[str] = []

    async def send_text(self, message: OutboundMessage) -> str:
        self.attempts.append(message)
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)
        return f"{self.name}.OUT{len(self.sent)}"

    async def mark_read(self, provider_message_id: str) -> None:
        if self.mark_read_fails:
            raise TransientChannelError("provider down")
        self.read.append(provider_message_id)


class Spy:
    """Stands in for the agent handoff. Anything reaching it would, from Iteration 04,
    cost an LLM call."""

    def __init__(self) -> None:
        self.calls: list[InboxRow] = []

    async def __call__(
        self, row: InboxRow, channel: OutboundChannel, tenant: TenantState, now: datetime
    ) -> Advance | None:
        self.calls.append(row)
        return None


async def insert_inbox(
    conn: Any,
    *,
    phone: str | None = SENDER_PHONE,
    user_id: str | None = None,
    body: str | None = "hi",
    reply_id: str | None = None,
    message_type: str = "text",
    channel: str = "gowa",
    sent_at: datetime | None = None,
    group_id: str | None = None,
    sender_name: str | None = None,
    context_message_id: str | None = None,
) -> UUID:
    """A row as ingress would store it. The sender key follows ``SenderRef.key``: BSUID first."""
    key = f"uid:{user_id}" if user_id else f"tel:{phone}"
    raw = json.dumps({"body": body}) if body is not None else None
    inbox_id: UUID = await conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_user_id, "
        "sender_phone, message_type, body, reply_id, raw, sent_at, group_id, sender_name, "
        "context_message_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13) RETURNING id",
        channel,
        f"wamid.{uuid4().hex}",
        key,
        user_id,
        phone,
        message_type,
        body,
        reply_id,
        raw,
        sent_at or datetime.now(UTC),
        group_id,
        sender_name,
        context_message_id,
    )
    return inbox_id
