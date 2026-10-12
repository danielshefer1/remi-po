"""LLM provider interface.

Kept provider-neutral so a second provider is configuration plus an adapter rather than a
rewrite. Roles are ``system``, ``user``, ``assistant`` and ``tool`` (a tool's result, answering
``tool_call_id``). A tool is a ``{"name", "description", "input_schema"}`` dict, the schema
being JSON Schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    #: An assistant turn as the provider returned it, sent back unchanged. Claude's thinking
    #: blocks must go back with the tool calls they led to; nothing else reads this.
    native: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    tokens_in: int
    tokens_out: int
    finish_reason: str
    tool_calls: tuple[ToolCall, ...] = ()
    native: Any = field(default=None, compare=False)


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int,
    ) -> Completion: ...


__all__ = ["Completion", "LLMProvider", "Message", "ToolCall"]
