"""Inbound message processing.

One job per stored inbound message, deferred by the ingress transaction with
``lock=<sender hash>`` so a sender's messages are handled one at a time, in arrival order.
The job carries the inbox row id and nothing else -- the body is read back from the
database, per docs/adr/0001.

Retries are Procrastinate's, and only for failures that are definitely-not-sent, a lost
database connection, or an LLM, Composio or transcriber that is down (D3 of
docs/plan-iteration-04.md). One retry layer keeps every attempt visible in
``procrastinate_jobs``; an in-task retry loop would hide them and hold a worker slot while it
slept. The price is ordering: a job waiting to retry keeps its lock, so the same sender's next
message waits too. The backoff's waits add up to about
two minutes, and the seven attempts' own time comes on top: with the model timing out, 20 s each
(``AGENT__REQUEST_TIMEOUT_S``, no SDK retries), about four and a half minutes in all. On the last
attempt an LLM or Composio that is still down is answered with the fixed texts instead, so the
person never gets silence.
"""

from __future__ import annotations

import functools
from datetime import datetime
from typing import Final
from uuid import UUID

import procrastinate
import structlog
from sqlalchemy.exc import InterfaceError, OperationalError

from personal_organizer.core.errors import (
    CalendarProviderUnavailableError,
    LLMUnavailableError,
    TranscriptionUnavailableError,
    TransientChannelError,
)
from personal_organizer.db.engine import Database, get_database
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.agent import agent_turn
from personal_organizer.messaging.groups.gate import OnGroup
from personal_organizer.messaging.groups.turn import group_turn
from personal_organizer.messaging.inbound import InboxRow, OnAllowed, acknowledge, handle_inbound
from personal_organizer.messaging.onboarding import Advance
from personal_organizer.messaging.runtime import get_outbound_channel
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.providers.calendar.composio import ComposioConnector
from personal_organizer.providers.llm.anthropic import AnthropicLLM
from personal_organizer.providers.transcription.soniox import SonioxTranscriber
from personal_organizer.settings import get_settings
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

HANDLE_INBOUND_TASK: Final = "channel:handle_inbound"

#: Waits of 2, 4, 8, 16, 32 and 64 seconds: Procrastinate retries while the previous attempts
#: number fewer than ``max_attempts``, so a job runs at most seven times.
INBOUND_RETRY: Final = procrastinate.RetryStrategy(
    max_attempts=6,
    exponential_wait=2,
    retry_exceptions={
        TransientChannelError,
        LLMUnavailableError,
        CalendarProviderUnavailableError,
        TranscriptionUnavailableError,
        OperationalError,
        InterfaceError,
    },
)


def is_last_attempt(attempts: int) -> bool:
    """Whether a failure now is final: Procrastinate's own test in ``get_retry_decision``.
    ``attempts`` counts the previous runs, so the first run is 0."""
    return attempts >= (INBOUND_RETRY.max_attempts or 0)


@functools.cache
def agent_llm() -> AnthropicLLM | None:
    """One client for the process, built on first use: the agent is off unless enabled."""
    settings = get_settings()
    return AnthropicLLM.from_settings(settings) if settings.agent.enabled else None


@functools.cache
def strong_llm() -> AnthropicLLM | None:
    """The stronger model's client, built on first use: off unless the agent and it are set."""
    settings = get_settings()
    # An empty value is unset too: it would build a client on the chat model.
    if not settings.agent.enabled or not settings.models.strong_model:
        return None
    return AnthropicLLM.from_settings(
        settings, model=settings.models.strong_model, timeout=settings.agent.strong_timeout_s
    )


@functools.cache
def agent_calendar() -> ComposioConnector:
    """One Composio client for the process; the agent being on implies Composio is."""
    return ComposioConnector.from_settings(get_settings().composio)


@functools.cache
def voice_transcriber() -> SonioxTranscriber | None:
    """One client for the process, built on first use: voice is off unless enabled."""
    settings = get_settings()
    return SonioxTranscriber.from_settings(settings) if settings.voice.enabled else None


def _on_allowed(db: Database, *, last_attempt: bool) -> OnAllowed:
    """The agent's turn when it is enabled, else Iteration 02's acknowledgement."""
    llm = agent_llm()
    if llm is not None:
        return functools.partial(
            agent_turn,
            db=db,
            llm=llm,
            strong=strong_llm(),
            calendar=agent_calendar(),
            settings=get_settings(),
            last_attempt=last_attempt,
        )

    async def ack(
        row: InboxRow, channel: OutboundChannel, _tenant: TenantState, _now: datetime
    ) -> Advance | None:
        await acknowledge(row, channel, db=db)
        return None

    return ack


def _on_group(db: Database, *, last_attempt: bool) -> OnGroup | None:
    """The group turn when the agent is enabled; else group messages are purged unanswered."""
    llm = agent_llm()
    if llm is None:
        return None
    return functools.partial(
        group_turn, db=db, llm=llm, calendar=agent_calendar(), last_attempt=last_attempt
    )


def register(app: procrastinate.App) -> None:
    @app.task(
        queue=Queue.WEBHOOKS.value,
        name=HANDLE_INBOUND_TASK,
        retry=INBOUND_RETRY,
        pass_context=True,
    )
    async def handle_inbound_task(context: procrastinate.JobContext, inbox_id: str) -> None:
        db = get_database()
        settings = get_settings()
        last_attempt = is_last_attempt(context.job.attempts)
        await handle_inbound(
            UUID(inbox_id),
            db=db,
            channels=get_outbound_channel,
            # The invite list, whichever channel the message came in on.
            allowlist=settings.whatsapp.allowlist,
            on_allowed=_on_allowed(db, last_attempt=last_attempt),
            # Composio on: the tenant gate and onboarding. Off: Iteration 02's gate, exactly.
            settings=settings,
            llm=agent_llm(),
            transcriber=voice_transcriber(),
            last_attempt=last_attempt,
            on_group=_on_group(db, last_attempt=last_attempt),
        )


__all__ = [
    "HANDLE_INBOUND_TASK",
    "INBOUND_RETRY",
    "agent_calendar",
    "agent_llm",
    "is_last_attempt",
    "register",
    "strong_llm",
    "voice_transcriber",
]
