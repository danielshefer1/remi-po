"""One LLM turn: the prompt, the history, the person's message, and the tool loop (D7).

Shared by onboarding now and by the agent for active tenants later. The caller owns the
tools: it passes their schemas and ``run_tool``, which gets a call's name and arguments and
returns the text the model reads back. A tool that fails returns its error as that text, so
the model can say so or ask again; only the caller decides what a tool *did*.

Bounds: at most :data:`MAX_TOOL_ROUNDS` rounds of tool calls and :data:`MAX_TOKENS` per
completion. A model still calling tools after the last round has its text taken as the reply,
which may be empty, and a completion cut off at ``max_tokens`` is an empty reply; the caller
has a fallback for that.

With ``hebrew`` (the person wrote Hebrew, and Hebrew is their language), a reply with no Hebrew
in it is said again once, in Hebrew (#62). If the redo fails or still isn't Hebrew, the first
reply stands: a redo is never worth retrying the turn, whose tools have already run.

Provider errors pass through unchanged: :class:`~personal_organizer.core.errors.
LLMUnavailableError` is the job's to retry, and the caller falls back on the last attempt.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

import structlog

from personal_organizer.core.errors import LLMError
from personal_organizer.interfaces.llm import Completion, LLMProvider, Message
from personal_organizer.messaging.language import HEBREW, detect_language
from personal_organizer.observability.langfuse import observation

log = structlog.get_logger(__name__)

MAX_TOOL_ROUNDS: Final = 4
# Adaptive thinking counts against this too, and so does every tool call in one completion:
# a month of class reminders (~50 set_reminder calls) overran 4096.
MAX_TOKENS: Final = 16384

_SAY_IT_IN_HEBREW: Final = "Write that same reply in Hebrew. Send only the reply."

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
    hebrew: bool = False,
) -> str:
    """The model's reply to ``text``, after any tool calls it makes on the way."""
    messages = [Message("system", system), *history, Message("user", text)]
    with observation("conversation.turn"):
        rounds = 0
        while True:
            completion = await llm.complete(messages, tools=tools, max_tokens=MAX_TOKENS)
            if completion.finish_reason == "max_tokens":
                # Half a sentence or half a tool call's arguments: neither is worth using.
                log.info("conversation.truncated", rounds=rounds)
                return ""
            if not completion.tool_calls or rounds == MAX_TOOL_ROUNDS:
                if hebrew and completion.content.strip() and not completion.tool_calls:
                    return await _in_hebrew(messages, completion, tools=tools, llm=llm)
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


async def _in_hebrew(
    messages: list[Message],
    reply: Completion,
    *,
    tools: Sequence[Mapping[str, Any]],
    llm: LLMProvider,
) -> str:
    """``reply``, or the same said again in Hebrew when it is in English."""
    # "?" or "18:00" has no Hebrew either, and is fine as it is.
    if detect_language(reply.content) == HEBREW or not re.search("[A-Za-z]", reply.content):
        return reply.content
    log.info("conversation.redo_in_hebrew")
    asked = [
        *messages,
        Message("assistant", reply.content, native=reply.native),
        Message("user", _SAY_IT_IN_HEBREW),
    ]
    try:
        redo = await llm.complete(asked, tools=tools, max_tokens=MAX_TOKENS)
    except LLMError:
        return reply.content
    if redo.tool_calls or redo.finish_reason == "max_tokens":
        return reply.content
    return redo.content if detect_language(redo.content) == HEBREW else reply.content


__all__ = ["MAX_TOKENS", "MAX_TOOL_ROUNDS", "RunTool", "converse"]
