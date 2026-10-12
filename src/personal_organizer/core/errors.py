"""Application error hierarchy.

Exception *messages* are never surfaced to users or to logs: database drivers embed the
offending values in them (`Key (phone)=(+3161...) already exists`), which is exactly the PII
the redaction layer exists to keep out. Handlers log `error_type` and `code`, never `str(exc)`.
"""


class AppError(Exception):
    """Base class for errors this application raises deliberately."""

    code: str = "app_error"


class ConfigError(AppError):
    """Settings are missing or internally inconsistent."""

    code = "config_error"


class MissingDatabaseRoleError(ConfigError):
    """No DSN was configured for the requested database role."""

    code = "missing_database_role"

    def __init__(self, role: str, *, reason: str | None = None) -> None:
        message = f"No DSN configured for database role {role!r}"
        super().__init__(f"{message}: {reason}" if reason else message)
        self.role = role


class BootstrapError(AppError):
    """The database bootstrap step failed a precondition."""

    code = "bootstrap_error"


class ChannelNotConfiguredError(ConfigError):
    """A messaging channel was used before it was configured for this process."""

    code = "channel_not_configured"


class ChannelError(AppError):
    """A messaging provider refused or failed a request.

    The subclasses are a *delivery* classification, not a transport one, because what the
    caller must decide is whether a retry could send the same message twice.
    """

    code = "channel_error"


class TransientChannelError(ChannelError):
    """Definitely not delivered, and worth retrying: throttled, 5xx, or never connected."""

    code = "channel_transient"


class RejectedChannelError(ChannelError):
    """Definitely not delivered, and a retry would be refused the same way."""

    code = "channel_rejected"

    def __init__(self, provider_code: int | None) -> None:
        super().__init__(f"Provider rejected the request (code {provider_code})")
        self.provider_code = provider_code


class AmbiguousDeliveryError(ChannelError):
    """The request may or may not have been delivered -- the response never arrived.

    Retrying risks a duplicate message; not retrying risks a missing one. See
    docs/adr/0003 for why this codebase takes the second risk.
    """

    code = "channel_ambiguous"


class MessageTooLongError(ChannelError):
    """The body exceeds the provider's limit. Split it first; nothing truncates silently."""

    code = "channel_message_too_long"

    def __init__(self, length: int, limit: int) -> None:
        super().__init__(f"Message of {length} characters exceeds the limit of {limit}")
        self.length = length
        self.limit = limit


class CalendarProviderError(AppError):
    """The calendar connector (Composio) refused or failed a request.

    Messages are fixed strings. The SDK's own name the user id and echo request URLs, and our
    callback URL carries a signed state.
    """

    code = "calendar_provider_error"


class CalendarProviderUnavailableError(CalendarProviderError):
    """Timed out, unreachable, throttled or a 5xx. The same request may work later."""

    code = "calendar_provider_unavailable"


class CalendarProviderRejectedError(CalendarProviderError):
    """Refused: a 4xx, an SDK-side refusal, or a response we cannot use. Retrying won't help."""

    code = "calendar_provider_rejected"

    def __init__(self, status_code: int | None) -> None:
        super().__init__(f"Calendar provider rejected the request (status {status_code})")
        self.status_code = status_code


class LLMError(AppError):
    """The LLM provider refused or failed a request.

    Messages are fixed strings. The SDK's own carry the response body, and a request it echoes
    carries the prompt: the person's messages.
    """

    code = "llm_error"


class LLMUnavailableError(LLMError):
    """Timed out, unreachable, throttled or a 5xx. The same request may work later."""

    code = "llm_unavailable"


class LLMRejectedError(LLMError):
    """Refused: a 4xx, a refusal by the model, or a response we cannot use. Retrying won't help."""

    code = "llm_rejected"

    def __init__(self, status_code: int | None) -> None:
        super().__init__(f"LLM provider rejected the request (status {status_code})")
        self.status_code = status_code


__all__ = [
    "AmbiguousDeliveryError",
    "AppError",
    "BootstrapError",
    "CalendarProviderError",
    "CalendarProviderRejectedError",
    "CalendarProviderUnavailableError",
    "ChannelError",
    "ChannelNotConfiguredError",
    "ConfigError",
    "LLMError",
    "LLMRejectedError",
    "LLMUnavailableError",
    "MessageTooLongError",
    "MissingDatabaseRoleError",
    "RejectedChannelError",
    "TransientChannelError",
]
