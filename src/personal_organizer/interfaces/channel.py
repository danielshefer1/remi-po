"""Messaging channel interface.

WhatsApp now; Iteration 13 requires contract tests so a Telegram adapter can be added in
one to two days after the beta. Nothing above this Protocol may assume WhatsApp.

Split in two halves because the two processes use disjoint halves: the api only verifies
and parses what arrives (and so never needs an HTTP client), the worker only sends. The
types between them are provider-neutral: a provider's message id is an opaque string, and
the sender is a :class:`SenderRef`, never a bare phone number, because a WhatsApp user who
has set a username can reach us with no phone number in the payload at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SenderRef:
    """Who sent a message, as the provider identifies them.

    ``user_id`` is the provider-scoped user id (WhatsApp's BSUID) and is preferred when
    present: it survives a user hiding their number behind a username. ``phone`` is E.164
    with a leading ``+``. At least one is always set.
    """

    user_id: str | None
    phone: str | None

    @property
    def key(self) -> str:
        """A stable string identity for ordering and deduplication, BSUID first."""
        if self.user_id:
            return f"uid:{self.user_id}"
        return f"tel:{self.phone}"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    provider_message_id: str
    sender: SenderRef
    sent_at: datetime
    #: The provider's own type name -- ``text``, ``audio``, ``interactive``, ``image``, ...
    #: Kept raw rather than mapped onto an enum so an unanticipated type is stored, not lost.
    message_type: str
    text: str | None = None
    media_id: str | None = None
    media_mime_type: str | None = None
    #: The id of the button or list row the user picked, for interactive replies.
    reply_id: str | None = None
    #: The provider id of the message this one replies to, if any.
    context_message_id: str | None = None
    #: The provider's group id (``120363...@g.us``) when the message was sent in a group.
    group_id: str | None = None
    #: The sender's display name as the provider gave it, if any.
    sender_name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class DeliveryStatus(StrEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryUpdate:
    """The provider reporting on a message *we* sent."""

    provider_message_id: str
    status: DeliveryStatus
    at: datetime
    error_code: int | None = None


@dataclass(frozen=True, slots=True)
class WebhookBatch:
    """Everything one webhook delivery carried, in payload order."""

    messages: tuple[InboundMessage, ...] = ()
    updates: tuple[DeliveryUpdate, ...] = ()
    #: Entries that were well-formed JSON but not something we act on -- a foreign phone
    #: number id, an unsubscribed field, a message with no id. Counted so it is visible.
    skipped: int = 0


@dataclass(frozen=True)
class OutboundMessage:
    recipient: str
    body: str


@runtime_checkable
class InboundChannel(Protocol):
    name: str

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """True only if ``signature`` authenticates exactly ``raw_body``. Never raises."""
        ...

    def parse_webhook(self, payload: object) -> WebhookBatch:
        """Extract what we act on from a verified payload. Never raises on shape."""
        ...


@runtime_checkable
class OutboundChannel(Protocol):
    name: str

    async def send_text(self, message: OutboundMessage) -> str:
        """Send ``message`` and return the provider's id for it.

        Raises a :class:`~personal_organizer.core.errors.ChannelError` subclass that says
        whether the message was definitely not delivered or might have been.
        """
        ...

    async def mark_read(self, provider_message_id: str) -> None: ...


@runtime_checkable
class Channel(InboundChannel, OutboundChannel, Protocol):
    """Both halves -- what a provider implements in full, and what contract tests target."""


__all__ = [
    "Channel",
    "DeliveryStatus",
    "DeliveryUpdate",
    "InboundChannel",
    "InboundMessage",
    "OutboundChannel",
    "OutboundMessage",
    "SenderRef",
    "WebhookBatch",
]
