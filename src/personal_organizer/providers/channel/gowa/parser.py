"""GOWA webhook payload -> :class:`WebhookBatch`.

One pure function, total by design, for the same reason as the Meta parser: its input has
already passed signature verification, so a shape it does not expect is the gateway changing
something, and the right response is to store what can be stored, count what cannot, and
answer 200. An exception here would answer 500 and the gateway would redeliver.

The envelope, one event per POST (``docs/webhook-payload.md`` in the gateway's repo)::

    {"event": "message",
     "device_id": "<our JID>@s.whatsapp.net",
     "session_id": "<device id registered with the gateway>",
     "payload": {"id": "3EB0...", "chat_id": "...@s.whatsapp.net",
                 "from": "31612345678@s.whatsapp.net", "from_lid": "2515...@lid",
                 "timestamp": "2026-10-04T10:30:00Z", "is_from_me": false,
                 "body": "Hello", "replied_to_id": "3EB0..."}}

    {"event": "message.ack", "device_id": "...", "timestamp": "2026-10-04T10:31:00Z",
     "payload": {"ids": ["3EB0..."], "from": "...", "receipt_type": "delivered"}}

**Groups.** A message in a group (``chat_id`` ending ``@g.us``) keeps ``group_id``; the
sender is still ``from``, and ``from_name`` is their push name. A resolved poll vote becomes
``poll_vote`` with the poll id as ``reply_id`` and the first chosen option as text.

**Identity is the phone number.** GOWA normalises ``from`` to the phone JID when it can and
keeps WhatsApp's linked id (LID) in ``from_lid``. The LID becomes the sender's ``user_id``
only when no phone is known: ``SenderRef.key`` prefers ``user_id``, and a person must have
the same key on every channel for the invite-only mute (which is per person) to hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

from personal_organizer.core.phone import normalise_e164
from personal_organizer.interfaces.channel import (
    DeliveryStatus,
    DeliveryUpdate,
    InboundMessage,
    SenderRef,
    WebhookBatch,
)

MESSAGE_EVENT: Final = "message"
ACK_EVENT: Final = "message.ack"

#: A one-to-one chat with a phone-identified or LID-identified user, or a group
#: (``@g.us``). Status broadcasts and newsletters are skipped.
PHONE_SERVER: Final = "@s.whatsapp.net"
LID_SERVER: Final = "@lid"
GROUP_SERVER: Final = "@g.us"
_DIRECT_SERVERS: Final = (PHONE_SERVER, LID_SERVER)
POLL_VOTE: Final = "poll_vote"
#: A vote whose selection names we can trust; ``decrypt_failed`` and the rest carry none.
_READABLE_VOTES: Final = frozenset({"resolved", "partially_resolved"})

#: The key a non-text message's content arrives under, which also names its type. Checked
#: before ``body``: GOWA fills ``body`` with a caption or a summary ("Poll: Lunch?") too.
_KINDS: Final = (
    "image",
    "video",
    "audio",
    "document",
    "sticker",
    "video_note",
    "contact",
    "contacts_array",
    "location",
    "live_location",
    "poll",
    "selection",
    "template",
    "buttons",
    "list",
    "product",
    "order",
)
_MEDIA_KINDS: Final = frozenset({"image", "video", "audio", "document", "sticker", "video_note"})

_RECEIPTS: Final = {"delivered": DeliveryStatus.DELIVERED, "read": DeliveryStatus.READ}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> datetime:
    """GOWA sends RFC 3339. Anything unreadable becomes "now"."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(UTC)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def jid_user(jid: str) -> str:
    """``31612345678:12@s.whatsapp.net`` -> ``31612345678``: no server, no device suffix."""
    return jid.split("@", 1)[0].split(":", 1)[0]


def phone_jid(e164: str) -> str:
    """``+31612345678`` -> ``31612345678@s.whatsapp.net``, the form GOWA sends to."""
    return f"{e164.removeprefix('+')}{PHONE_SERVER}"


def _sender(payload: Mapping[str, Any]) -> SenderRef:
    raw_from = _text(payload.get("from")) or ""
    phone = normalise_e164(jid_user(raw_from)) if raw_from.endswith(PHONE_SERVER) else None
    lid = raw_from if raw_from.endswith(LID_SERVER) else _text(payload.get("from_lid"))
    return SenderRef(user_id=None if phone else lid, phone=phone)


class _Content(TypedDict, total=False):
    message_type: str
    text: str | None
    reply_id: str | None


def _content(payload: Mapping[str, Any]) -> _Content:
    body = _text(payload.get("body"))
    for kind in _KINDS:
        if payload.get(kind) is None:
            continue
        if kind in _MEDIA_KINDS:
            return {"message_type": kind, "text": body}  # the caption, if any
        if kind == "selection":
            selection = _mapping(payload.get("selection"))
            return {
                "message_type": "interactive",
                "reply_id": _text(selection.get("selected_id")),
                "text": _text(selection.get("text")) or body,
            }
        if kind == "poll":
            poll = _mapping(payload.get("poll"))
            if poll.get("type") == "vote" and poll.get("resolution_status") in _READABLE_VOTES:
                chosen = poll.get("selected_options")
                return {
                    "message_type": POLL_VOTE,
                    "reply_id": _text(poll.get("poll_id")),
                    "text": _text(chosen[0]) if isinstance(chosen, list) and chosen else None,
                }
        # A poll, a location, a contact card: GOWA's ``body`` is its own summary, not words
        # the user typed, so it is not passed on as text.
        return {"message_type": kind}
    return {"message_type": "text", "text": body} if body else {"message_type": "unknown"}


def _message(payload: Mapping[str, Any]) -> InboundMessage | None:
    if payload.get("is_from_me") is True:
        return None  # our own sends, echoed back: answering them would loop
    chat_id = _text(payload.get("chat_id"))
    if chat_id is not None and not chat_id.endswith((*_DIRECT_SERVERS, GROUP_SERVER)):
        return None
    provider_message_id = _text(payload.get("id"))
    if provider_message_id is None:
        return None
    sender = _sender(payload)
    if sender.user_id is None and sender.phone is None:
        return None
    return InboundMessage(
        provider_message_id=provider_message_id,
        sender=sender,
        sent_at=_timestamp(payload.get("timestamp")),
        context_message_id=_text(payload.get("replied_to_id")),
        group_id=chat_id if chat_id is not None and chat_id.endswith(GROUP_SERVER) else None,
        sender_name=_text(payload.get("from_name")),
        raw=dict(payload),
        **_content(payload),
    )


def _updates(envelope: Mapping[str, Any], payload: Mapping[str, Any]) -> list[DeliveryUpdate]:
    state = _RECEIPTS.get(str(payload.get("receipt_type")))
    if state is None:
        return []
    if payload.get("from") == envelope.get("device_id"):
        return []  # our own account reading an incoming message, not a receipt for ours
    ids = payload.get("ids")
    at = _timestamp(envelope.get("timestamp"))
    return [
        DeliveryUpdate(provider_message_id=message_id, status=state, at=at)
        for message_id in (ids if isinstance(ids, list) else [])
        if isinstance(message_id, str) and message_id
    ]


def parse_webhook(payload: object, *, device_id: str | None) -> WebhookBatch:
    """Extract the message or delivery receipts in one GOWA event.

    With ``device_id`` set, an event from another of the gateway's devices is skipped -- a
    gateway can hold several, and each posts to the same webhook unless configured apart.
    """
    envelope = _mapping(payload)
    session_id = envelope.get("session_id")
    if device_id is not None and session_id is not None and session_id != device_id:
        return WebhookBatch(skipped=1)
    event = envelope.get("event")
    body = _mapping(envelope.get("payload"))
    if event == MESSAGE_EVENT:
        message = _message(body)
        return WebhookBatch(messages=(message,)) if message else WebhookBatch(skipped=1)
    if event == ACK_EVENT:
        updates = _updates(envelope, body)
        return WebhookBatch(updates=tuple(updates)) if updates else WebhookBatch(skipped=1)
    return WebhookBatch(skipped=1)


__all__ = [
    "ACK_EVENT",
    "GROUP_SERVER",
    "LID_SERVER",
    "MESSAGE_EVENT",
    "PHONE_SERVER",
    "POLL_VOTE",
    "jid_user",
    "parse_webhook",
    "phone_jid",
]
