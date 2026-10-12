"""Inbound message processing.

One job per stored inbound message, deferred by the ingress transaction with
``lock=<sender hash>`` so a sender's messages are handled one at a time, in arrival order.
The job carries the inbox row id and nothing else -- the body is read back from the
database, per docs/adr/0001.

Retries are Procrastinate's, and only for failures that are definitely-not-sent, a lost
database connection, or an LLM that is down (D3 of docs/plan-iteration-04.md). One retry layer
keeps every attempt visible in ``procrastinate_jobs``; an in-task retry loop would hide them
and hold a worker slot while it slept. The price is ordering: a job waiting to retry keeps its
lock, so the same sender's next message waits too. The backoff's waits add up to about two
minutes, and the seven attempts' own time comes on top: with the model timing out, 20 s each
(``AGENT__REQUEST_TIMEOUT_S``, no SDK retries), about four and a half minutes in all. On the last
attempt an LLM that is still down is answered with the fixed texts instead, so the person never
gets silence.
"""

from __future__ import annotations

import functools
from typing import Final
from uuid import UUID

import procrastinate
import structlog
from sqlalchemy.exc import InterfaceError, OperationalError

from personal_organizer.core.errors import LLMUnavailableError, TransientChannelError
from personal_organizer.db.engine import get_database
from personal_organizer.messaging.inbound import acknowledge, handle_inbound
from personal_organizer.messaging.runtime import get_outbound_channel
from personal_organizer.providers.llm.anthropic import AnthropicLLM
from personal_organizer.settings import get_settings
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

HANDLE_INBOUND_TASK: Final = "channel:handle_inbound"

#: Waits of 2, 4, 8, 16, 32 and 64 seconds: Procrastinate retries while the previous attempts
#: number fewer than ``max_attempts``, so a job runs at most seven times.
INBOUND_RETRY: Final = procrastinate.RetryStrategy(
    max_attempts=6,
    exponential_wait=2,
    retry_exceptions={TransientChannelError, LLMUnavailableError, OperationalError, InterfaceError},
)


def is_last_attempt(attempts: int) -> bool:
    """Whether a failure now is final: Procrastinate's own test in ``get_retry_decision``.
    ``attempts`` counts the previous runs, so the first run is 0."""
    return attempts >= (INBOUND_RETRY.max_attempts or 0)


@functools.cache
def _agent_llm() -> AnthropicLLM | None:
    """One client for the process, built on first use: the agent is off unless enabled."""
    settings = get_settings()
    return AnthropicLLM.from_settings(settings) if settings.agent.enabled else None


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
        await handle_inbound(
            UUID(inbox_id),
            db=db,
            channels=get_outbound_channel,
            # The invite list, whichever channel the message came in on.
            allowlist=settings.whatsapp.allowlist,
            on_allowed=functools.partial(acknowledge, db=db),
            # Composio on: the tenant gate and onboarding. Off: Iteration 02's gate, exactly.
            settings=settings,
            llm=_agent_llm(),
            last_attempt=is_last_attempt(context.job.attempts),
        )


__all__ = ["HANDLE_INBOUND_TASK", "INBOUND_RETRY", "is_last_attempt", "register"]
