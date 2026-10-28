"""The shared tool loop, with a scripted LLM."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from personal_organizer.core.errors import LLMUnavailableError
from personal_organizer.interfaces.llm import Completion, Message
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


@pytest.mark.parametrize(
    "cut", [said("Two quick steps: first"), said("", call("set_timezone", zone="Asia/Jer"))]
)
async def test_a_reply_cut_off_at_max_tokens_is_empty(cut: Completion) -> None:
    """Half a sentence, or half a tool call: either way the caller's fallback answers."""
    tools = Tools()
    assert await run(ScriptedLLM(replace(cut, finish_reason="max_tokens")), tools) == ""
    assert tools.ran == []


async def test_provider_errors_pass_through() -> None:
    with pytest.raises(LLMUnavailableError):
        await run(ScriptedLLM(LLMUnavailableError("down")), Tools())


async def hebrew(llm: ScriptedLLM) -> str:
    return await converse(
        [], "אני פנוי מחר?", system="be brief", tools=[], run_tool=Tools(), llm=llm, hebrew=True
    )


async def test_an_english_reply_to_hebrew_is_redone_once_in_hebrew() -> None:
    llm = ScriptedLLM(said("Yes, you're free."), said("כן, את פנויה."))
    assert await hebrew(llm) == "כן, את פנויה."
    english, nudge = llm.calls[1][-2:]
    assert english == Message("assistant", "Yes, you're free.")
    assert nudge.role == "user"


async def test_a_redo_still_in_english_sends_the_first_reply() -> None:
    llm = ScriptedLLM(said("Yes, you're free."), said("Yes, free."))
    assert await hebrew(llm) == "Yes, you're free."
    assert len(llm.calls) == 2


@pytest.mark.parametrize("reply", ["כן, פנוי.", "", "18:00 👍"])
async def test_a_reply_not_in_english_is_not_redone(reply: str) -> None:
    llm = ScriptedLLM(said(reply))
    assert await hebrew(llm) == reply
    assert len(llm.calls) == 1


async def test_english_is_left_alone_unless_hebrew_is_asked() -> None:
    llm = ScriptedLLM(said("Yes, you're free."))
    assert await run(llm, Tools()) == "Yes, you're free."
