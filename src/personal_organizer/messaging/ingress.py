"""Persist-then-ack: the only thing the webhook does besides verifying.

For each inbound message, in one transaction: insert it keyed on the provider's message id
(``ON CONFLICT DO NOTHING``), and defer its processing job **only if the insert took**. The
job is deferred through Procrastinate's ``connection=`` option, so the job row is written
by the same transaction as the inbox row. There is no instant where one exists without the
other: a crash before COMMIT leaves neither (and Meta redelivers), a crash after leaves both.
That is what makes "replaying the same webhook ten times creates one job" hold without a
sweeper -- see docs/adr/0002.

This module speaks SQL to the psycopg connection Procrastinate's pool hands out, rather than
the SQLAlchemy/asyncpg session everything else uses: the two drivers cannot share a
transaction, and sharing a transaction is the whole point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, LiteralString, Protocol

import procrastinate
from psycopg.types.json import Jsonb

from personal_organizer.db.models.channel import OUTBOX_STATUS_ORDER
from personal_organizer.interfaces.channel import (
    DeliveryUpdate,
    InboundMessage,
    SenderRef,
    WebhookBatch,
)
from personal_organizer.observability.redaction import hash_identifier
from personal_organizer.worker.tasks.channel import HANDLE_INBOUND_TASK

_INSERT_INBOX: Final[LiteralString] = """
INSERT INTO channel_inbox (
    channel, provider_message_id, sender_key, sender_user_id, sender_phone, message_type,
    body, media_id, media_mime_type, reply_id, context_message_id, group_id, sender_name,
    raw, sent_at
) VALUES (
    %(channel)s, %(provider_message_id)s, %(sender_key)s, %(sender_user_id)s, %(sender_phone)s,
    %(message_type)s, %(body)s, %(media_id)s, %(media_mime_type)s, %(reply_id)s,
    %(context_message_id)s, %(group_id)s, %(sender_name)s, %(raw)s, %(sent_at)s
)
ON CONFLICT (channel, provider_message_id) DO NOTHING
RETURNING id
"""

#: Moves an outbox row *forward* only. Meta delivers statuses out of order and more than
#: once, so ``read`` followed by a late ``delivered`` must leave ``read`` in place.
_APPLY_STATUS: Final[LiteralString] = """
UPDATE channel_outbox
SET status = %(status)s,
    status_at = %(at)s,
    error_code = COALESCE(%(error_code)s, error_code),
    updated_at = now()
WHERE channel = %(channel)s
  AND provider_message_id = %(provider_message_id)s
  AND array_position(%(order)s::text[], status) < array_position(%(order)s::text[], %(status)s)
"""


@dataclass(frozen=True, slots=True)
class IngressResult:
    inserted: int = 0
    duplicates: int = 0
    statuses_applied: int = 0


class IngressStore(Protocol):
    async def record(self, channel: str, batch: WebhookBatch) -> IngressResult: ...


def sender_lock(channel: str, sender: SenderRef, pepper: str) -> str:
    """The Procrastinate lock that serialises one sender's messages.

    Hashed, because ``procrastinate_jobs.lock`` is a plain column that outlives the job and
    shows up in every queue inspection. It is the same digest the logs show as
    ``sender_hash``, so a stuck lock can be traced to its log lines.
    """
    return f"inbox:{channel}:{hash_identifier(sender.key, pepper)}"


def _without_nul(value: Any) -> Any:
    """PostgreSQL text and jsonb reject NUL, and a user can send one. An insert that fails
    on it would answer 500 and be redelivered, failing identically, for seven days."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_without_nul(key): _without_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_nul(item) for item in value]
    return value


def _inbox_params(channel: str, message: InboundMessage) -> dict[str, Any]:
    params: dict[str, Any] = _without_nul(
        {
            "channel": channel,
            "provider_message_id": message.provider_message_id,
            "sender_key": message.sender.key,
            "sender_user_id": message.sender.user_id,
            "sender_phone": message.sender.phone,
            "message_type": message.message_type,
            "body": message.text,
            "media_id": message.media_id,
            "media_mime_type": message.media_mime_type,
            "reply_id": message.reply_id,
            "context_message_id": message.context_message_id,
            "group_id": message.group_id,
            "sender_name": message.sender_name,
            "raw": dict(message.raw),
        }
    )
    params["raw"] = Jsonb(params["raw"])
    params["sent_at"] = message.sent_at
    return params


def _status_params(channel: str, update: DeliveryUpdate) -> dict[str, Any]:
    return {
        "channel": channel,
        "provider_message_id": update.provider_message_id,
        "status": update.status.value,
        "at": update.at,
        "error_code": update.error_code,
        "order": list(OUTBOX_STATUS_ORDER),
    }


class PgIngressStore:
    def __init__(self, app: procrastinate.App, *, pepper: str) -> None:
        self._app = app
        self._pepper = pepper

    async def record(self, channel: str, batch: WebhookBatch) -> IngressResult:
        if not batch.messages and not batch.updates:
            return IngressResult()
        inserted = duplicates = applied = 0
        connector = self._app.connector
        if not isinstance(connector, procrastinate.PsycopgConnector):
            # The atomicity argument depends on a psycopg connection Procrastinate accepts.
            msg = f"ingress needs a PsycopgConnector, not {type(connector).__name__}"
            raise TypeError(msg)
        async with connector.pool.connection() as conn, conn.transaction():
            for message in batch.messages:
                cursor = await conn.execute(_INSERT_INBOX, _inbox_params(channel, message))
                row = await cursor.fetchone()
                if row is None:
                    duplicates += 1
                    continue
                inserted += 1
                await self._app.configure_task(
                    HANDLE_INBOUND_TASK,
                    # A drifted task name must fail the transaction, not enqueue a job no
                    # worker will ever run while the webhook answers 200.
                    allow_unknown=False,
                    lock=sender_lock(channel, message.sender, self._pepper),
                    connection=conn,
                ).defer_async(inbox_id=str(row[0]))
            for update in batch.updates:
                cursor = await conn.execute(_APPLY_STATUS, _status_params(channel, update))
                applied += cursor.rowcount
        return IngressResult(inserted=inserted, duplicates=duplicates, statuses_applied=applied)


__all__ = ["IngressResult", "IngressStore", "PgIngressStore", "sender_lock"]
