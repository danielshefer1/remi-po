"""The agent's reminder and morning-message tools (Iteration 05), on an in-memory schedule.

No database: :func:`respond` is called with :class:`InMemorySchedule` and a scripted model.
Each tool's result is read from the tool message the model gets back.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.interfaces.llm import Completion
from personal_organizer.messaging.agent import agent_prompt, respond
from personal_organizer.messaging.schedule import InMemorySchedule
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from tests.fixtures.llm import ScriptedLLM, call, said

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)  # 12:00 in Jerusalem
TENANT = TenantState(
    id=UUID(int=1),
    status="active",
    step=None,
    language="he",
    first_message=False,
    timezone="Asia/Jerusalem",
)


async def _respond(schedule: InMemorySchedule | None, *script: Completion) -> ScriptedLLM:
    llm = ScriptedLLM(*script)
    await respond(
        [],
        "hi",
        TENANT,
        llm=llm,
        calendar=InMemoryCalendar(),
        account=None,
        now=NOW,
        schedule=schedule,
    )
    return llm


async def _seeded() -> InMemorySchedule:
    schedule = InMemorySchedule(NOW)
    await schedule.add("take the laundry out", datetime(2026, 10, 16, 6, 0, tzinfo=UTC))
    await schedule.add("call mom", datetime(2026, 10, 15, 15, 0, tzinfo=UTC))
    return schedule


async def test_set_reminder_stores_it_in_the_tenants_zone() -> None:
    schedule = InMemorySchedule(NOW)
    llm = ScriptedLLM(
        said("", call("set_reminder", text="call mom", at="2026-10-15T18:00")),
        said("Sure, I'll remind you at 6 to call mom."),
    )
    reply = await respond(
        [],
        "remind me at 6 to call mom",
        TENANT,
        llm=llm,
        calendar=InMemoryCalendar(),
        account=None,
        now=NOW,
        schedule=schedule,
    )
    assert reply == "Sure, I'll remind you at 6 to call mom."
    [r] = schedule.reminders
    assert r.text == "call mom"
    assert r.due_at == datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
    assert llm.calls[1][-1].content == "ok: set for 2026-10-15T18:00"


@pytest.mark.parametrize(
    "arguments",
    [
        {"text": "call mom", "at": "2026-10-15T11:00"},  # an hour ago
        {"text": "call mom", "at": "2028-01-01T09:00"},  # more than a year ahead
        {"text": "", "at": "2026-10-15T18:00"},
        {"text": "  ", "at": "2026-10-15T18:00"},
        {"text": "x" * 201, "at": "2026-10-15T18:00"},
        {"text": "call mom", "at": "tomorrow"},
        {"text": "call mom", "at": "2026-10-15T18:00+03:00"},
        {"at": "2026-10-15T18:00"},
    ],
)
async def test_set_reminder_refuses_the_past_and_far_future_and_bad_text(
    arguments: dict[str, Any],
) -> None:
    schedule = InMemorySchedule(NOW)
    llm = await _respond(schedule, said("", call("set_reminder", **arguments)), said("מתי?"))
    assert llm.calls[1][-1].content.startswith("error:")
    assert schedule.reminders == []


async def test_set_reminder_refuses_a_date_with_no_time() -> None:
    """Else "remind me on Friday" fires at midnight."""
    schedule = InMemorySchedule(NOW)
    at = call("set_reminder", text="call mom", at="2026-10-16")
    llm = await _respond(schedule, said("", at), said("מתי?"))
    assert llm.calls[1][-1].content == "error: at needs a time, e.g. 2026-10-16T09:00"
    assert schedule.reminders == []


async def test_list_reminders_shows_local_times_and_short_numbers() -> None:
    schedule = await _seeded()
    llm = await _respond(schedule, said("", call("list_reminders")), said("ok"))
    result = llm.calls[1][-1].content
    assert json.loads(result) == {
        "reminders": [
            {"n": 1, "text": "call mom", "at": "2026-10-15T18:00"},
            {"n": 2, "text": "take the laundry out", "at": "2026-10-16T09:00"},
        ]
    }
    assert all(str(r.id) not in result for r in schedule.reminders)


async def test_cancel_needs_a_list_in_this_turn() -> None:
    schedule = await _seeded()
    llm = await _respond(schedule, said("", call("cancel_reminder", n=1)), said("ok"))
    assert llm.calls[1][-1].content == "error: call list_reminders first"
    assert {r.status for r in schedule.reminders} == {"pending"}


async def test_cancel_after_list_cancels_that_one() -> None:
    schedule = await _seeded()
    llm = await _respond(
        schedule,
        said("", call("list_reminders")),
        said("", call("cancel_reminder", n=5)),
        said("", call("cancel_reminder", n=2)),
        said("", call("cancel_reminder", n=2)),
        said("ok"),
    )
    assert llm.calls[2][-1].content == "error: no reminder 5 in the list"
    assert llm.calls[3][-1].content.startswith("ok:")
    assert llm.calls[4][-1].content.startswith("error:")  # already cancelled
    laundry, mom = schedule.reminders
    assert (laundry.status, mom.status) == ("cancelled", "pending")


@pytest.mark.parametrize(
    ("at", "expected"),
    [("08:00", time(8, 0)), ("04:00", time(4, 0)), ("22:00", time(22, 0)), ("off", None)],
)
async def test_set_briefing_moves_and_stops_it(at: str, expected: time | None) -> None:
    schedule = InMemorySchedule(NOW)
    llm = await _respond(schedule, said("", call("set_briefing", at=at)), said("ok"))
    assert llm.calls[1][-1].content.startswith("ok: morning message")
    assert schedule.briefing_at == expected


@pytest.mark.parametrize("at", ["23:00", "03:30", "eight", "08:00+02:00", 8])
async def test_set_briefing_refuses_other_times(at: object) -> None:
    schedule = InMemorySchedule(NOW)
    llm = await _respond(schedule, said("", call("set_briefing", at=at)), said("ok"))
    assert llm.calls[1][-1].content.startswith("error:")
    assert schedule.briefing_at == time(7, 30)


@pytest.mark.parametrize(
    "tool",
    [
        call("set_reminder", text="call mom", at="2026-10-15T18:00"),
        call("list_reminders"),
        call("cancel_reminder", n=1),
        call("set_briefing", at="08:00"),
    ],
)
async def test_without_a_schedule_the_tools_say_so(tool: Any) -> None:
    llm = await _respond(None, said("", tool), said("ok"))
    assert llm.calls[1][-1].content == "error: reminders are not available here"


def test_the_prompt_states_the_morning_message_time() -> None:
    assert "now set for 07:30" in agent_prompt("he", NOW, briefing_at=time(7, 30))
    off = agent_prompt("he", NOW, briefing_at=None)
    assert "They turned the morning message off." in off
    assert "now set for" not in off


async def test_respond_puts_the_tenants_morning_time_in_the_prompt() -> None:
    llm = ScriptedLLM(said("ok"))
    tenant = replace(TENANT, briefing_at=time(8, 15))
    await respond([], "hi", tenant, llm=llm, calendar=InMemoryCalendar(), account=None, now=NOW)
    assert "now set for 08:15" in llm.calls[0][0].content
