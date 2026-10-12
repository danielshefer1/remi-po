"""Composio as the :class:`~personal_organizer.interfaces.calendar.ConnectLinker` and the
:class:`~personal_organizer.interfaces.calendar.CalendarProvider`.

Composio's Python SDK is synchronous, so every call runs on a worker thread under one deadline,
``COMPOSIO__REQUEST_TIMEOUT_S``, which also covers the SDK's own retry. Three SDK defaults are
changed here:

- **Telemetry.** Methods of the SDK's ``Resource`` classes post each call to
  ``telemetry.composio.dev``, with redacted error text and a stack trace. The off switch is a
  ``ContextVar``, and ``Composio(allow_tracking=False)`` sets it only in the context that
  constructs the client. The api constructs this one in its lifespan, so request handlers would
  still see the default, ``True``. Each call therefore sets it again inside the thread's copied
  context. (In 0.24.0, neither ``connected_accounts.link`` nor ``.get`` is traced. This keeps
  it that way after an upgrade.)
- **Request logs.** The client logs every request at INFO. ``LogLevel.WARNING`` keeps only
  failures. They go through the root logger, so the redaction chain sees them.
- **The multiple-accounts guard.** ``link`` refuses a user who already has an ``ACTIVE``
  account on the auth config. A tenant whose first attempt reached ``ACTIVE`` but whose callback
  never arrived would then be stuck for good. So ``allow_multiple=True``, and the callback's
  ``bind_connection`` revokes our old row.

The SDK's account record carries the OAuth tokens (``data``, ``params``, ``state``).
:meth:`ComposioConnector.get_account` copies four fields out and lets the record go.

Calendar calls go through ``tools.execute`` with two slugs and nothing else: no tool router, no
``tools.get()`` schemas. Each call names the caller's ``connected_account_id`` and ``user_id``
and the pinned toolkit version, and its arguments are built here from typed parameters. So
nothing a model writes can choose the account, the user, the calendar or the toolkit (plan
D2). Events are cut down to title, times and location as they are parsed (D6).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, tzinfo
from typing import Any, Final
from zoneinfo import ZoneInfo

import anyio
import anyio.to_thread
import composio_client
import httpx
from composio import Composio
from composio import exceptions as composio_exceptions
from composio.core.models.base import allow_tracking
from composio.sdk import SDKConfig
from composio.utils.logging import LogLevel

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderError,
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
    ConfigError,
)
from personal_organizer.interfaces.calendar import CalendarEvent, ConnectedAccount
from personal_organizer.settings import ComposioSettings

#: Statuses worth another attempt later. Any 5xx is too.
_TRANSIENT_STATUSES: Final = frozenset({408, 409, 429})
#: One retry, for a quick 5xx. A slow failure uses up the deadline before a retry could finish.
_SDK_MAX_RETRIES: Final = 1
#: Worker threads for SDK calls: at most four in flight, drawn from this limiter rather than
#: anyio's shared default (40), so a slow Composio cannot take the threads every other
#: ``to_thread`` user waits on. It does not cap abandoned calls: anyio releases the token when a
#: call past its deadline is abandoned, and that call keeps its thread until the SDK gives up.
SDK_LIMITER: Final = anyio.CapacityLimiter(4)

#: The Google Calendar toolkit version every call runs, so a Composio release can't change the
#: arguments or the response shape under us. Upgrading means re-checking the test fixtures.
TOOLKIT: Final = "googlecalendar"
TOOLKIT_VERSION: Final = "20261010_00"
LIST_EVENTS: Final = "GOOGLECALENDAR_EVENTS_LIST"
CREATE_EVENT: Final = "GOOGLECALENDAR_CREATE_EVENT"


def _untracked[T](fn: Callable[[], T]) -> T:
    """Run ``fn`` with Composio's telemetry off, in this thread's context only."""
    token = allow_tracking.set(False)
    try:
        return fn()
    finally:
        allow_tracking.reset(token)


def _from_status(status: int | None) -> CalendarProviderError:
    if status is None:
        return CalendarProviderRejectedError(None)
    if status >= 500 or status in _TRANSIENT_STATUSES:
        return CalendarProviderUnavailableError(f"Calendar provider answered {status}")
    if status == 422:  # Composio: "invalid state of the connected account"
        return CalendarNotConnectedError(status)
    return CalendarProviderRejectedError(status)


def _translate(exc: Exception) -> CalendarProviderError:
    if isinstance(exc, composio_client.APIConnectionError):  # APITimeoutError is one too
        return CalendarProviderUnavailableError("Calendar provider unreachable")
    if isinstance(exc, composio_client.APIStatusError):
        return _from_status(exc.status_code)
    return CalendarProviderRejectedError(None)


def _rfc3339(moment: datetime) -> str:
    if moment.utcoffset() is None:
        msg = "calendar times must be timezone-aware"
        raise ValueError(msg)
    return moment.isoformat(timespec="seconds")


def _when(field: Any, zone: tzinfo | None) -> tuple[datetime, bool]:
    """Google's ``{"dateTime": ...}`` or, for an all-day event, ``{"date": ...}``."""
    if "dateTime" in field:
        moment = datetime.fromisoformat(field["dateTime"])
        if moment.utcoffset() is None:
            raise ValueError
        return moment, False
    # A date has no zone; midnight in the caller's zone keeps it on the same day for them.
    return datetime.combine(date.fromisoformat(field["date"]), time(), zone), True


def _event(item: Any, zone: tzinfo | None) -> CalendarEvent:
    """Title, times and location only: descriptions and attendees are anyone's to write."""
    start, all_day = _when(item["start"], zone)
    end, _ = _when(item["end"], zone)
    title, location = item.get("summary", ""), item.get("location")
    if not isinstance(title, str) or not isinstance(location, str | None):
        raise TypeError
    return CalendarEvent(
        external_id=str(item["id"]),
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        location=location or None,
    )


class ComposioConnector:
    def __init__(self, sdk: Any, *, timeout_s: float) -> None:
        self._sdk = sdk
        self._timeout_s = timeout_s

    @classmethod
    def from_settings(
        cls, settings: ComposioSettings, *, http_client: httpx.Client | None = None
    ) -> ComposioConnector:
        """``http_client`` is for tests: the SDK sends every request through it."""
        if settings.api_key is None:
            msg = "ComposioConnector needs COMPOSIO__API_KEY"
            raise ConfigError(msg)
        config: SDKConfig = {
            "api_key": settings.api_key.get_secret_value(),
            "allow_tracking": False,
            "timeout": math.ceil(settings.request_timeout_s),
            "max_retries": _SDK_MAX_RETRIES,
            "logging_level": LogLevel.WARNING,
            "toolkit_versions": {TOOLKIT: TOOLKIT_VERSION},
        }
        if http_client is not None:
            config["http_client"] = http_client
        return cls(Composio(**config), timeout_s=settings.request_timeout_s)

    async def _call[T](self, fn: Callable[[], T]) -> T:
        try:
            with anyio.fail_after(self._timeout_s):
                # abandon_on_cancel: past the deadline the request is left to finish on its
                # thread rather than holding the caller.
                return await anyio.to_thread.run_sync(
                    _untracked, fn, abandon_on_cancel=True, limiter=SDK_LIMITER
                )
        except TimeoutError:
            # Ours (fail_after), or the SDK's ComposioSDKTimeoutError, which is also one.
            raise CalendarProviderUnavailableError("Calendar provider timed out") from None
        except (composio_client.ComposioError, composio_exceptions.ComposioError) as exc:
            # No chaining: the SDK's message is the response body, and Sentry sends __cause__.
            raise _translate(exc) from None

    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        request = await self._call(
            lambda: self._sdk.connected_accounts.link(
                user_id, auth_config_id, callback_url=callback_url, allow_multiple=True
            )
        )
        redirect_url = request.redirect_url
        # We 303 the browser to it, so it must be a page, not a scheme of someone's choosing.
        if not isinstance(redirect_url, str) or not redirect_url.startswith("https://"):
            raise CalendarProviderRejectedError(None)
        return redirect_url

    async def get_account(self, connected_account_id: str) -> ConnectedAccount:
        record = await self._call(
            lambda: self._sdk.connected_accounts.get(nanoid=connected_account_id)
        )
        try:
            return ConnectedAccount(
                id=str(record.id),
                user_id=str(record.user_id),
                auth_config_id=str(record.auth_config.id),
                status=str(record.status),
            )
        except AttributeError, TypeError:
            raise CalendarProviderRejectedError(None) from None

    async def _execute(
        self, slug: str, arguments: dict[str, object], account_id: str, user_id: str
    ) -> Any:
        """Run one tool on the caller's account; return its payload or raise ours."""
        result = await self._call(
            lambda: self._sdk.tools.execute(
                slug,
                arguments,
                connected_account_id=account_id,
                user_id=user_id,
                version=TOOLKIT_VERSION,
            )
        )
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise CalendarProviderRejectedError(None)
        if result.get("successful") is not True:
            # Google's own error comes back as a 200 with its status in the data.
            status = data.get("status_code")
            if status == 401:
                raise CalendarNotConnectedError(status)
            raise _from_status(status if isinstance(status, int) else None)
        # CREATE_EVENT's docs put the event under response_data; accept either.
        return data.get("response_data", data)

    async def list_events(
        self, account_id: str, user_id: str, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]:
        arguments: dict[str, object] = {
            "calendarId": "primary",
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "singleEvents": True,  # recurring events as their instances
            "orderBy": "startTime",
        }
        # shortcut: one page (Google's default 250 events), nextPageToken ignored; plenty for a
        # day or a week. Follow the token if someone asks for a month of a busy calendar.
        payload = await self._execute(LIST_EVENTS, arguments, account_id, user_id)
        try:
            return [_event(item, start.tzinfo) for item in payload["items"]]
        except KeyError, TypeError, ValueError:
            raise CalendarProviderRejectedError(None) from None

    async def create_event(
        self, account_id: str, user_id: str, event: CalendarEvent
    ) -> CalendarEvent:
        start, end = _rfc3339(event.start), _rfc3339(event.end)
        arguments: dict[str, object] = {
            "calendar_id": "primary",
            "summary": event.title,
            "create_meeting_room": False,  # its default adds a Google Meet link to every event
        }
        if event.all_day:
            arguments["start_date"] = event.start.date().isoformat()
            arguments["end_date"] = event.end.date().isoformat()  # exclusive, as Google's is
        else:
            arguments["start_datetime"] = start
            arguments["end_datetime"] = end
        if isinstance(event.start.tzinfo, ZoneInfo):
            # The offsets fix the instant; the zone is what Google shows when it is edited.
            arguments["timezone"] = event.start.tzinfo.key
        if event.location:
            arguments["location"] = event.location
        payload = await self._execute(CREATE_EVENT, arguments, account_id, user_id)
        try:
            return _event(payload, event.start.tzinfo)
        except KeyError, TypeError, ValueError:
            raise CalendarProviderRejectedError(None) from None


__all__ = ["ComposioConnector", "ConnectedAccount"]
