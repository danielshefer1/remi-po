"""Claude, through Anthropic's SDK, as the :class:`~personal_organizer.interfaces.llm.LLMProvider`.

Written for Claude Haiku 5.5 (D1), which refuses two things older models took: sampling
parameters (``temperature``) and an assistant prefill both return a 400, so neither is ever
sent. Thinking is adaptive and on by default, so ``thinking`` is left out and
``output_config.effort`` is ``low``, which is enough for chat. The response can then open with
thinking blocks: text is read by block type, and an assistant turn that called tools is sent
back as the SDK returned it (``Message.native``), thinking included, as the API requires.

``stop_reason == "refusal"`` has no server-side fallback on Haiku. It is a
:class:`LLMRejectedError`, so the turn falls back to fixed text rather than retrying.

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
        cls, settings: Settings, *, http_client: httpx2.AsyncClient | None = None
    ) -> AnthropicLLM:
        """``http_client`` is for tests: the SDK sends every request through it."""
        if settings.agent.api_key is None:
            msg = "AnthropicLLM needs AGENT__API_KEY"
            raise ConfigError(msg)
        client = anthropic.AsyncAnthropic(
            api_key=settings.agent.api_key.get_secret_value(),
            timeout=settings.models.request_timeout_s,
            max_retries=settings.models.max_retries,
            http_client=http_client,
        )
        return cls(client, model=settings.models.chat_model)

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
            params["tools"] = cast(
                "list[ToolParam]",
                [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["input_schema"],
                    }
                    for t in tools
                ],
            )
        with observation(
            "llm.complete", as_type="generation", model=self._model, input=wire
        ) as generation:
            try:
                response = await self._client.messages.create(**params)
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
                    output=[block.to_dict() for block in response.content],
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
            content="".join(block.text for block in response.content if block.type == "text"),
            model=response.model,
            tokens_in=tokens_in,
            tokens_out=usage.output_tokens,
            finish_reason=response.stop_reason or "",
            tool_calls=calls,
            native=response.content,
        )


__all__ = ["AnthropicLLM"]
