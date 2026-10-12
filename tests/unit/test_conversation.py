"""The shared tool loop, with a scripted LLM."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from personal_organizer.core.errors import LLMUnavailableError
from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging.conversation import MAX_TOOL_ROUNDS, converse
from tests.fixtures.llm import ScriptedLLM, call, said


class Tools:
    def __init__(self) -> None:
        self.ran: list[tuple[str, Mapping[str, Any]]] = []

    async def __call__(self, name: str, arguments: Mapping[str, Any]) -> str:
        self.ran.append((name, arguments))
        return f"ran {name}"


async def run(llm: ScriptedLLM, tools: Tools, history: list[Message] | None = None) -> str:
    return await converse(
        history or [], "hello", system="be brief", tools=[], run_tool=tools, llm=llm
    )


async def test_a_reply_without_tools_is_one_call() -> None:
    llm = ScriptedLLM(said("Hi!"))
    history = [Message("user", "earlier"), Message("assistant", "reply")]
    assert await run(llm, Tools(), history) == "Hi!"
    [messages] = llm.calls
    assert messages == [Message("system", "be brief"), *history, Message("user", "hello")]


async def test_tool_results_go_back_to_the_model() -> None:
    llm = ScriptedLLM(said("", call("set_language", language="en")), said("Switched."))
    tools = Tools()
    assert await run(llm, tools) == "Switched."
    assert tools.ran == [("set_language", {"language": "en"})]
    assistant, result = llm.calls[1][-2:]
    assert assistant.tool_calls == (call("set_language", language="en"),)
    assert result == Message("tool", "ran set_language", tool_call_id="call-set_language")


async def test_tool_rounds_are_bounded() -> None:
    looping = [said(f"round {n}", call("set_language", language="en")) for n in range(10)]
    llm = ScriptedLLM(*looping)
    tools = Tools()
    assert await run(llm, tools) == f"round {MAX_TOOL_ROUNDS}"
    assert len(tools.ran) == MAX_TOOL_ROUNDS
    assert len(llm.calls) == MAX_TOOL_ROUNDS + 1


async def test_provider_errors_pass_through() -> None:
    with pytest.raises(LLMUnavailableError):
        await run(ScriptedLLM(LLMUnavailableError("down")), Tools())
