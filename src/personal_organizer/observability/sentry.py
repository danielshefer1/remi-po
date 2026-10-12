"""Sentry wiring.

Sentry does not go through structlog, so the log redaction chain gives it no protection at
all: it captures exceptions directly, and exception messages from asyncpg and Pydantic embed
the offending values. Both hooks below run the event through the *same* :func:`scrub_text`
the log redactor uses -- a separate implementation here is how the two silently drift apart.

They also share :func:`~personal_organizer.observability.redaction.is_opaque_id`, and for the
same reason. A Sentry event is mostly correlation identifiers -- ``event_id``, ``trace_id``,
``span_id``, ``release`` -- all of which are long hex strings that ``scrub_text``'s token rule
would otherwise replace with a marker, leaving an issue that cannot be tied to the log line
that produced it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.anthropic import AnthropicIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from personal_organizer.observability.redaction import (
    REDACTED_SECRET,
    SECRET_KEYS,
    is_opaque_id,
    scrub_text,
)
from personal_organizer.settings import Settings

_MAX_DEPTH = 8


def _scrub_keyed(key: Any, value: Any, depth: int) -> Any:
    """Scrub one mapping entry, using the key to decide.

    Two key-dependent cases, and they pull in opposite directions. A ``SECRET_KEYS`` name is
    dropped whatever it holds. An ``OPAQUE_ID_KEYS`` name is *kept* when the value has an
    identifier shape: a Sentry event's ``trace_id``, ``span_id``, ``event_id`` and ``release``
    are all long hex strings, so the token rule inside :func:`scrub_text` would rewrite every
    one of them to a marker and leave the issue impossible to tie back to a log line.
    """
    name = str(key).casefold()
    if name in SECRET_KEYS:
        return REDACTED_SECRET
    if is_opaque_id(name, value):
        return value
    return _scrub(value, depth + 1)


_STATE_MARKER = "<redacted:state>"


def _is_state_pair(value: Sequence[Any]) -> bool:
    """A ``["state", token]`` pair, as Sentry lists a query string. The name decides, not the
    value's shape: the signed state is the one parameter that must never leave."""
    return len(value) == 2 and isinstance(value[0], str) and value[0].casefold() == "state"


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth >= _MAX_DEPTH:
        return "<redacted:depth>"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {key: _scrub_keyed(key, item, depth) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        if _is_state_pair(value):
            return [value[0], _STATE_MARKER]
        return [_scrub(item, depth + 1) for item in value]
    return value


def scrub_event(event: Event, _hint: Hint) -> Event:
    """``before_send`` / ``before_send_transaction`` hook."""
    scrubbed: Event = _scrub(event)
    return scrubbed


def scrub_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(_scrub(crumb))


def init_sentry(settings: Settings) -> None:
    """Initialise Sentry. A no-op when no DSN is configured (local, CI)."""
    if settings.sentry.dsn is None:
        return

    sentry_sdk.init(
        dsn=settings.sentry.dsn.get_secret_value(),
        environment=settings.app.env,
        release=settings.app.release,
        # Never let Sentry attach request bodies, headers or user identifiers on its own.
        send_default_pii=False,
        max_request_body_size="never",
        # Defaults to True, and send_default_pii=False does not cover it: every captured
        # exception would ship its stack frames' local variables. In a webhook handler those
        # locals are the parsed payload, and scrub_text cannot recognise prose -- a calendar
        # title or a note would arrive in Sentry verbatim. This is the only layer that stops it.
        include_local_variables=False,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        profiles_sample_rate=settings.sentry.profiles_sample_rate,
        # Breadcrumbs from stdlib logging only; events come from explicit captures, so a
        # log line never becomes a second, unredacted copy of an error.
        integrations=[LoggingIntegration(level=None, event_level=None)],
        # Auto-enabled because ``anthropic`` is installed, it captures the SDK's own exceptions,
        # response bodies included, before the adapter translates them without chaining (D8).
        disabled_integrations=[AnthropicIntegration()],
        before_send=scrub_event,
        before_send_transaction=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )


__all__ = ["init_sentry", "scrub_breadcrumb", "scrub_event"]
