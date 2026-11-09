"""PII redaction for logs and for Sentry.

**Allowlist, not denylist.** A denylist over free-form ``**kwargs`` dies the first time
someone writes ``log.info("received", payload=msg)``. So any key not in :data:`SAFE_KEYS`
has its value replaced by a shape summary. The friction is the point: it is what makes
"logs contain no raw PII" an assertion rather than an aspiration.

Four layers, in order:

1. **Secret key names** (:data:`SECRET_KEYS`) -- non-overridable, wins over everything.
   This is "never store passwords, one-time codes or payment details", mechanised.
2. **Correlatable identifiers** (:data:`HASH_KEYS`) -- HMAC'd with a pepper and re-emitted
   under a ``*_hash`` key, so you keep "same user across two lines" without keeping the
   identifier.
3. **Allowlist** -- everything else is replaced.
4. **Value scrubbing** -- applied *even to allowlisted values*, because ``event``, ``path``
   and exception strings are allowlisted and routinely carry PII. The one exception is
   :data:`OPAQUE_ID_KEYS`: correlation ids are long alphanumeric strings, so the token rule
   would otherwise replace every one of them with a marker and leave nothing to correlate.
   Those keys skip text scrubbing only when the value matches an identifier shape exactly.

The same :func:`scrub_text` backs the Sentry ``before_send`` hook. One scrubber, two
consumers; wiring Sentry through a separate implementation is how the two drift apart.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

MAX_STR: Final = 512
#: How much of a value the rules below ever see. Outsiders choose some logged values (an
#: ``X-Request-ID``, a path), and every log line and Sentry event runs this on the event loop:
#: the cap bounds the work per value whatever a rule's worst case. Four times ``MAX_STR``
#: leaves room for the rules to shrink the text (a call string's dropped arguments) and still
#: fill the ``MAX_STR`` that is kept.
MAX_SCAN: Final = 4 * MAX_STR
TRUNCATED: Final = "...<truncated>"
MAX_DEPTH: Final = 4
MAX_ITEMS: Final = 50

REDACTED: Final = "<redacted>"
REDACTED_SECRET: Final = "<redacted:secret>"  # noqa: S105 - a marker, not a credential

#: Never logged under any circumstances, at any nesting depth.
SECRET_KEYS: Final = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "otp",
        "pin",
        "cvv",
        "card",
        "pan",
        "iban",
        "cookie",
        "credential",
        "private_key",
        "dsn",
        "access_token",
        "refresh_token",
        "client_secret",
    }
)

#: Identifiers that are useful for correlation but must not be stored in the clear.
HASH_KEYS: Final = frozenset(
    {
        "wa_id",
        "bsuid",
        "phone",
        "msisdn",
        "sender",
        "recipient",
        "email",
        "backup_email",
        "attendee_email",
        "from_",
        "to",
        # A WhatsApp message id is not a secret, but it is not opaque either: ``wamid.HBgL...``
        # is base64 over a structure that embeds the counterpart's phone number. Log the
        # inbox row's UUID to correlate, and let these hash if they are logged at all.
        "wamid",
        "provider_message_id",
    }
)

#: Free-text fields that carry user content. Never logged, never hashed.
CONTENT_KEYS: Final = frozenset(
    {
        "message",
        "body",
        "text",
        "content",
        "transcript",
        "title",
        "description",
        "summary",
        "note",
        "memory",
        "prompt",
        "completion",
        "payload",
    }
)

#: Everything permitted through. Anything absent here is replaced with a shape summary.
SAFE_KEYS: Final = frozenset(
    {
        # structlog / stdlib
        "event",
        "level",
        "logger",
        "timestamp",
        "exc_info",
        "exception",
        "stack",
        # service identity
        "service",
        "env",
        "component",
        "release",
        "hostname",
        # correlation
        "request_id",
        "trace_id",
        "span_id",
        # Sentry's id for an event, so a log line can point at the Sentry issue and back.
        "event_id",
        "tenant_id",
        "job_id",
        "session_id_hash",
        # http
        "method",
        "path",
        "route",
        "status_code",
        "duration_ms",
        "client_ip_hash",
        # jobs
        "task_name",
        "queue",
        "attempt",
        "priority",
        "lock",
        # messaging -- our own row ids, provider-neutral enums and counts. Never the
        # provider's message id (see HASH_KEYS) and never the sender (hashed as ``sender``).
        "channel",
        "inbox_id",
        "outbox_id",
        "connection_id",
        "message_type",
        "delivery_status",
        "disposition",
        "reason",
        "message_count",
        "status_count",
        "duplicate_count",
        "skipped_count",
        "whatsapp_enabled",
        "gowa_enabled",
        "composio_enabled",
        "channels",
        "allowlist_size",
        # db -- infrastructure coordinates, so that a connection failure says which host it
        # could not reach. The password lives in the DSN and never in these.
        "db_role",
        "db_host",
        "db_port",
        "db_name",
        "table",
        "rows",
        # llm
        "model",
        "provider",
        "tokens_in",
        "tokens_out",
        "text_count",
        "latency_ms",
        "cost_usd",
        "finish_reason",
        "tool_name",
        # errors -- type and code only, never the message
        "error_type",
        "error_code",
        "error_count",
    }
)

#: Allowlisted keys holding system-generated opaque identifiers -- UUIDs, hex digests,
#: release SHAs. They are allowlisted *for* correlation, but they are long and alphanumeric,
#: so ``_LONG_TOKEN`` (and, for a digit-heavy id, ``_DIGITS_RUN``) would otherwise rewrite
#: every one of them and correlate nothing with nothing. Values here skip text scrubbing
#: only when they genuinely look like an identifier -- see :func:`is_opaque_id` -- because
#: ``request_id`` is taken from an inbound header when one is present, so an allowlisted key
#: is not a trusted value.
OPAQUE_ID_KEYS: Final = frozenset(
    {
        "request_id",
        "trace_id",
        "span_id",
        # Sentry's own id for an event. Preserved for the same reason as the rest: it is the
        # handle that ties a Sentry issue back to the log line that produced it.
        "event_id",
        "tenant_id",
        "job_id",
        "session_id_hash",
        "client_ip_hash",
        "release",
        # Our own UUIDs for inbox and outbox rows: the handle a messaging log line carries
        # instead of the provider's message id.
        "inbox_id",
        "outbox_id",
        # Our UUID for a calendar connection: what the connect callback and the
        # onboarding:connected task log to tie a page view to the message that followed.
        "connection_id",
    }
)

#: Keys that must never be allowlisted. Asserted disjoint from SAFE_KEYS in the test suite.
FORBIDDEN_KEYS: Final = SECRET_KEYS | HASH_KEYS | CONTENT_KEYS

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{8,26}\b")
_DIGITS_RUN = re.compile(r"(?<![\w.])[+(]?\d[\d\s().-]{6,}\d\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b")
# Opaque-identifier shapes, anchored end to end: a dashed UUID, or a bare hex digest --
# uuid4().hex, a W3C trace or span id, a release commit SHA, a hash_identifier() digest.
_UUID_SHAPE = re.compile(r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_HEX_SHAPE = re.compile(r"\A[0-9a-fA-F]{16,64}\Z")
# Procrastinate interpolates Job.call_string -- "task_name[id](kwarg=<repr>, ...)" --
# straight into its log messages at INFO and ERROR. Those kwargs carry user content, and
# no content-detecting regex can save us there, so the argument list is dropped wholesale.
# The architectural counterpart is in docs/adr/0001: task kwargs carry ids, never content.
# Only the head is a regex, anchored by the lookbehind to the start of a name, so each name is
# tried once. The closing parenthesis is found with str.find in _drop_call_args: as a regex,
# ``\([^)]*\)`` would scan to the end of the text for every unclosed head, which is quadratic.
_CALL_STRING = re.compile(r"(?<![\w.:-])([\w.:-]+\[\d+\])\(")

# Onboarding links carry a bearer token in their path (/connect/<token>), and Composio's
# callback our signed state in its query (?state=<token>). The token rules below would catch
# today's shape by length; these catch it by position. A route template (/connect/{token}) and
# the callback path itself are kept.
# The same two shapes percent-encoded, as they appear in a ``next=`` value or a Referer, and in
# any case. A token never contains ``%``, so the value class stops at an encoded separator.
_CONNECT_TOKEN = re.compile(
    r"((?:/|%2F)connect(?:/|%2F))(?!callback\b|\{|%7B)[^/?#%&\s\"'<>]+", re.IGNORECASE
)
_STATE_PARAM = re.compile(
    r"((?:(?<![A-Za-z0-9])|(?<=%[0-9A-F][0-9A-F]))state(?:=|%3D))(?:(?!%26)[^&#\s\"'<>])+",
    re.IGNORECASE,
)


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _sub_digit_run(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "<redacted:pan>"
    if len(digits) >= 8:
        return "<redacted:phone>"
    return raw


#: What a phone number may be written with: the characters of ``_DIGITS_RUN``, plus ``+``.
_NUMBER_CHARS: Final = "0123456789+()-. \t\n\r\f\v"


def _drop_call_args(value: str) -> str:
    """``name[id](anything)`` becomes ``name[id](<redacted:args>)``, in linear time.

    Up to the first ``)``, as Procrastinate's repr'd kwargs may hold any character. A head
    with no ``)`` after it drops everything after it: its arguments run past the text, which
    is what happens when :func:`_scan_window` cuts a long call string short.
    """
    parts: list[str] = []
    position = 0
    while (head := _CALL_STRING.search(value, position)) is not None:
        parts.extend((value[position : head.start()], head.group(1), "(<redacted:args>)"))
        close = value.find(")", head.end())
        if close < 0:
            return "".join(parts)
        position = close + 1
    parts.append(value[position:])
    return "".join(parts)


def _scan_window(value: str) -> tuple[str, bool]:
    """The part of ``value`` the rules see, and whether anything was cut.

    The cut may split a phone number or a token into a piece too short for its rule, so the
    last whitespace-separated word of the window goes too. With no whitespace, nothing is kept.
    A number written in groups (``+31 6 1234 5678``) spans words, so a trailing run of number
    characters goes as well. ``rstrip``, not a regex: it is linear.
    """
    if len(value) <= MAX_SCAN:
        return value, False
    window = value[:MAX_SCAN]
    boundary = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    return window[: boundary + 1].rstrip(_NUMBER_CHARS), True


def scrub_text(value: str) -> str:
    """Remove PII-shaped substrings and cap the length.

    The length cap is load-bearing on its own: it means a transcript or a file body cannot
    land in a log line even if some future key is allowlisted by mistake. Only the first
    :data:`MAX_SCAN` characters are scrubbed at all, which bounds the cost (see there).
    """
    value, truncated = _scan_window(value)
    value = _drop_call_args(value)
    value = _CONNECT_TOKEN.sub(r"\1<redacted:link>", value)
    value = _STATE_PARAM.sub(r"\1<redacted:state>", value)
    value = _JWT.sub("<redacted:jwt>", value)
    value = _EMAIL.sub("<redacted:email>", value)
    value = _IBAN.sub("<redacted:iban>", value)
    value = _DIGITS_RUN.sub(_sub_digit_run, value)
    value = _LONG_TOKEN.sub("<redacted:token>", value)
    if len(value) > MAX_STR:
        value, truncated = value[:MAX_STR], True
    return value + TRUNCATED if truncated else value


@dataclass(frozen=True)
class Unredacted:
    """Wrapper that bypasses redaction. Honoured only when ``logging.allow_raw_pii`` is set,
    which the settings validator forbids in staging and production."""

    value: object


def hash_identifier(value: object, pepper: str) -> str:
    normalised = re.sub(r"\s+", "", str(value)).casefold()
    digest = hmac.new(pepper.encode(), normalised.encode(), sha256).hexdigest()
    return digest[:16]


def _shape(value: object) -> str:
    if isinstance(value, str):
        return f"<redacted:str[{len(value)}]>"
    if isinstance(value, Mapping):
        return f"<redacted:dict[{len(value)}]>"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return f"<redacted:list[{len(value)}]>"
    return f"<redacted:{type(value).__name__}>"


def _scrub_value(value: object, depth: int = 0) -> object:
    """Recursively scrub a value that has already been allowed through by key.

    The allowlist is *not* re-applied at depth: structures like structlog's
    ``dict_tracebacks`` output have arbitrary internal keys that we want to keep. Secret and
    identifier key names are still honoured at every depth.
    """
    if depth >= MAX_DEPTH:
        return _shape(value)
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bool | int | float | type(None)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, item in list(value.items())[:MAX_ITEMS]:
            key = str(raw_key).casefold()
            if key in SECRET_KEYS:
                out[str(raw_key)] = REDACTED_SECRET
            elif key in CONTENT_KEYS:
                out[str(raw_key)] = _shape(item)
            else:
                out[str(raw_key)] = _scrub_value(item, depth + 1)
        return out
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_scrub_value(item, depth + 1) for item in value[:MAX_ITEMS]]
    return scrub_text(str(value))


def _has_id_shape(value: str) -> bool:
    """True only for values that are certainly machine-generated identifiers.

    Anchored and narrow on purpose: anything that is not exactly a UUID or a hex digest
    falls through to :func:`scrub_text` and is redacted as usual, so a caller who puts a
    phone number in ``X-Request-ID`` gains nothing. All-digit values are excluded because a
    numeric run of this length is a phone or card number, not an identifier.
    """
    if value.isdigit():
        return False
    return bool(_UUID_SHAPE.match(value) or _HEX_SHAPE.match(value))


def is_opaque_id(key: str, value: object) -> bool:
    """True when ``value`` is a correlation id that must survive scrubbing intact.

    Public because :mod:`personal_organizer.observability.sentry` needs the same judgement:
    it scrubs Sentry events, which carry ``trace_id``, ``span_id``, ``event_id`` and
    ``release``, and without this it would rewrite every one of them to a marker -- exactly
    the defect this function exists to prevent on the log path.
    """
    return key.casefold() in OPAQUE_ID_KEYS and isinstance(value, str) and _has_id_shape(value)


def _scrub_allowlisted(key: str, value: object) -> object:
    """Scrub a value whose key is on the allowlist, keeping correlation ids intact."""
    if is_opaque_id(key, value):
        return value
    return _scrub_value(value)


def redact_event(
    event_dict: dict[str, Any], *, pepper: str, allow_raw: bool = False
) -> dict[str, Any]:
    """Apply the four layers to one structlog event dict."""
    out: dict[str, Any] = {}
    for raw_key, value in event_dict.items():
        key = str(raw_key).casefold()

        if allow_raw and isinstance(value, Unredacted):
            out[str(raw_key)] = value.value
            continue
        if isinstance(value, Unredacted):
            value = value.value  # unwrap, then redact normally

        if key in SECRET_KEYS:
            out[str(raw_key)] = REDACTED_SECRET
        elif key in HASH_KEYS:
            out[f"{raw_key}_hash"] = hash_identifier(value, pepper)
        elif key in SAFE_KEYS:
            out[str(raw_key)] = _scrub_allowlisted(key, value)
        else:
            out[str(raw_key)] = _shape(value)
    return out


def make_redactor(*, pepper: str, allow_raw: bool = False) -> Any:
    """Build the structlog processor."""

    def processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return redact_event(event_dict, pepper=pepper, allow_raw=allow_raw)

    return processor


__all__ = [
    "CONTENT_KEYS",
    "FORBIDDEN_KEYS",
    "HASH_KEYS",
    "MAX_SCAN",
    "MAX_STR",
    "OPAQUE_ID_KEYS",
    "SAFE_KEYS",
    "SECRET_KEYS",
    "TRUNCATED",
    "Unredacted",
    "hash_identifier",
    "is_opaque_id",
    "make_redactor",
    "redact_event",
    "scrub_text",
]
