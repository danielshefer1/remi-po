"""Tenants and everything that belongs to one.

Every table here sits under RLS (migration 0004): ``tenants`` through :class:`TenantRoot`,
the rest through :class:`TenantMixin`. The runtime role reaches a row only inside
``Database.tenant_session(tenant_id)`` for that tenant. The one way in without a tenant is the
pair of ``SECURITY DEFINER`` functions ``resolve_tenant`` and ``create_tenant``, owned by
``app_definer``, which return a tenant id and nothing else (D1 in docs/plan-iteration-03.md).

A person is one tenant however many numbers or gateways reach us: identities are keyed by
*network*, not by channel, so the GOWA gateway and Meta's Cloud API both write
``network = 'whatsapp'`` (D12). ``external_id`` is ``SenderRef.key`` -- ``tel:+...`` or
``uid:...``.

Composio's ``user_id`` for a tenant is ``str(tenant.id)``, never a phone number, which is why
``calendar_connections.composio_user_id`` is filled from the tenant id.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from personal_organizer.db.base import Base, TenantMixin, TenantRoot, check_in

TENANT_STATUSES: Final = ("onboarding", "active", "suspended")
#: The open onboarding step; ``NULL`` once onboarding is over. D10.
ONBOARDING_STEPS: Final = ("zone", "connect")
#: Detected from the first message, never asked. D11.
LANGUAGES: Final = ("he", "en")
#: The identity network both WhatsApp channels (``gowa`` and ``whatsapp``) write. D12.
NETWORK_WHATSAPP: Final = "whatsapp"
CONNECTION_STATUSES: Final = ("active", "revoked", "failed")
DIRECTIONS: Final = ("in", "out")
REMINDER_STATUSES: Final = ("pending", "sent", "cancelled", "failed")
POLL_ANSWERS: Final = ("in", "out")

_GEN_UUID = text("gen_random_uuid()")


class Tenant(Base, TenantRoot):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(check_in("status", TENANT_STATUSES), name="status"),
        CheckConstraint(check_in("onboarding_step", ONBOARDING_STEPS), name="onboarding_step"),
        CheckConstraint(check_in("language", LANGUAGES), name="language"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    status: Mapped[str] = mapped_column(Text, server_default=text("'onboarding'"))
    onboarding_step: Mapped[str | None] = mapped_column(Text, server_default=text("'zone'"))
    language: Mapped[str] = mapped_column(Text)
    #: IANA name. ``NULL`` until the user confirms one: a guess from the country code is
    #: shown, never stored (D10).
    timezone: Mapped[str | None] = mapped_column(Text)
    #: The one write waiting for a confirm, ``{id, kind, payload, expires_at}``, or
    #: ``{claimed_by}`` once claimed. Read and written only through ``repositories.pending``
    #: (Iteration 04, D5).
    pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Local time of the morning briefing; ``NULL`` is off. Iteration 05.
    briefing_at: Mapped[time | None] = mapped_column(server_default=text("'07:30'"))
    #: The tenant's last local day a briefing went out, so a re-run or a later tick skips it.
    briefed_on: Mapped[date | None]
    #: What the person asked to be called ("call me Dani"); how groups name them. Iteration 07.
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantIdentity(Base, TenantMixin):
    __tablename__ = "tenant_identities"
    __table_args__ = (
        # One person, one key (D12). create_tenant's idempotency rests on this constraint.
        UniqueConstraint("network", "external_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    network: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    #: E.164, when the identity carries one. A Meta BSUID identity may not.
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CalendarConnection(Base, TenantMixin):
    __tablename__ = "calendar_connections"
    __table_args__ = (
        UniqueConstraint("connected_account_id"),
        CheckConstraint(check_in("status", CONNECTION_STATUSES), name="status"),
        # At most one live connection per tenant. Named by hand: the convention would call it
        # ix_calendar_connections_tenant_id, which TenantMixin's plain index already is.
        Index(
            "uq_calendar_connections_tenant_id_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    #: Always ``str(tenant_id)``; stored so a row can be checked against Composio by itself.
    composio_user_id: Mapped[str] = mapped_column(Text)
    connected_account_id: Mapped[str] = mapped_column(Text)
    auth_config_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    connected_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OnboardingLink(Base, TenantMixin):
    """One connect link. The signature proves we issued it; this row makes it single-use."""

    __tablename__ = "onboarding_links"
    __table_args__ = (UniqueConstraint("nonce"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    nonce: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Message(Base, TenantMixin):
    """Conversation content under RLS (D7).

    For a known tenant the worker copies ``body`` here and nulls it in ``channel_inbox``, in
    one transaction, so long-lived personal content never stays in the un-scoped ledger.
    ``channel`` is the channel *name* (``gowa``, ``whatsapp``): a proactive send goes out on
    the channel of the tenant's latest inbound message.
    """

    __tablename__ = "messages"
    __table_args__ = (CheckConstraint(check_in("direction", DIRECTIONS), name="direction"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    direction: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    inbox_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channel_inbox.id", ondelete="SET NULL")
    )
    message_type: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Reminder(Base, TenantMixin):
    """One reminder the person asked for. Sent once by ``schedule:reminder`` (Iteration 05)."""

    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(check_in("status", REMINDER_STATUSES), name="status"),
        Index(None, "status", "due_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime]
    # A plain string default: the ``text`` column above shadows ``sqlalchemy.text`` here.
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # A repeating reminder is a chain of rows: each carries the RRULE (never with COUNT) and
    # the first row's id; that first row's own ``series_id`` stays NULL.
    rule: Mapped[str | None] = mapped_column(Text)
    series_id: Mapped[UUID | None]


class Note(Base, TenantMixin):
    """One thing the person asked the agent to remember, or told it in passing (Iteration 06)."""

    __tablename__ = "notes"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GroupMember(Base, TenantMixin):
    """A tenant seen in a group, and whether they okayed sharing busy/free there (Iteration 07).

    The only path from a group to its people. Per person, per group.
    """

    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("tenant_id", "group_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("wa_groups.id", ondelete="CASCADE"))
    sharing: Mapped[bool] = mapped_column(server_default=text("false"))
    seen_at: Mapped[datetime]
    asked_at: Mapped[datetime | None]


class DetailGrant(Base, TenantMixin):
    """The owner (``tenant_id``) lets ``viewer_tenant_id`` see their events' details, in every
    group where the owner is sharing (Iteration 07)."""

    __tablename__ = "detail_grants"
    __table_args__ = (UniqueConstraint("tenant_id", "viewer_tenant_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    viewer_tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PollAnswer(Base, TenantMixin):
    """One person's answer to an outing poll, and the follow-ups it stops (Iteration 07).

    ``claimed_at`` set means the event was, or may have been, added to their calendar: it is
    claimed at most once, before the write, as with ``pending.claim``.
    """

    __tablename__ = "poll_answers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "poll_id"),
        CheckConstraint(check_in("answer", POLL_ANSWERS), name="answer"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    poll_id: Mapped[UUID] = mapped_column(ForeignKey("group_polls.id", ondelete="CASCADE"))
    answer: Mapped[str | None] = mapped_column(Text)
    #: Why they can't come, in their own words: "Eden can't, she's at work".
    reason: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None]
    nudged_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "CONNECTION_STATUSES",
    "DIRECTIONS",
    "LANGUAGES",
    "NETWORK_WHATSAPP",
    "ONBOARDING_STEPS",
    "POLL_ANSWERS",
    "REMINDER_STATUSES",
    "TENANT_STATUSES",
    "CalendarConnection",
    "DetailGrant",
    "GroupMember",
    "Message",
    "Note",
    "OnboardingLink",
    "PollAnswer",
    "Reminder",
    "Tenant",
    "TenantIdentity",
]
