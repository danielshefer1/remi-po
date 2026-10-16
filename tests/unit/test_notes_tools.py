"""The agent's notes tools and profile (Iteration 06), on in-memory notes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from personal_organizer.interfaces.llm import Completion
from personal_organizer.messaging.agent import (
    PROFILE_CHARS,
    agent_prompt,
    looks_secret,
    profile_lines,
    respond,
)
from personal_organizer.messaging.notes import MAX_NOTES, InMemoryNotes
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from tests.fixtures.llm import ScriptedLLM, call, said

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
TENANT = TenantState(
    id=UUID(int=1),
    status="active",
    step=None,
    language="en",
    first_message=False,
    timezone="Asia/Jerusalem",
)


async def _run(notes: InMemoryNotes | None, *script: Completion) -> ScriptedLLM:
    llm = ScriptedLLM(*script)
    await respond(
        [], "hi", TENANT, llm=llm, calendar=InMemoryCalendar(), account=None, now=NOW, notes=notes
    )
    return llm


def _result(llm: ScriptedLLM, turn: int = 1) -> str:
    """The tool result the model read at its ``turn``-th call."""
    return llm.calls[turn][-1].content


async def test_remember_saves_one_note() -> None:
    notes = InMemoryNotes()
    llm = await _run(notes, said("", call("remember", content="Wife: Dana")), said("Noted."))
    assert _result(llm).startswith("ok")
    assert [n.content for n in notes.notes] == ["Wife: Dana"]


@pytest.mark.parametrize("content", ["", "   ", "x" * 201])
async def test_remember_checks_its_argument(content: str) -> None:
    notes = InMemoryNotes()
    llm = await _run(notes, said("", call("remember", content=content)), said("ok"))
    assert _result(llm).startswith("error")
    assert notes.notes == []


@pytest.mark.parametrize(
    ("content", "secret"),
    [
        ("Mom's number: 052-123-4567", False),
        ("Card 4580 1234 5678 9012", True),
        ("Bank password is hunter2", True),
        ("The gate code is 4821", True),
        ("הקוד לשער 4821", True),
        ("הסיסמה למחשב", True),
        ("Zip area: מיקוד 6100000", False),
        ("Daughter Noa, ballet on Tuesdays", False),
        ("My PINs are 1234", True),
        ("passwords in 1Password", True),
        ("הסיסמאות שלי", True),
        ("בקוד 4821", True),
        ("סיסמת הוויפיי: dana2020", True),
        ("סיסמתי היא 1234", True),
        ("קודי הכניסה 1234", True),
        ("והקוד 1234", True),
        ("Phones 052-1234567 054-7654321", False),
        ("Kids born 2015 2018 2021 2023", False),
        ("4580 1234 5678 9012", True),
        ("4580123456789012", True),
        ("Dress code at work: business casual", False),
        ("Zip code 6100000", False),
        ("Pinchas is pinned to the board", False),
    ],
)
def test_secret_check(content: str, secret: bool) -> None:
    assert looks_secret(content) is secret


async def test_a_secret_is_refused_by_code() -> None:
    notes = InMemoryNotes()
    llm = await _run(notes, said("", call("remember", content="PIN 4821")), said("ok"))
    assert "secret" in _result(llm)
    assert notes.notes == []


async def test_a_full_list_asks_to_forget_one() -> None:
    notes = InMemoryNotes(*(f"note {i}" for i in range(MAX_NOTES)))
    llm = await _run(notes, said("", call("remember", content="one more")), said("ok"))
    assert "forget" in _result(llm)


async def test_list_and_forget_by_number_in_the_same_turn() -> None:
    notes = InMemoryNotes("Gym: Mondays", "Wife: Dana")
    llm = await _run(
        notes,
        said("", call("list_notes")),
        said("", call("forget", n=2)),
        said("Done."),
    )
    shown = json.loads(_result(llm, 1))["notes"]
    assert shown == [{"n": 1, "content": "Wife: Dana"}, {"n": 2, "content": "Gym: Mondays"}]
    assert _result(llm, 2).startswith("ok")
    assert [n.content for n in notes.notes] == ["Wife: Dana"]


async def test_forget_without_a_list_is_refused() -> None:
    notes = InMemoryNotes("Wife: Dana")
    llm = await _run(notes, said("", call("forget", n=1)), said("ok"))
    assert _result(llm) == "error: call list_notes first"
    assert len(notes.notes) == 1


async def test_without_notes_the_tools_say_so() -> None:
    llm = await _run(None, said("", call("remember", content="Wife: Dana")), said("ok"))
    assert _result(llm) == "error: notes are not available here"


async def test_the_profile_is_in_the_prompt_newest_first() -> None:
    notes = InMemoryNotes("Gym: Mondays", "Wife: Dana")
    llm = await _run(notes, said("Hi!"))
    system = llm.calls[0][0].content
    assert system.index("Wife: Dana") < system.index("Gym: Mondays")


def test_the_profile_is_cut_keeping_the_newest() -> None:
    lines = profile_lines([f"{i:03d} " + "x" * 96 for i in range(60)])  # newest first
    assert sum(len(line) + 3 for line in lines) <= PROFILE_CHARS
    assert len(lines) == 29  # 103 characters each with "- " and the newline
    assert lines[0].startswith("000")


def test_no_notes_reads_as_one_line() -> None:
    prompt = agent_prompt("en", NOW, None)
    assert "nothing yet" in prompt


async def test_a_note_with_a_newline_is_stored_as_one_line() -> None:
    notes = InMemoryNotes()
    await _run(notes, said("", call("remember", content=" Wife:\nDana \n")), said("ok"))
    assert [n.content for n in notes.notes] == ["Wife: Dana"]
