"""One LLM turn: the prompt, the history, the person's message, and the tool loop (D7).

Shared by onboarding now and by the agent for active tenants later. The caller owns the
tools: it passes their schemas and ``run_tool``, which gets a call's name and arguments and
returns the text the model reads back. A tool that fails returns its error as that text, so
the model can say so or ask again; only the caller decides what a tool *did*.

Bounds: at most :data:`MAX_TOOL_ROUNDS` rounds of tool calls and :data:`MAX_TOKENS` per
completion. A model still calling tools after the last round has its text taken as the reply,
which may be empty; the caller has a fallback for that.

Provider errors pass through unchanged: :class:`~personal_organizer.core.errors.
LLMUnavailableError` is the job's to retry, and the caller falls back on the last attempt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

import structlog

from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.observability.langfuse import observation

log = structlog.get_logger(__name__)

MAX_TOOL_ROUNDS: Final = 4
MAX_TOKENS: Final = 1024

#: ``(name, arguments) -> result text``.
RunTool = Callable[[str, Mapping[str, Any]], Awaitable[str]]


async def converse(
    history: Sequence[Message],
    text: str,
    *,
    system: str,
    tools: Sequence[Mapping[str, Any]],
    run_tool: RunTool,
    llm: LLMProvider,
) -> str:
    """The model's reply to ``text``, after any tool calls it makes on the way."""
    messages = [Message("system", system), *history, Message("user", text)]
    with observation("conversation.turn"):
        rounds = 0
        while True:
            completion = await llm.complete(messages, tools=tools, max_tokens=MAX_TOKENS)
            if not completion.tool_calls or rounds == MAX_TOOL_ROUNDS:
                return completion.content
            rounds += 1
            messages.append(
                Message(
                    "assistant",
                    completion.content,
                    completion.tool_calls,
                    native=completion.native,
                )
            )
            for call in completion.tool_calls:
                log.info("conversation.tool", tool_name=call.name)
                result = await run_tool(call.name, call.arguments)
                messages.append(Message("tool", result, tool_call_id=call.id))


__all__ = ["MAX_TOKENS", "MAX_TOOL_ROUNDS", "RunTool", "converse"]
