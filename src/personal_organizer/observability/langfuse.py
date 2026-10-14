"""Langfuse stub.

Iteration 01 only establishes the client and proves credentials resolve; real tracing with
redaction arrives in Iteration 04. Call sites can be decorated unconditionally because
:func:`get_langfuse` returns ``None`` when no keys are configured.

Defaults to the **EU** host: the data inventory for this product (phone numbers, message
content, calendar detail, long-term memories) makes residency a real constraint, not a
preference.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any

import structlog

from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

_client: Any | None = None


def init_langfuse(settings: Settings) -> Any | None:
    """Initialise the Langfuse client, or return ``None`` when unconfigured."""
    global _client  # module-level singleton, mirroring the SDK's own model

    if not settings.langfuse.enabled:
        log.debug("langfuse.disabled")
        _client = None
        return None

    assert settings.langfuse.public_key is not None
    assert settings.langfuse.secret_key is not None

    from langfuse import Langfuse

    _client = Langfuse(
        public_key=settings.langfuse.public_key.get_secret_value(),
        secret_key=settings.langfuse.secret_key.get_secret_value(),
        host=settings.langfuse.host,
        environment=settings.app.env,
        release=settings.app.release,
    )
    log.info("langfuse.initialised", env=settings.app.env)
    return _client


def get_langfuse() -> Any | None:
    return _client


def observation(name: str, **fields: Any) -> AbstractContextManager[Any]:
    """A Langfuse observation as the current span, or a no-op yielding ``None`` when off.

    Observations opened inside one nest under it, so one turn is one trace.
    """
    if _client is None:
        return nullcontext()
    manager: AbstractContextManager[Any] = _client.start_as_current_observation(name=name, **fields)
    return manager


def flush_langfuse() -> None:
    """Flush buffered spans. Safe to call when Langfuse was never initialised."""
    if _client is not None:
        _client.flush()


__all__ = ["flush_langfuse", "get_langfuse", "init_langfuse", "observation"]
