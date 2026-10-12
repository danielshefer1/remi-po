"""Fake LLM providers: a scripted one, and one whose every call fails the test."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from personal_organizer.interfaces.llm import Completion, Message, ToolCall


def said(content: str = "", *calls: ToolCall) -> Completion:
    """A completion: some text, and any tool calls."""
    return Completion(
        content=content,
        model="fake",
        tokens_in=1,
        tokens_out=1,
        finish_reason="tool_use" if calls else "end_turn",
        tool_calls=calls,
    )


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


class ScriptedLLM:
    """Answers each ``complete`` with the next scripted completion, or raises it."""

    def __init__(self, *script: Completion | Exception) -> None:
        self.script = list(script)
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int,
    ) -> Completion:
        self.calls.append(list(messages))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class NoLLM:
    """The default path costs no LLM call: anything reaching this fails the test."""

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int,
    ) -> Completion:
        msg = "the LLM was called on a path that must not call it"
        raise AssertionError(msg)
