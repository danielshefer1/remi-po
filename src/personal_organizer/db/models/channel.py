"""Channel ledgers: what arrived, and what we sent in reply.

Neither table is tenant-scoped, and that is deliberate rather than an Iteration 03 TODO. The
tenant is not known at ingress -- resolving a sender to a tenant is the worker's first step --
so these are written through ``system_session`` and carry no ``TenantMixin``. From Iteration
03 the worker copies inbound content into the RLS-protected ``messages`` table and nulls it
here, so long-lived personal content ends up under RLS and this stays a transit and
deduplication ledger. See docs/adr/0002.

``channel_outbox`` holds delivery *state* only, never message text: the replies Iteration 02
sends are fixed strings, and from Iteration 04 reply content lives in tenant tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from personal_organizer.db.base import Base, check_in

#: Outbox states in the only order they may move through. Delivery updates arrive out of
#: order and are redelivered, so a status write only ever moves *forward* in this list --
#: see ``messaging.ingress``. ``failed`` is last so that nothing overwrites it.
OUTBOX_STATUS_ORDER: Final = (
    "pending",
    "sending",
    "unknown",
    "accepted",
    "sent",
    "delivered",
    "read",
    "failed",
)

#: What the worker decided about an inbound row. ``onboarding`` (Iteration 03) is a message
#: from a tenant still onboarding, answered by its current step; ``allowed`` is one from an
#: active tenant, handed to ``on_allowed``. The ``group`` ones are the group gate's
#: (Iteration 07, ``messaging.groups.gate``).
DISPOSITIONS: Final = (
    "allowed",
    "stranger",
    "stranger_muted",
    "stale",
    "onboarding",
    "group",
    "group_unserved",
    "group_stale",
    "group_off",
)

_GEN_UUID = text("gen_random_uuid()")


class ChannelInbox(Base):
    __tablename__ = "channel_inbox"
    __table_args__ = (
        # The deduplication key. Meta redelivers for up to seven days and sends duplicates
        # even when nothing failed; ON CONFLICT on this constraint is what makes that safe.
        UniqueConstraint("channel", "provider_message_id"),
        CheckConstraint("sender_user_id IS NOT NULL OR sender_phone IS NOT NULL", name="sender"),
        CheckConstraint(check_in("disposition", DISPOSITIONS), name="disposition"),
        Index(None, "sender_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    channel: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[str] = mapped_column(Text)
    #: ``SenderRef.key`` -- BSUID first, phone otherwise. What ordering and the rate limit key on.
    sender_key: Mapped[str] = mapped_column(Text)
    sender_user_id: Mapped[str | None] = mapped_column(Text)
    sender_phone: Mapped[str | None] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    media_id: Mapped[str | None] = mapped_column(Text)
    media_mime_type: Mapped[str | None] = mapped_column(Text)
    reply_id: Mapped[str | None] = mapped_column(Text)
    context_message_id: Mapped[str | None] = mapped_column(Text)
    #: Set for a message sent in a group: the provider's group id.
    group_id: Mapped[str | None] = mapped_column(Text)
    #: The sender's display name; purged with the body, since a name is personal data too.
    sender_name: Mapped[str | None] = mapped_column(Text)
    #: The single message object as received, so a type this version does not understand can
    #: be re-parsed later. Not the envelope, which repeats other users' contact details.
    #: ``none_as_null``: without it SQLAlchemy writes Python ``None`` as the JSON value
    #: ``null``, so "purging" a stranger's message would leave a non-NULL column behind.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    sent_at: Mapped[datetime]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: Set when the worker has finished with the row; a re-run of the job is then a no-op.
    processed_at: Mapped[datetime | None]
    disposition: Mapped[str | None] = mapped_column(Text)


class ChannelOutbox(Base):
    __tablename__ = "channel_outbox"
    __table_args__ = (
        # At most one reply of each kind per inbound message: the idempotency key for sends.
        UniqueConstraint("inbox_id", "kind"),
        # NULLs are distinct in a unique constraint, so rows not yet accepted do not collide.
        UniqueConstraint("channel", "provider_message_id"),
        # The claim key for a send that answers no inbound row (D6): "You're all set" is
        # ``connected:<connection_id>``. NULLs are distinct, so rows claimed on
        # ``(inbox_id, kind)`` -- every reply -- never collide here.
        UniqueConstraint("idempotency_key"),
        CheckConstraint(check_in("status", OUTBOX_STATUS_ORDER), name="status"),
        Index(None, "recipient_key", "kind", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    channel: Mapped[str] = mapped_column(Text)
    #: Null for a send that answers nothing (a reminder, from Iteration 06).
    inbox_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channel_inbox.id", ondelete="SET NULL")
    )
    #: ``ack`` or ``invite_only`` in Iteration 02.
    kind: Mapped[str] = mapped_column(Text)
    #: ``SenderRef.key`` of the recipient -- what the invite-only rate limit looks up.
    recipient_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    #: Set instead of ``inbox_id`` for a send that answers nothing. See ``messaging.outbox``.
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    #: The provider's numeric error code. Never its error message, which can echo content.
    error_code: Mapped[int | None]
    status_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = ["DISPOSITIONS", "OUTBOX_STATUS_ORDER", "ChannelInbox", "ChannelOutbox"]
