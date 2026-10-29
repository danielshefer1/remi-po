"""Calendar interfaces.

Two seams. ``ConnectLinker`` takes a user through connecting their Google Calendar: Composio
today, under our own Google OAuth app (a Composio custom auth config). ``CalendarProvider``
reads and writes events once connected. Its caller holds the tenant's ``calendar_connections``
row and passes that row's account and user ids, so nothing a model writes can pick whose
calendar a call reaches. Both stay dependency-free, so swapping an adapter never reaches the
callers.

``CalendarEvent`` carries the title, the times and the location, and nothing else. Anyone can
send an invite into your calendar, so its description and attendees never reach a prompt.
``link`` is the provider's page for the event, for the confirmation of one just created.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, runtime_checkable

#: The one connected-account status that means "tokens held, calls will work".
ACCOUNT_ACTIVE: Final = "ACTIVE"
#: Statuses on the way to ``ACTIVE``: the user is still at Google, or Composio is still
#: exchanging the code. Every other status (``FAILED``, ``EXPIRED``, ``INACTIVE``,
#: ``REVOKED``, and any Composio adds) will not turn ``ACTIVE`` by waiting.
ACCOUNT_CONNECTING: Final = frozenset({"INITIALIZING", "INITIATED"})


@dataclass(frozen=True)
class CalendarEvent:
    external_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    link: str | None = None
    rule: str | None = None
    series_id: str | None = None
    invited: bool = False


Which = Literal["this", "following", "all"]


@dataclass(frozen=True)
class EventChange:
    """A change to, or the deletion of, one event or part of a series."""

    target: CalendarEvent  # the occurrence as list_events showed it
    which: Which = "this"  # "this" for an event that doesn't repeat
    delete: bool = False
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    location: str | None = None  # "" clears it
    rule: str | None = None  # a new pattern; only with which="following"


@dataclass(frozen=True, slots=True)
class ConnectedAccount:
    """What the connect callback needs to know about an account, and nothing more.

    The provider's own record also holds the OAuth tokens. An adapter copies these four fields
    out and drops the rest, so the tokens never reach a log line, an exception or Sentry.
    """

    id: str
    user_id: str
    auth_config_id: str
    status: str


@runtime_checkable
class ConnectLinker(Protocol):
    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        """Start a connection for ``user_id``; return the URL to send the browser to."""
        ...

    async def get_account(self, connected_account_id: str) -> ConnectedAccount: ...


@runtime_checkable
class CalendarProvider(Protocol):
    async def list_events(
        self, account_id: str, user_id: str, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]:
        """Events overlapping ``[start, end)``, both timezone-aware, from the primary calendar."""
        ...

    async def create_event(
        self, account_id: str, user_id: str, event: CalendarEvent
    ) -> CalendarEvent:
        """Add ``event`` (its ``external_id`` is ignored); return it as the provider stored it."""
        ...


__all__ = [
    "ACCOUNT_ACTIVE",
    "ACCOUNT_CONNECTING",
    "CalendarEvent",
    "CalendarProvider",
    "ConnectLinker",
    "ConnectedAccount",
    "EventChange",
    "Which",
]
