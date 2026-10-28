"""The agent's calendar tools and prompt (D5, D6): zone maths, what an event shows, argument
checks, and the confirm choice code puts under a proposal.

No database: :func:`read_calendar` and :func:`respond` are called with an in-memory calendar and
a scripted model.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import sentry_sdk
from structlog.testing import capture_logs

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
    LLMUnavailableError,
)
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.llm import ToolCall
from personal_organizer.messaging.agent import (
    MAX_WINDOW,
    TOOLS,
    add_event,
    agent_prompt,
    check_event,
    event_summary,
    read_calendar,
    respond,
    week_of,
)
from personal_organizer.messaging.calendar import InMemoryTenantCalendar
from personal_organizer.messaging.confirm import confirm_prompt
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.schedule import InMemorySchedule
from personal_organizer.messaging.tenancy import TenantState
from tests.fixtures.llm import ScriptedLLM, call, said

JERUSALEM = ZoneInfo("Asia/Jerusalem")
LONDON = ZoneInfo("Europe/London")
TENANT = TenantState(
    id=UUID(int=1),
    status="active",
    step=None,
    language="he",
    first_message=False,
    timezone="Asia/Jerusalem",
)


def _calendar(*events: CalendarEvent, connected: bool = True) -> InMemoryTenantCalendar:
    return InMemoryTenantCalendar(events, connected=connected)


async def _read(
    calendar: InMemoryTenantCalendar, start: object, end: object, zone: ZoneInfo = JERUSALEM
) -> str:
    return await read_calendar({"start": start, "end": end}, calendar=calendar, zone=zone)


def _event(start: datetime, end: datetime, **kw: Any) -> CalendarEvent:
    return CalendarEvent(external_id="evt-secret-id", title="Dentist", start=start, end=end, **kw)


class TestWeek:
    @pytest.mark.parametrize(
        ("day", "sunday"),
        [
            (date(2026, 10, 11), date(2026, 10, 11)),  # a Sunday is its own week's start
            (date(2026, 10, 12), date(2026, 10, 11)),  # Monday
            (date(2026, 10, 15), date(2026, 10, 11)),  # Thursday, the work week's last day
            (date(2026, 10, 17), date(2026, 10, 11)),  # Saturday still belongs to it
        ],
    )
    def test_weeks_start_on_sunday(self, day: date, sunday: date) -> None:
        assert week_of(day) == (sunday, sunday + timedelta(days=6))


class TestPrompt:
    def test_local_date_time_and_weekday(self) -> None:
        # 21:30 UTC on Monday is already Tuesday 00:30 in Jerusalem (IDT, +3).
        now = datetime(2026, 10, 12, 21, 30, tzinfo=UTC).astimezone(JERUSALEM)
        prompt = agent_prompt("he", now)
        assert "Tuesday 2026-10-13, 00:30, in their time zone, Asia/Jerusalem" in prompt
        assert "Sunday 2026-10-11 to Saturday 2026-10-17" in prompt
        assert "Sunday to Thursday" in prompt
        assert "Their language is Hebrew" in prompt

    async def test_respond_uses_the_tenant_zone(self) -> None:
        llm = ScriptedLLM(said("ok"))
        now = datetime(2026, 10, 12, 21, 30, tzinfo=UTC)
        await respond([], "hi", TENANT, llm=llm, calendar=_calendar(), now=now)
        assert "Tuesday 2026-10-13, 00:30" in llm.calls[0][0].content


class TestZone:
    async def test_naive_local_times_get_the_tenant_zone(self) -> None:
        calendar = _calendar()
        await _read(calendar, "2026-10-13T00:00", "2026-10-14T00:00")
        [(start, end)] = calendar.calls
        assert start.isoformat() == "2026-10-13T00:00:00+03:00"
        assert end.isoformat() == "2026-10-14T00:00:00+03:00"

    async def test_a_date_alone_is_midnight(self) -> None:
        calendar = _calendar()
        await _read(calendar, "2026-10-13", "2026-10-14")
        assert calendar.calls[0][0].isoformat() == "2026-10-13T00:00:00+03:00"

    @pytest.mark.parametrize(
        ("day", "offsets", "hours"),
        [
            ("2026-03-29", ("+00:00", "+01:00"), 23),  # clocks go forward
            ("2026-10-25", ("+01:00", "+00:00"), 25),  # clocks go back
        ],
    )
    async def test_a_day_across_a_dst_change(
        self, day: str, offsets: tuple[str, str], hours: int
    ) -> None:
        calendar = _calendar()
        following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        await _read(calendar, f"{day}T00:00", f"{following}T00:00", zone=LONDON)
        [(start, end)] = calendar.calls
        assert (start.isoformat()[-6:], end.isoformat()[-6:]) == offsets
        assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=hours)

    async def test_events_come_back_in_the_zone(self) -> None:
        calendar = _calendar(
            _event(
                datetime(2026, 10, 13, 6, 0, tzinfo=UTC),
                datetime(2026, 10, 13, 6, 30, tzinfo=UTC),
            )
        )
        result = json.loads(await _read(calendar, "2026-10-13T00:00", "2026-10-14T00:00"))
        assert result == {
            "events": [
                {
                    "title": "Dentist",
                    "start": "2026-10-13T09:00",
                    "end": "2026-10-13T09:30",
                    "all_day": False,
                }
            ]
        }

    async def test_an_event_after_the_dst_change_keeps_its_wall_time(self) -> None:
        # 09:00 BST is 08:00 UTC; the day after the change it is 09:00 UTC.
        calendar = _calendar(
            _event(
                datetime(2026, 3, 30, 8, 0, tzinfo=UTC),
                datetime(2026, 3, 30, 9, 0, tzinfo=UTC),
            )
        )
        result = json.loads(await _read(calendar, "2026-03-29", "2026-04-01", zone=LONDON))
        assert result["events"][0]["start"] == "2026-03-30T09:00"


class TestEventFields:
    async def test_title_times_location_only(self) -> None:
        start = datetime(2026, 10, 13, 20, 0, tzinfo=JERUSALEM)
        calendar = _calendar(_event(start, start + timedelta(hours=2), location="Tel Aviv"))
        [shown] = json.loads(await _read(calendar, "2026-10-13", "2026-10-14"))["events"]
        assert set(shown) == {"title", "start", "end", "all_day", "location"}
        assert shown["location"] == "Tel Aviv"

    async def test_an_all_day_event_shows_its_last_day(self) -> None:
        calendar = _calendar(
            _event(
                datetime(2026, 10, 13, tzinfo=JERUSALEM),
                datetime(2026, 10, 16, tzinfo=JERUSALEM),
                all_day=True,
            )
        )
        [shown] = json.loads(await _read(calendar, "2026-10-13", "2026-10-14"))["events"]
        assert (shown["start"], shown["end"], shown["all_day"]) == (
            "2026-10-13",
            "2026-10-15",
            True,
        )

    async def test_hebrew_titles_stay_readable(self) -> None:
        start = datetime(2026, 10, 13, 20, 0, tzinfo=JERUSALEM)
        calendar = _calendar(replace(_event(start, start), title="ארוחה עם נועה"))
        assert "ארוחה עם נועה" in await _read(calendar, "2026-10-13", "2026-10-14")


class TestArguments:
    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("tomorrow", "2026-10-14T00:00"),
            ("2026-10-13T00:00", "next week"),
            (20261013, "2026-10-14T00:00"),
            (None, "2026-10-14T00:00"),
            ("2026-10-13T00:00+03:00", "2026-10-14T00:00+03:00"),  # an offset: not local
            ("2026-13-01T00:00", "2026-13-02T00:00"),
        ],
    )
    async def test_anything_but_naive_iso_is_an_error(self, start: object, end: object) -> None:
        calendar = _calendar()
        assert (await _read(calendar, start, end)).startswith("error: ")
        assert calendar.calls == []

    @pytest.mark.parametrize("end", ["2026-10-13T00:00", "2026-10-12T09:00"])
    async def test_end_must_be_after_start(self, end: str) -> None:
        calendar = _calendar()
        assert await _read(calendar, "2026-10-13T00:00", end) == ("error: end must be after start")
        assert calendar.calls == []

    async def test_a_wide_window_is_capped(self) -> None:
        calendar = _calendar()
        result = json.loads(await _read(calendar, "2026-10-01T00:00", "2027-10-01T00:00"))
        [(start, end)] = calendar.calls
        assert end - start == MAX_WINDOW
        assert "62 days" in result["note"]

    async def test_a_window_within_the_cap_has_no_note(self) -> None:
        result = json.loads(await _read(_calendar(), "2026-10-01", "2026-12-02"))
        assert "note" not in result


class TestRespond:
    @staticmethod
    async def _respond(llm: ScriptedLLM, calendar: InMemoryTenantCalendar | None = None) -> str:
        return await respond(
            [],
            "מה יש לי מחר?",
            TENANT,
            llm=llm,
            calendar=calendar or _calendar(),
            now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
        )

    async def test_the_tool_result_reaches_the_model(self) -> None:
        llm = ScriptedLLM(
            said("", call("list_events", start="2026-10-13T00:00", end="2026-10-14T00:00")),
            said("אין לך כלום מחר."),
        )
        assert await self._respond(llm) == "אין לך כלום מחר."
        assert llm.calls[1][-1].content == '{"events": []}'

    async def test_list_events_without_google_reads_native(self) -> None:
        """Remi keeps their calendar: without Google, list_events reads it, and no line about
        connecting is added."""
        start = datetime(2026, 10, 13, 9, 0, tzinfo=JERUSALEM)
        calendar = _calendar(_event(start, start + timedelta(hours=1)), connected=False)
        llm = ScriptedLLM(
            said("", call("list_events", start="2026-10-13T00:00", end="2026-10-14T00:00")),
            said("מחר בתשע רופא שיניים."),
        )
        assert await self._respond(llm, calendar) == "מחר בתשע רופא שיניים."
        assert json.loads(llm.calls[1][-1].content)["events"][0]["title"] == "Dentist"

    async def test_no_tool_call_no_fixed_line(self) -> None:
        llm = ScriptedLLM(said("שלום!"))
        assert await self._respond(llm, _calendar(connected=False)) == "שלום!"

    @staticmethod
    async def _with_link(llm: ScriptedLLM, calendar: InMemoryTenantCalendar) -> str:
        async def connect_link() -> str:
            return "https://po.example/connect/TOKEN"

        return await respond(
            [],
            "מה יש לי מחר?",
            TENANT,
            llm=llm,
            calendar=calendar,
            now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
            connect_link=connect_link,
        )

    @pytest.mark.parametrize("connected", [False, True])
    async def test_connect_calendar_still_sends_link(self, connected: bool) -> None:
        """Asked for, it is sent even to a connected tenant: reconnecting is how a calendar
        that stopped working comes back."""
        llm = ScriptedLLM(said("", call("connect_calendar")), said("בשמחה."))
        line = t("calendar_not_connected", "he", url="https://po.example/connect/TOKEN")
        assert await self._with_link(llm, _calendar(connected=connected)) == f"בשמחה.\n\n{line}"
        assert llm.calls[1][-1].content.startswith("ok:")

    async def test_an_overlong_answer_gives_way_to_the_link(self) -> None:
        llm = ScriptedLLM(said("", call("connect_calendar")), said("x" * 5000))
        line = t("calendar_not_connected", "he", url="https://po.example/connect/TOKEN")
        assert await self._with_link(llm, _calendar(connected=False)) == line

    def test_the_prompt_says_what_google_does(self) -> None:
        now = datetime(2026, 10, 12, 12, 0, tzinfo=JERUSALEM)
        alone = agent_prompt("he", now, connected=False)
        hourly = agent_prompt("he", now)
        live = agent_prompt("he", now, live=True)
        assert "They haven't connected Google" in alone
        assert "about every hour" in hourly
        assert "call live_calendar if they want it" in hourly
        assert "check their Google Calendar live every time" in live
        assert "live_calendar with off" in live

    @pytest.mark.parametrize("text", ["see https://evil.example", "open [link]"])
    async def test_a_url_or_the_link_placeholder_drops_the_reply(self, text: str) -> None:
        assert await self._respond(ScriptedLLM(said(text))) == ""

    async def test_an_unknown_tool_is_an_error_the_model_reads(self) -> None:
        llm = ScriptedLLM(said("", call("delete_event", title="x")), said("ok"))
        assert await self._respond(llm) == "ok"
        assert llm.calls[1][-1].content == "error: unknown tool"

    async def test_an_unavailable_model_raises(self) -> None:
        with pytest.raises(LLMUnavailableError):
            await self._respond(ScriptedLLM(LLMUnavailableError("down")))


class TestLive:
    @staticmethod
    async def _respond(
        llm: ScriptedLLM, calendar: InMemoryTenantCalendar, schedule: InMemorySchedule
    ) -> str:
        return await respond(
            [], "תבדוק תמיד בגוגל", TENANT, llm=llm, calendar=calendar, now=NOW, schedule=schedule
        )

    async def test_live_calendar_on_and_off(self) -> None:
        calendar, schedule = _calendar(), InMemorySchedule(NOW)
        on = ScriptedLLM(said("", call("live_calendar", on=True)), said("ok"))
        await self._respond(on, calendar, schedule)
        assert on.calls[1][-1].content.startswith("ok: ")
        # This turn's later reads are live too.
        assert (schedule.live_calendar, calendar.live) == (True, True)
        off = ScriptedLLM(said("", call("live_calendar", on=False)), said("ok"))
        await self._respond(off, calendar, schedule)
        assert off.calls[1][-1].content.startswith("ok: ")
        assert (schedule.live_calendar, calendar.live) == (False, False)

    async def test_live_calendar_without_google_is_error(self) -> None:
        calendar, schedule = _calendar(connected=False), InMemorySchedule(NOW)
        llm = ScriptedLLM(said("", call("live_calendar", on=True)), said("ok"))
        await self._respond(llm, calendar, schedule)
        assert llm.calls[1][-1].content == (
            "error: they haven't connected Google; offer connect_calendar"
        )
        assert (schedule.live_calendar, calendar.live) == (False, False)

    async def test_on_must_be_true_or_false(self) -> None:
        calendar, schedule = _calendar(), InMemorySchedule(NOW)
        llm = ScriptedLLM(said("", call("live_calendar", on="yes")), said("ok"))
        await self._respond(llm, calendar, schedule)
        assert llm.calls[1][-1].content.startswith("error: ")
        assert schedule.live_calendar is False

    def test_the_tool_takes_on(self) -> None:
        [tool] = [tool for tool in TOOLS if tool["name"] == "live_calendar"]
        assert tool["input_schema"]["properties"]["on"]["type"] == "boolean"
        assert tool["input_schema"]["required"] == ["on"]


DENTIST = call(
    "propose_event", title="רופא שיניים", start="2026-10-13T09:00", end="2026-10-13T10:00"
)


class TestPropose:
    @staticmethod
    async def _respond(
        llm: ScriptedLLM,
        proposed: list[CalendarEvent],
        calendar: InMemoryTenantCalendar | None = None,
    ) -> str:
        return await respond(
            [],
            "תוסיף רופא שיניים מחר בתשע",
            TENANT,
            llm=llm,
            calendar=calendar or _calendar(),
            now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
            proposed=proposed,
        )

    async def test_code_puts_the_choice_under_the_model_words(self) -> None:
        proposed: list[CalendarEvent] = []
        calendar = _calendar()
        llm = ScriptedLLM(said("", DENTIST), said("להוסיף את זה?"))
        reply = await self._respond(llm, proposed, calendar=calendar)
        [event] = proposed
        assert (event.start.isoformat(), event.end.isoformat()) == (
            "2026-10-13T09:00:00+03:00",
            "2026-10-13T10:00:00+03:00",
        )
        summary = "\n".join(["רופא שיניים", "יום שלישי 2026-10-13, 09:00-10:00"])
        assert reply == f"להוסיף את זה?\n\n{confirm_prompt(summary, 'he')}"
        assert calendar.added == []  # nothing is written before the confirm
        assert llm.calls[1][-1].content.startswith("ok: proposed")

    async def test_a_dropped_reply_keeps_the_choice(self) -> None:
        proposed: list[CalendarEvent] = []
        llm = ScriptedLLM(said("", DENTIST), said("see https://evil.example\n1. Add"))
        reply = await self._respond(llm, proposed)
        assert reply.startswith("רופא שיניים\n")
        assert "http" not in reply
        assert len(proposed) == 1

    async def test_one_proposal_per_turn(self) -> None:
        proposed: list[CalendarEvent] = []
        other = call(
            "propose_event", title="other", start="2026-10-14T09:00", end="2026-10-14T10:00"
        )
        llm = ScriptedLLM(said("", DENTIST), said("", other), said("ok"))
        await self._respond(llm, proposed)
        assert [event.title for event in proposed] == ["רופא שיניים"]
        assert llm.calls[2][-1].content.startswith("error: one proposal")

    async def test_propose_without_google_is_allowed(self) -> None:
        proposed: list[CalendarEvent] = []
        llm = ScriptedLLM(said("", DENTIST), said("להוסיף?"))
        reply = await self._respond(llm, proposed, _calendar(connected=False))
        assert [event.title for event in proposed] == ["רופא שיניים"]
        assert llm.calls[1][-1].content.startswith("ok: proposed")
        assert reply.endswith(confirm_prompt("", "he").strip())

    async def test_an_invalid_proposal_is_an_error_the_model_reads(self) -> None:
        proposed: list[CalendarEvent] = []
        bad = call("propose_event", title="x", start="tomorrow", end="2026-10-13T10:00")
        llm = ScriptedLLM(said("", bad), said("מתי?"))
        assert await self._respond(llm, proposed) == "מתי?"
        assert proposed == []
        assert llm.calls[1][-1].content.startswith("error: ")


class TestCheckEvent:
    GOOD: ClassVar[dict[str, Any]] = {
        "title": "Dentist",
        "start": "2026-10-13T09:00",
        "end": "2026-10-13T10:00",
    }

    @pytest.mark.parametrize(
        "changed",
        [
            {"title": None},
            {"title": "  "},
            {"title": "x" * 201},
            {"location": 7},
            {"location": "x" * 201},
            {"all_day": "yes"},
            {"start": "tomorrow"},
            {"start": "2026-10-13T09:00+03:00"},  # an offset: not local
            {"end": None},
            {"end": "2026-10-13T09:00"},  # end == start
            {"end": "2026-10-13T08:00"},
            {"end": "2026-12-15T09:00"},  # 63 days
        ],
    )
    def test_bad_arguments_are_errors(self, changed: dict[str, Any]) -> None:
        result = check_event({**self.GOOD, **changed}, JERUSALEM)
        assert isinstance(result, str)
        assert result.startswith("error: ")

    def test_a_good_one(self) -> None:
        event = check_event({**self.GOOD, "title": " Dentist ", "location": "Haifa"}, JERUSALEM)
        assert event == CalendarEvent(
            "",
            "Dentist",
            datetime(2026, 10, 13, 9, tzinfo=JERUSALEM),
            datetime(2026, 10, 13, 10, tzinfo=JERUSALEM),
            location="Haifa",
        )

    def test_an_all_day_event_ends_the_midnight_after_its_last_day(self) -> None:
        event = check_event(
            {"title": "Sukkot", "start": "2026-10-13", "end": "2026-10-13", "all_day": True},
            JERUSALEM,
        )
        assert isinstance(event, CalendarEvent)
        assert (event.start, event.end, event.all_day) == (
            datetime(2026, 10, 13, tzinfo=JERUSALEM),
            datetime(2026, 10, 14, tzinfo=JERUSALEM),
            True,
        )


class TestSummary:
    def test_timed_in_the_zone_with_location(self) -> None:
        start = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)  # 20:00 in Jerusalem
        event = _event(start, start + timedelta(hours=2), location="Port Said")
        assert event_summary(event, JERUSALEM, "en") == (
            "Dentist\nThursday 2026-10-15, 20:00-22:00\nPort Said"
        )

    def test_across_midnight(self) -> None:
        start = datetime(2026, 10, 15, 23, 0, tzinfo=JERUSALEM)
        event = _event(start, start + timedelta(hours=2))
        assert event_summary(event, JERUSALEM, "he").splitlines()[1] == (
            "יום חמישי 2026-10-15 23:00 - יום שישי 2026-10-16 01:00"
        )

    @pytest.mark.parametrize(
        ("days", "when"),
        [(1, "Saturday 2026-10-17"), (2, "Saturday 2026-10-17 - Sunday 2026-10-18")],
    )
    def test_all_day(self, days: int, when: str) -> None:
        start = datetime(2026, 10, 17, tzinfo=JERUSALEM)
        event = _event(start, start + timedelta(days=days), all_day=True)
        assert event_summary(event, JERUSALEM, "en").splitlines()[1] == when


class TestAddEvent:
    EVENT = _event(
        datetime(2026, 10, 13, 9, tzinfo=JERUSALEM), datetime(2026, 10, 13, 10, tzinfo=JERUSALEM)
    )

    async def _add(self, calendar: InMemoryTenantCalendar) -> str:
        return await add_event(self.EVENT, calendar=calendar, zone=JERUSALEM, language="en")

    async def test_added_with_the_link(self) -> None:
        calendar = Linked()
        summary = event_summary(self.EVENT, JERUSALEM, "en")
        assert await self._add(calendar) == (
            f"{t('event_added', 'en', summary=summary)}"
            "\nhttps://www.google.com/calendar/event?eid=abc"
        )
        assert len(calendar.added) == 1

    @pytest.mark.parametrize("connected", [False, True])
    async def test_added_without_a_link(self, connected: bool) -> None:
        summary = event_summary(self.EVENT, JERUSALEM, "en")
        calendar = _calendar(connected=connected)
        assert await self._add(calendar) == t("event_added", "en", summary=summary)
        assert len(calendar.added) == 1

    @pytest.mark.parametrize(
        ("error", "key"),
        [
            (CalendarProviderUnavailableError("down"), "event_not_added"),
            (CalendarProviderRejectedError(400), "event_not_added"),
            (CalendarNotConnectedError(401), "calendar_disconnected"),
        ],
    )
    async def test_a_failed_create_is_a_fixed_line_never_a_raise(
        self, error: Exception, key: str
    ) -> None:
        calendar = _calendar()
        calendar.fail = error
        assert await self._add(calendar) == t(key, "en")


class Linked(InMemoryTenantCalendar):
    """Google hands back a link to the event it created."""

    async def add(self, event: CalendarEvent) -> CalendarEvent:
        stored = await super().add(event)
        return replace(stored, link="https://www.google.com/calendar/event?eid=abc")


NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


class _FakeGrants:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def let_see(self, name: str) -> str:
        self.asked.append(("let_see", name))
        return "ok"

    async def stop(self, name: str) -> str:
        self.asked.append(("stop", name))
        return "ok"

    async def stop_sharing(self) -> str:
        self.asked.append(("stop_sharing", ""))
        return "ok"


async def test_stop_sharing_calendar_reaches_the_grants_port() -> None:
    grants = _FakeGrants()
    llm = ScriptedLLM(said("", ToolCall("c1", "stop_sharing_calendar", {})), said("Done."))
    await respond([], "hi", TENANT, llm=llm, calendar=_calendar(), now=NOW, grants=grants)
    assert grants.asked == [("stop_sharing", "")]


async def test_without_grants_stop_sharing_calendar_says_so() -> None:
    llm = ScriptedLLM(said("", ToolCall("c1", "stop_sharing_calendar", {})), said("ok"))
    await respond([], "hi", TENANT, llm=llm, calendar=_calendar(), now=NOW)
    assert llm.calls[1][-1].content == "error: sharing details is not available here"


async def test_the_sharing_tools_reach_the_grants_port() -> None:
    grants = _FakeGrants()
    llm = ScriptedLLM(
        said(
            "",
            ToolCall("c1", "let_see_details", {"name": "Rina"}),
            ToolCall("c2", "stop_details", {"name": " Dana "}),
        ),
        said("Done."),
    )
    await respond([], "hi", TENANT, llm=llm, calendar=_calendar(), now=NOW, grants=grants)
    assert grants.asked == [("let_see", "Rina"), ("stop", " Dana ")]


async def test_without_grants_the_sharing_tools_say_so() -> None:
    llm = ScriptedLLM(said("", ToolCall("c1", "let_see_details", {"name": "Rina"})), said("ok"))
    await respond([], "hi", TENANT, llm=llm, calendar=_calendar(), now=NOW)
    assert llm.calls[1][-1].content == "error: sharing details is not available here"


TIMETABLE = call("hand_off", reason="timetable")


def _tool_names(llm: ScriptedLLM) -> list[str]:
    return [tool["name"] for tool in llm.tools[0] or ()]


class TestHandOff:
    @staticmethod
    async def _respond(
        llm: ScriptedLLM,
        strong: ScriptedLLM | None,
        proposed: list[CalendarEvent] | None = None,
        grants: _FakeGrants | None = None,
    ) -> str:
        return await respond(
            [],
            "הנה המערכת שלי, תזכיר לי לפני כל שיעור",
            TENANT,
            llm=llm,
            calendar=_calendar(),
            now=NOW,
            proposed=proposed,
            grants=grants,
            strong=strong,
        )

    async def test_hand_off_reruns_on_strong(self) -> None:
        cheap, strong = ScriptedLLM(said("", TIMETABLE)), ScriptedLLM(said("done"))
        assert await self._respond(cheap, strong) == "done"
        assert "hand_off" in _tool_names(cheap)
        assert "call hand_off first" in cheap.calls[0][0].content
        # The strong run starts the turn over, and can't hand it off again.
        assert strong.calls[0][-1].content == "הנה המערכת שלי, תזכיר לי לפני כל שיעור"
        assert "hand_off" not in _tool_names(strong)
        assert "hand_off" not in strong.calls[0][0].content

    async def test_hand_off_after_write_is_refused(self) -> None:
        grants = _FakeGrants()
        cheap = ScriptedLLM(
            said("", call("stop_sharing_calendar")), said("", TIMETABLE), said("Stopped.")
        )
        strong = ScriptedLLM()
        assert await self._respond(cheap, strong, grants=grants) == "Stopped."
        assert cheap.calls[2][-1].content == "error: too late to hand off; finish it yourself"
        assert grants.asked == [("stop_sharing", "")]  # written once, never again
        assert strong.calls == []

    async def test_no_strong_no_tool(self) -> None:
        cheap = ScriptedLLM(said("hi"))
        assert await self._respond(cheap, None) == "hi"
        assert "hand_off" not in _tool_names(cheap)
        assert "hand_off" not in cheap.calls[0][0].content

    async def test_proposal_from_cheap_run_is_dropped(self) -> None:
        proposed: list[CalendarEvent] = []
        other = call(
            "propose_event", title="other", start="2026-10-17T09:00", end="2026-10-17T10:00"
        )
        cheap = ScriptedLLM(said("", DENTIST), said("", TIMETABLE))
        strong = ScriptedLLM(said("", other), said("ok"))
        reply = await self._respond(cheap, strong, proposed=proposed)
        # The caller's own list holds the strong run's proposal only.
        assert [event.title for event in proposed] == ["other"]
        assert reply.startswith("ok\n\nother\n")
        assert "רופא שיניים" not in reply

    async def test_strong_unavailable_raises_for_retry(self) -> None:
        cheap = ScriptedLLM(said("", TIMETABLE))
        with pytest.raises(LLMUnavailableError):
            await self._respond(cheap, ScriptedLLM(LLMUnavailableError("down")))

    @pytest.mark.parametrize(
        ("reason", "logged"),
        [
            ("timetable", "timetable"),
            ("multi_step", "multi_step"),
            # The model's own words could repeat theirs: only a code is ever logged.
            ("Dana's chemo timetable", "other"),
            (3, "other"),
        ],
    )
    async def test_hand_off_logs_reason(
        self, monkeypatch: pytest.MonkeyPatch, reason: object, logged: str
    ) -> None:
        tags: list[tuple[str, str]] = []
        monkeypatch.setattr(sentry_sdk, "set_tag", lambda key, value: tags.append((key, value)))
        cheap = ScriptedLLM(said("", call("hand_off", reason=reason)))
        with capture_logs() as logs:
            await self._respond(cheap, ScriptedLLM(said("done")))
        [entry] = [entry for entry in logs if entry["event"] == "agent.handoff"]
        assert entry["reason"] == logged
        assert tags == [("agent.handoff", "yes")]

    async def test_the_reason_is_one_of_the_codes(self) -> None:
        cheap = ScriptedLLM(said("hi"))
        await self._respond(cheap, ScriptedLLM())
        [tool] = [tool for tool in cheap.tools[0] or () if tool["name"] == "hand_off"]
        reason = tool["input_schema"]["properties"]["reason"]
        assert reason["enum"] == ["many_items", "timetable", "multi_step", "other"]
        assert tool["input_schema"]["required"] == ["reason"]

    async def test_the_tag_stays_with_the_turn(self) -> None:
        await self._respond(ScriptedLLM(said("", TIMETABLE)), ScriptedLLM(said("done")))
        # The worker's scope outlives the turn: a tag left there would mark every later event.
        assert "agent.handoff" not in sentry_sdk.get_isolation_scope()._tags
