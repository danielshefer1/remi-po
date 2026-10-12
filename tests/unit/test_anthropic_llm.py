"""AnthropicLLM against the real, pinned SDK, with only HTTP stubbed.

As for Composio: what the SDK actually sends and raises is the part most likely to surprise,
so these run ``anthropic`` end to end over ``httpx2.MockTransport``. The request bodies are
checked against what Claude Haiku 5.5 refuses (sampling parameters, a prefill, a forced tool).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from personal_organizer.core.errors import ConfigError, LLMRejectedError, LLMUnavailableError
from personal_organizer.interfaces.llm import LLMProvider, Message, ToolCall
from personal_organizer.observability import langfuse
from personal_organizer.providers.llm.anthropic import AnthropicLLM
from personal_organizer.settings import AgentSettings, Settings

TOOL = {
    "name": "set_timezone",
    "description": "Save the time zone.",
    "input_schema": {"type": "object", "properties": {"zone": {"type": "string"}}},
}

Handler = Callable[[httpx2.Request], httpx2.Response]


def answer(*content: dict[str, Any], stop_reason: str = "end_turn") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": list(content),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 0,
            },
        },
    )


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}
TOOL_USE = {
    "type": "tool_use",
    "id": "toolu_1",
    "name": "set_timezone",
    "input": {"zone": "Asia/Jerusalem"},
}


class Api:
    """Scripted responses, in order, and every request body kept."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def agent_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "agent": AgentSettings(enabled=True, api_key=SecretStr("sk-ant-test")),
            "models": settings.models.model_copy(
                update={"chat_model": "claude-haiku-5-5", "max_retries": 0}
            ),
        }
    )


def llm(settings: Settings, api: Handler) -> AnthropicLLM:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(api))
    return AnthropicLLM.from_settings(settings, http_client=client)


SYSTEM = Message("system", "You are the onboarding assistant.")


def test_it_is_an_llm_provider(agent_settings: Settings) -> None:
    assert isinstance(llm(agent_settings, Api()), LLMProvider)


def test_it_needs_a_key(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        AnthropicLLM.from_settings(settings)


class TestRequest:
    async def test_what_haiku_5_5_accepts(self, agent_settings: Settings) -> None:
        api = Api(answer(text("Hi!")))
        await llm(agent_settings, api).complete(
            [SYSTEM, Message("user", "hello")], tools=[TOOL], max_tokens=1024
        )
        [body] = api.bodies
        assert body["model"] == "claude-haiku-5-5"
        assert body["max_tokens"] == 1024
        assert body["output_config"] == {"effort": "low"}
        assert body["system"] == [
            {
                "type": "text",
                "text": "You are the onboarding assistant.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert body["tools"] == [TOOL]
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        for refused in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
            assert refused not in body

    async def test_history_never_starts_or_ends_with_our_turn(
        self, agent_settings: Settings
    ) -> None:
        """A history cut at 20 rows can open with a reply; a prefill would be a 400."""
        api = Api(answer(text("ok")))
        await llm(agent_settings, api).complete(
            [
                SYSTEM,
                Message("assistant", "Which city are you in?"),
                Message("user", "Haifa"),
                Message("assistant", "Set."),
                Message("user", "thanks"),
            ],
            max_tokens=1024,
        )
        messages = api.bodies[0]["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert "tools" not in api.bodies[0]

    async def test_a_tool_round_sends_the_thinking_back(self, agent_settings: Settings) -> None:
        api = Api(answer(THINKING, TOOL_USE, stop_reason="tool_use"), answer(text("Done.")))
        target = llm(agent_settings, api)
        first = await target.complete([SYSTEM, Message("user", "Tel Aviv")], max_tokens=1024)
        assert first.tool_calls == (
            ToolCall(id="toolu_1", name="set_timezone", arguments={"zone": "Asia/Jerusalem"}),
        )
        await target.complete(
            [
                SYSTEM,
                Message("user", "Tel Aviv"),
                Message("assistant", first.content, first.tool_calls, native=first.native),
                Message("tool", "ok", tool_call_id="toolu_1"),
            ],
            max_tokens=1024,
        )
        assistant, results = api.bodies[1]["messages"][1:]
        assert [block["type"] for block in assistant["content"]] == ["thinking", "tool_use"]
        assert assistant["content"][0]["signature"] == "sig-1"
        assert results == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
        }

    async def test_tool_calls_without_native_content(self, agent_settings: Settings) -> None:
        """A provider-neutral assistant turn (another provider's, or a test's) still maps."""
        api = Api(answer(text("ok")))
        call = ToolCall(id="toolu_9", name="set_timezone", arguments={"zone": "UTC"})
        await llm(agent_settings, api).complete(
            [
                Message("user", "utc"),
                Message("assistant", "Setting it.", (call,)),
                Message("tool", "ok", tool_call_id="toolu_9"),
                Message("tool", "also ok", tool_call_id="toolu_9"),
            ],
            max_tokens=1024,
        )
        _, assistant, results = api.bodies[0]["messages"]
        assert assistant["content"] == [
            {"type": "text", "text": "Setting it."},
            {"type": "tool_use", "id": "toolu_9", "name": "set_timezone", "input": {"zone": "UTC"}},
        ]
        assert len(results["content"]) == 2


class TestResponse:
    async def test_text_is_read_by_block_type(self, agent_settings: Settings) -> None:
        api = Api(answer(THINKING, text("Hello "), text("there")))
        completion = await llm(agent_settings, api).complete(
            [Message("user", "hi")], max_tokens=1024
        )
        assert completion.content == "Hello there"
        assert completion.tool_calls == ()
        assert (completion.tokens_in, completion.tokens_out) == (110, 5)
        assert (completion.model, completion.finish_reason) == ("claude-haiku-5-5", "end_turn")

    async def test_a_refusal_is_rejected(self, agent_settings: Settings) -> None:
        api = Api(answer(stop_reason="refusal"))
        with pytest.raises(LLMRejectedError):
            await llm(agent_settings, api).complete([Message("user", "hi")], max_tokens=1024)


ERROR_BODY = {"type": "error", "error": {"type": "x", "message": "PROMPT-ECHO"}}


class TestErrors:
    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (httpx2.Response(429, json=ERROR_BODY), LLMUnavailableError),
            (httpx2.Response(500, json=ERROR_BODY), LLMUnavailableError),
            (httpx2.Response(529, json=ERROR_BODY), LLMUnavailableError),
            (httpx2.Response(400, json=ERROR_BODY), LLMRejectedError),
            (httpx2.Response(401, json=ERROR_BODY), LLMRejectedError),
            (httpx2.ConnectError("refused"), LLMUnavailableError),
            (httpx2.ReadTimeout("slow"), LLMUnavailableError),
        ],
    )
    async def test_translated_without_chaining(
        self,
        agent_settings: Settings,
        failure: httpx2.Response | Exception,
        expected: type[Exception],
    ) -> None:
        """Sentry sends ``__cause__``; the SDK's message is the response body."""
        with pytest.raises(expected) as excinfo:
            await llm(agent_settings, Api(failure)).complete(
                [Message("user", "hi")], max_tokens=1024
            )
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__
        assert "PROMPT-ECHO" not in str(excinfo.value)

    async def test_a_rejection_keeps_its_status(self, agent_settings: Settings) -> None:
        with pytest.raises(LLMRejectedError) as excinfo:
            await llm(agent_settings, Api(httpx2.Response(400, json=ERROR_BODY))).complete(
                [Message("user", "hi")], max_tokens=1024
            )
        assert excinfo.value.status_code == 400


class FakeGeneration:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def __enter__(self) -> FakeGeneration:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def update(self, **fields: Any) -> None:
        self.updates.append(fields)


class FakeLangfuse:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.generation = FakeGeneration()

    def start_as_current_observation(self, **fields: Any) -> FakeGeneration:
        self.opened.append(fields)
        return self.generation


async def test_each_call_is_a_langfuse_generation(
    agent_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse, "_client", fake)
    api = Api(answer(TOOL_USE, stop_reason="tool_use"))
    await llm(agent_settings, api).complete([Message("user", "hi")], max_tokens=1024)
    [opened] = fake.opened
    assert (opened["name"], opened["as_type"], opened["model"]) == (
        "llm.complete",
        "generation",
        "claude-haiku-5-5",
    )
    [update] = fake.generation.updates
    assert update["usage_details"] == {"input": 110, "output": 5}
    assert update["metadata"]["tools"] == ["set_timezone"]
