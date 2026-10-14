"""``po-chat``: its arguments, and scripted turns that touch no database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.admin.chat import (
    NO_ANSWER,
    NOT_ADDED,
    OK,
    _parser,
    agent_chat,
    chat,
    sample_events,
)
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging.agent import AGENT_REPLY
from personal_organizer.messaging.onboarding import REPLY, zone_ask
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.llm import ScriptedLLM, call, said

PHONE = "+31612345678"


def lines(*typed: str) -> Callable[[str], str]:
    queue = list(typed)

    def read(prompt: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read


class TestArguments:
    def test_the_tenant_is_a_uuid(self) -> None:
        tenant = uuid4()
        assert _parser().parse_args(["--tenant", str(tenant)]).tenant == tenant

    @pytest.mark.parametrize("argv", [[], ["--tenant", "not-a-uuid"]])
    def test_it_is_required_and_checked(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit):
            _parser().parse_args(argv)


async def test_scripted_turns_print_and_keep_history_in_memory(settings: Settings) -> None:
    llm = ScriptedLLM(said("Hi! Two quick steps."), said("A personal organizer."))
    printed: list[str] = []
    tenant = TenantState(
        id=UUID(int=1), status="onboarding", step="zone", language="en", first_message=True
    )
    # Neither turn issues a link, so nothing reaches the database.
    no_db = cast(Database, cast(Any, None))
    result = await chat(
        no_db,
        settings,
        tenant,
        PHONE,
        llm=llm,
        read=lines("hi", "", "what is this?"),
        write=printed.append,
    )
    assert result == OK
    choice = zone_ask("en", "Europe/Amsterdam")
    assert printed == [
        f"bot [{REPLY}]> Hi! Two quick steps.\n\n{choice}",
        f"bot [{REPLY}]> A personal organizer.\n\n{choice}",
    ]
    assert llm.calls[1][1:] == [
        Message("user", "hi"),
        Message("assistant", f"Hi! Two quick steps.\n\n{choice}"),
        Message("user", "what is this?"),
    ]


class TestActiveTenants:
    def test_fake_calendar_is_a_flag(self) -> None:
        tenant = str(uuid4())
        assert _parser().parse_args(["--tenant", tenant]).fake_calendar is False
        assert _parser().parse_args(["--tenant", tenant, "--fake-calendar"]).fake_calendar

    def test_the_sample_events_are_in_the_tenant_zone(self) -> None:
        zone = ZoneInfo("Asia/Jerusalem")
        events = sample_events(datetime(2026, 10, 12, 22, 0, tzinfo=UTC), zone)
        # 22:00 UTC is already the 13th in Jerusalem: "today" is the tenant's.
        assert min(event.start for event in events).date().isoformat() == "2026-10-13"
        assert all(event.start.tzinfo is zone for event in events)

    async def test_scripted_turns_read_the_calendar(self) -> None:
        tenant = TenantState(
            id=UUID(int=1),
            status="active",
            step=None,
            language="en",
            first_message=False,
            timezone="Asia/Jerusalem",
        )
        calendar = InMemoryCalendar()
        account = CalendarConnection(connected_account_id="fake", composio_user_id="t")
        llm = ScriptedLLM(
            said("", call("list_events", start="2026-10-13T00:00", end="2026-10-14T00:00")),
            said("Nothing tomorrow."),
            said(""),
        )
        printed: list[str] = []
        result = await agent_chat(
            tenant,
            llm=llm,
            calendar=calendar,
            account=account,
            read=lines("what's on tomorrow?", "and then?"),
            write=printed.append,
        )
        assert result == OK
        assert printed == [
            f"bot [{AGENT_REPLY}]> Nothing tomorrow.",
            f"bot [{AGENT_REPLY}]> {NO_ANSWER}",
        ]
        assert len(calendar.calls) == 1
        assert llm.calls[2][1:] == [
            Message("user", "what's on tomorrow?"),
            Message("assistant", "Nothing tomorrow."),
            Message("user", "and then?"),
        ]

    @pytest.mark.parametrize("fake", [True, False])
    async def test_a_proposal_waits_for_the_next_line(self, fake: bool) -> None:
        tenant = TenantState(
            id=UUID(int=1),
            status="active",
            step=None,
            language="en",
            first_message=False,
            timezone="Asia/Jerusalem",
        )
        memory = InMemoryCalendar()
        # Anything but the in-memory calendar stands for Google, which po-chat never writes.
        calendar = memory if fake else cast(Any, object())
        account = CalendarConnection(connected_account_id="fake", composio_user_id="t")
        dentist = call(
            "propose_event", title="Dentist", start="2026-10-13T09:00", end="2026-10-13T10:00"
        )
        llm = ScriptedLLM(
            said("", dentist),
            said("Shall I?"),
            said("ADD"),
            said("", dentist),
            said("Again?"),
            said("CANCEL"),
            said(""),
        )
        printed: list[str] = []
        await agent_chat(
            tenant,
            llm=llm,
            calendar=calendar,
            account=account,
            read=lines("add the dentist tomorrow at 9", "yes", "and again", "no", "yes"),
            write=printed.append,
        )
        summary = "Dentist\nTuesday 2026-10-13, 09:00-10:00"
        added = t("event_added", "en", summary=summary) if fake else NOT_ADDED
        assert printed[1:] == [
            f"bot [{AGENT_REPLY}]> {added}",
            printed[2],
            f"bot [{AGENT_REPLY}]> {t('event_cancelled', 'en')}",
            f"bot [{AGENT_REPLY}]> {NO_ANSWER}",
        ]
        assert printed[0].endswith("Add this to your calendar?")
        assert len(memory.events) == (1 if fake else 0)
        # "yes" with nothing waiting is the agent's, like any other line.
        assert len(llm.calls) == 7
