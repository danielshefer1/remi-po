"""ORM models. Importing this package registers every table on ``Base.metadata``, which is
what Alembic's autogenerate and the model-vs-migration test compare against."""

from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.db.models.group import GroupContext, GroupPoll, WaGroup
from personal_organizer.db.models.invite import Invite
from personal_organizer.db.models.tenant import (
    CalendarConnection,
    DetailGrant,
    Event,
    GroupMember,
    Message,
    OnboardingLink,
    PollAnswer,
    Reminder,
    Tenant,
    TenantIdentity,
)

__all__ = [
    "CalendarConnection",
    "ChannelInbox",
    "ChannelOutbox",
    "DetailGrant",
    "Event",
    "GroupContext",
    "GroupMember",
    "GroupPoll",
    "Invite",
    "Message",
    "OnboardingLink",
    "PollAnswer",
    "Reminder",
    "Tenant",
    "TenantIdentity",
    "WaGroup",
]
