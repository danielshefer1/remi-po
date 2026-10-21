"""WhatsApp groups Remi sits in, their rolling context, and the outing polls it sent there.

None of these is tenant-scoped, like ``channel_inbox``: a group exists before anyone in it is
known, and a poll belongs to a group, not a person. They are read and written through
``system_session``. The one path from a group to its people is ``group_members``, a tenant
table, reached across tenants only through the definer functions of migration 0009, which
return ids. ``group_context`` holds group chatter, but only the newest few messages of the
last few hours (``repositories.groups.CONTEXT_LIMIT`` and ``CONTEXT_AGE``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from personal_organizer.db.base import Base, check_in

POLL_STATUSES: Final = ("open", "closed")

_GEN_UUID = text("gen_random_uuid()")


class WaGroup(Base):
    __tablename__ = "wa_groups"
    __table_args__ = (UniqueConstraint("channel", "jid"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    channel: Mapped[str] = mapped_column(Text)
    #: The provider's group id, e.g. ``1203...@g.us``.
    jid: Mapped[str] = mapped_column(Text)
    #: When Remi first served this group. Set once, never moved.
    served_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GroupContext(Base):
    """One recent group message, kept so "let's do dinner at 7, set it up" makes sense."""

    __tablename__ = "group_context"
    __table_args__ = (Index(None, "group_id", "sent_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("wa_groups.id", ondelete="CASCADE"))
    sender_name: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime]


class GroupPoll(Base):
    """An outing poll Remi sent to a group: what the event is, and the two option names a
    vote is matched against."""

    __tablename__ = "group_polls"
    __table_args__ = (
        # One poll per asking message: a re-run of that message finds the poll it sent.
        UniqueConstraint("inbox_id"),
        CheckConstraint(check_in("status", POLL_STATUSES), name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("wa_groups.id", ondelete="CASCADE"))
    inbox_id: Mapped[UUID]
    #: The poll message's id at the provider, once sent: what a vote refers to.
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    all_day: Mapped[bool]
    location: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    in_option: Mapped[str] = mapped_column(Text)
    out_option: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = ["POLL_STATUSES", "GroupContext", "GroupPoll", "WaGroup"]
