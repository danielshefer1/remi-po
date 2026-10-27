"""Reading whether an unaddressed group message okays sharing (#51)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from personal_organizer.messaging.groups.approval import approves
from tests.fixtures.llm import ScriptedLLM, said


@pytest.mark.parametrize("answer", ["YES", " yes.\n"])
async def test_yes_is_true(answer: str) -> None:
    llm = ScriptedLLM(said(answer))

    assert await approves("whatever they wrote", llm=llm) is True
    [[system, user]] = llm.calls
    assert system.role == "system"
    assert (user.role, user.content) == ("user", "whatever they wrote")


@pytest.mark.parametrize("answer", ["NO", "", "YES, I think", "Maybe"])
async def test_anything_else_is_false(answer: str) -> None:
    assert await approves("whatever they wrote", llm=ScriptedLLM(said(answer))) is False


async def test_truncated_is_false() -> None:
    cut = replace(said("YES"), finish_reason="max_tokens")
    assert await approves("whatever they wrote", llm=ScriptedLLM(cut)) is False
