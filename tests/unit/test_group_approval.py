"""Reading whether an unaddressed group message okays sharing (#51)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging.groups.approval import approves
from tests.fixtures.llm import ScriptedLLM, said

CONTEXT = (
    Message("assistant", "Eden, I need your okay before I look at your calendar."),
    Message("user", "Dana: Eden, can you bring the cake?"),
)


@pytest.mark.parametrize("answer", ["YES", " yes.\n"])
async def test_yes_is_true(answer: str) -> None:
    llm = ScriptedLLM(said(answer))

    assert await approves("whatever they wrote", context=CONTEXT, llm=llm) is True
    [[system, *context, user]] = llm.calls
    assert system.role == "system"
    assert tuple(context) == CONTEXT
    assert (user.role, user.content) == ("user", "whatever they wrote")


@pytest.mark.parametrize("answer", ["NO", "", "YES, I think", "Maybe"])
async def test_anything_else_is_false(answer: str) -> None:
    assert (
        await approves("whatever they wrote", context=CONTEXT, llm=ScriptedLLM(said(answer)))
        is False
    )


async def test_truncated_is_false() -> None:
    cut = replace(said("YES"), finish_reason="max_tokens")
    assert await approves("whatever they wrote", context=CONTEXT, llm=ScriptedLLM(cut)) is False
