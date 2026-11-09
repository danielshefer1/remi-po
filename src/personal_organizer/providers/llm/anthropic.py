"""Claude, through Anthropic's SDK, as the :class:`~personal_organizer.interfaces.llm.LLMProvider`.

Written for Claude Haiku 5.5 (D1), which refuses two things older models took: sampling
parameters (``temperature``) and an assistant prefill both return a 400, so neither is ever
sent. Thinking is adaptive and on by default, so ``thinking`` is left out and
``output_config.effort`` is ``low``, which is enough for chat. The response can then open with
thinking blocks: text is read by block type, and an assistant turn that called tools is sent
back as the SDK returned it (``Message.native``), thinking included, as the API requires.

``stop_reason == "refusal"`` has no server-side fallback on Haiku. It is a
:class:`LLMRejectedError`, so the turn falls back to fixed text rather than retrying.

Hosted web tools (search, fetch) are typed tools, passed through as given. Their result blocks
go back exactly as received, inside ``Message.native``, because the API rejects a modified one;
only the reply text after the last web block counts, and the result URLs are reported apart.
Page text, titles and cited text are kept out of Langfuse. A turn with such a tool waits longer.

The system prompt is one block marked for caching. The SDK's errors are translated without
chaining (D8): their messages carry the response body, and Sentry sends ``__cause__``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

import anthropic
import httpx2
import structlog
from anthropic.types import MessageParam, TextBlockParam, ToolParam

from personal_organizer.core.errors import ConfigError, LLMRejectedError, LLMUnavailableError
from personal_organizer.interfaces.llm import Completion, Message, ToolCall
from personal_organizer.observability.langfuse import observation
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

#: Statuses worth another attempt later. Any 5xx (529 "overloaded" included) is too.
_TRANSIENT_STATUSES: Final = frozenset({408, 429})
_EFFORT: Final = "low"
_WEB_TIMEOUT: Final = 60.0
_WEB_BLOCKS: Final = frozenset(
    {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}
)
_WEB_RESULTS: Final = frozenset({"web_search_tool_result", "web_fetch_tool_result"})


def _tool_param(tool: Mapping[str, Any]) -> dict[str, Any]:
    if "type" in tool:
        return dict(tool)
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool["input_schema"],
    }


def _final_text(content: Sequence[Any]) -> str:
    """The text after the last web block: before it is only narration."""
    last = max((i for i, block in enumerate(content) if block.type in _WEB_BLOCKS), default=-1)
    return "".join(block.text for block in content[last + 1 :] if block.type == "text")


def _web_sources(content: Sequence[Any]) -> tuple[str, ...]:
    urls: list[str] = []
    for block in content:
        # A failed search or fetch has an object for ``content``, not a list.
        if block.type == "web_search_tool_result" and isinstance(block.content, list):
            urls.extend(result.url for result in block.content)
        elif block.type == "web_fetch_tool_result" and block.content.type == "web_fetch_result":
            urls.append(block.content.url)
    return tuple(dict.fromkeys(urls))


def _traced(blocks: Sequence[Any]) -> list[dict[str, Any]]:
    """Blocks for Langfuse without page text, titles, citations or encrypted content."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        item = block.to_dict() if hasattr(block, "to_dict") else dict(block)
        if item.get("type") in _WEB_RESULTS:
            item = {
                "type": item["type"],
                "tool_use_id": item.get("tool_use_id"),
                "content": "<omitted>",
            }
        elif item.get("type") == "text":
            item.pop("citations", None)
        out.append(item)
    return out


def _wire(messages: Sequence[Message]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Neutral messages as the Messages API's ``system`` blocks and ``messages``."""
    system: list[str] = []
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system.append(message.content)
        elif message.role == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            # Every result for one assistant turn goes in the single user turn after it.
            if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                wire[-1]["content"].append(result)
            else:
                wire.append({"role": "user", "content": [result]})
        elif message.native is not None:
            wire.append({"role": "assistant", "content": message.native})
        elif message.tool_calls:
            text = [{"type": "text", "text": message.content}] if message.content else []
            calls = [
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
                for call in message.tool_calls
            ]
            wire.append({"role": message.role, "content": text + calls})
        else:
            wire.append({"role": message.role, "content": message.content})
    # The API's first turn is the person's. History cut at its limit can start with ours.
    while wire and wire[0]["role"] != "user":
        wire.pop(0)
    # And the last: a trailing turn of ours is a prefill, which Haiku 5.5 refuses with a 400.
    # A paused web turn is the exception: the API resumes it from that last assistant turn.
    if not (messages and messages[-1].paused):
        while wire and wire[-1]["role"] != "user":
            wire.pop()
    if not system:
        return [], wire
    block = {"type": "text", "text": "\n\n".join(system), "cache_control": {"type": "ephemeral"}}
    return [block], wire


def _translate(exc: anthropic.AnthropicError) -> LLMRejectedError | LLMUnavailableError:
    if isinstance(exc, anthropic.APIConnectionError):  # APITimeoutError is one too
        return LLMUnavailableError("LLM provider unreachable")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return LLMUnavailableError(f"LLM provider answered {status}")
        return LLMRejectedError(status)
    return LLMRejectedError(None)


class AnthropicLLM:
    def __init__(self, client: anthropic.AsyncAnthropic, *, model: str) -> None:
        self._client = client
        self._model = model

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        model: str | None = None,
        timeout: float | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> AnthropicLLM:
        """``model``, ``timeout`` override the chat ones; ``http_client`` is for tests."""
        if settings.agent.api_key is None:
            msg = "AnthropicLLM needs AGENT__API_KEY"
            raise ConfigError(msg)
        client = anthropic.AsyncAnthropic(
            api_key=settings.agent.api_key.get_secret_value(),
            timeout=timeout or settings.agent.request_timeout_s,
            # The job retries (INBOUND_RETRY); the SDK's own would stack under each attempt.
            max_retries=0,
            http_client=http_client,
        )
        return cls(client, model=model or settings.models.chat_model)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int,
    ) -> Completion:
        system, wire = _wire(messages)
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": cast("list[MessageParam]", wire),
            "output_config": {"effort": _EFFORT},
        }
        if system:
            params["system"] = cast("list[TextBlockParam]", system)
        if tools:
            # tool_choice stays auto: a forced call skips thinking.
            params["tools"] = cast("list[ToolParam]", [_tool_param(t) for t in tools])
        # shortcut: a raised AGENT__STRONG_TIMEOUT_S is ignored on a web turn,
        # upgrade to max() if that is ever set above 60.
        client = (
            self._client.with_options(timeout=_WEB_TIMEOUT)
            if any("type" in t for t in tools or ())
            else self._client
        )
        traced = [
            {**m, "content": _traced(m["content"])}
            if m["role"] == "assistant" and isinstance(m["content"], list)
            else m
            for m in wire
        ]
        with observation(
            "llm.complete", as_type="generation", model=self._model, input=traced
        ) as generation:
            try:
                response = await client.messages.create(**params)
            except anthropic.AnthropicError as exc:
                raise _translate(exc) from None
            usage = response.usage
            tokens_in = (
                usage.input_tokens
                + (usage.cache_read_input_tokens or 0)
                + (usage.cache_creation_input_tokens or 0)
            )
            calls = tuple(
                ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                for block in response.content
                if block.type == "tool_use"
            )
            if generation is not None:
                generation.update(
                    output=_traced(response.content),
                    usage_details={"input": tokens_in, "output": usage.output_tokens},
                    metadata={
                        "finish_reason": response.stop_reason,
                        "tools": [call.name for call in calls],
                    },
                )
        log.info(
            "llm.completed",
            model=response.model,
            tokens_in=tokens_in,
            tokens_out=usage.output_tokens,
            finish_reason=response.stop_reason,
        )
        if response.stop_reason == "refusal":
            raise LLMRejectedError(None)
        return Completion(
            content=_final_text(response.content),
            model=response.model,
            tokens_in=tokens_in,
            tokens_out=usage.output_tokens,
            finish_reason=response.stop_reason or "",
            tool_calls=calls,
            native=response.content,
            web_sources=_web_sources(response.content),
        )


__all__ = ["AnthropicLLM"]
