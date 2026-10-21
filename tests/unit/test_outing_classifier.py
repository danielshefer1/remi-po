"""Reading an unaddressed group message against the open outings (Iteration 07)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from personal_organizer.db.models.group import GroupPoll
from personal_organizer.messaging.groups.outings import classify
from tests.fixtures.llm import ScriptedLLM, said


def _poll(title: str, day: int) -> GroupPoll:
    return GroupPoll(
        id=uuid4(),
        title=title,
        starts_at=datetime(2026, 10, day, 16, tzinfo=UTC),
        ends_at=datetime(2026, 10, day, 18, tzinfo=UTC),
        all_day=False,
        location=None,
        language="en",
    )


POLLS = (_poll("Family dinner", 23), _poll("Picnic", 24))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("IN 1", (POLLS[0].id, "in", None)),
        ("OUT 2: at work", (POLLS[1].id, "out", "at work")),
        ("OUT 1", (POLLS[0].id, "out", None)),
        ("OTHER", None),
        ("Sure, she's coming!", None),
        ("IN 3", None),
        ("OUT 0: busy", None),
    ],
)
async def test_classify(answer: str, expected: object) -> None:
    llm = ScriptedLLM(said(answer))

    assert await classify(POLLS, "whatever they wrote", llm=llm) == expected
    system, user = llm.calls[0]
    assert "1. Family dinner" in system.content
    assert "2. Picnic" in system.content
    assert user.content == "whatever they wrote"
