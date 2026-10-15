"""ORM models. Importing this package registers every table on ``Base.metadata``, which is
what Alembic's autogenerate and the model-vs-migration test compare against."""

from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.db.models.invite import Invite
from personal_organizer.db.models.tenant import (
    CalendarConnection,
    Message,
    OnboardingLink,
    Reminder,
    Tenant,
    TenantIdentity,
)

__all__ = [
    "CalendarConnection",
    "ChannelInbox",
    "ChannelOutbox",
    "Invite",
    "Message",
    "OnboardingLink",
    "Reminder",
    "Tenant",
    "TenantIdentity",
]
