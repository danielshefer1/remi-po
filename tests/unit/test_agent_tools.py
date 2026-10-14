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

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
    LLMUnavailableError,
)
from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.agent import (
    MAX_WINDOW,
    NOT_CONNECTED,
    UNAVAILABLE,
    add_event,
    agent_prompt,
    check_event,
    event_summary,
    read_calendar,
    respond,
    week_of,
)
from personal_organizer.messaging.confirm import confirm_prompt
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from tests.fixtures.llm import ScriptedLLM, call, said

JERUSALEM = ZoneInfo("Asia/Jerusalem")
LONDON = ZoneInfo("Europe/London")
ACCOUNT = CalendarConnection(connected_account_id="ca_bound", composio_user_id="tenant-uuid")
TENANT = TenantState(
    id=UUID(int=1),
    status="active",
    step=None,
    language="he",
    first_message=False,
    timezone="Asia/Jerusalem",
)


class Failing(InMemoryCalendar):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def list_events(self, *args: Any, **kwargs: Any) -> list[CalendarEvent]:
        raise self.error


async def _read(
    calendar: InMemoryCalendar,
    start: object,
    end: object,
    zone: ZoneInfo = JERUSALEM,
    account: CalendarConnection | None = ACCOUNT,
) -> str:
    return await read_calendar(
        {"start": start, "end": end}, calendar=calendar, account=account, zone=zone
    )


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
        await respond(
            [], "hi", TENANT, llm=llm, calendar=InMemoryCalendar(), account=ACCOUNT, now=now
        )
        assert "Tuesday 2026-10-13, 00:30" in llm.calls[0][0].content


class TestZone:
    async def test_naive_local_times_get_the_tenant_zone(self) -> None:
        calendar = InMemoryCalendar()
        await _read(calendar, "2026-10-13T00:00", "2026-10-14T00:00")
        [(account, user, start, end)] = calendar.calls
        assert (account, user) == ("ca_bound", "tenant-uuid")
        assert start.isoformat() == "2026-10-13T00:00:00+03:00"
        assert end.isoformat() == "2026-10-14T00:00:00+03:00"

    async def test_a_date_alone_is_midnight(self) -> None:
        calendar = InMemoryCalendar()
        await _read(calendar, "2026-10-13", "2026-10-14")
        assert calendar.calls[0][2].isoformat() == "2026-10-13T00:00:00+03:00"

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
        calendar = InMemoryCalendar()
        following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        await _read(calendar, f"{day}T00:00", f"{following}T00:00", zone=LONDON)
        [(_, _, start, end)] = calendar.calls
        assert (start.isoformat()[-6:], end.isoformat()[-6:]) == offsets
        assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=hours)

    async def test_events_come_back_in_the_zone(self) -> None:
        calendar = InMemoryCalendar(
            [
                _event(
                    datetime(2026, 10, 13, 6, 0, tzinfo=UTC),
                    datetime(2026, 10, 13, 6, 30, tzinfo=UTC),
                )
            ]
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
        calendar = InMemoryCalendar(
            [
                _event(
                    datetime(2026, 3, 30, 8, 0, tzinfo=UTC),
                    datetime(2026, 3, 30, 9, 0, tzinfo=UTC),
                )
            ]
        )
        result = json.loads(await _read(calendar, "2026-03-29", "2026-04-01", zone=LONDON))
        assert result["events"][0]["start"] == "2026-03-30T09:00"


class TestEventFields:
    async def test_title_times_location_only(self) -> None:
        start = datetime(2026, 10, 13, 20, 0, tzinfo=JERUSALEM)
        calendar = InMemoryCalendar(
            [_event(start, start + timedelta(hours=2), location="Tel Aviv")]
        )
        [shown] = json.loads(await _read(calendar, "2026-10-13", "2026-10-14"))["events"]
        assert set(shown) == {"title", "start", "end", "all_day", "location"}
        assert shown["location"] == "Tel Aviv"

    async def test_an_all_day_event_shows_its_last_day(self) -> None:
        calendar = InMemoryCalendar(
            [
                _event(
                    datetime(2026, 10, 13, tzinfo=JERUSALEM),
                    datetime(2026, 10, 16, tzinfo=JERUSALEM),
                    all_day=True,
                )
            ]
        )
        [shown] = json.loads(await _read(calendar, "2026-10-13", "2026-10-14"))["events"]
        assert (shown["start"], shown["end"], shown["all_day"]) == (
            "2026-10-13",
            "2026-10-15",
            True,
        )

    async def test_hebrew_titles_stay_readable(self) -> None:
        start = datetime(2026, 10, 13, 20, 0, tzinfo=JERUSALEM)
        calendar = InMemoryCalendar([replace(_event(start, start), title="ארוחה עם נועה")])
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
        calendar = InMemoryCalendar()
        assert (await _read(calendar, start, end)).startswith("error: ")
        assert calendar.calls == []

    @pytest.mark.parametrize("end", ["2026-10-13T00:00", "2026-10-12T09:00"])
    async def test_end_must_be_after_start(self, end: str) -> None:
        calendar = InMemoryCalendar()
        assert await _read(calendar, "2026-10-13T00:00", end) == ("error: end must be after start")
        assert calendar.calls == []

    async def test_a_wide_window_is_capped(self) -> None:
        calendar = InMemoryCalendar()
        result = json.loads(await _read(calendar, "2026-10-01T00:00", "2027-10-01T00:00"))
        [(_, _, start, end)] = calendar.calls
        assert end - start == MAX_WINDOW
        assert "62 days" in result["note"]

    async def test_a_window_within_the_cap_has_no_note(self) -> None:
        result = json.loads(await _read(InMemoryCalendar(), "2026-10-01", "2026-12-02"))
        assert "note" not in result


class TestErrors:
    async def test_no_connection(self) -> None:
        calendar = InMemoryCalendar()
        assert await _read(calendar, "2026-10-13", "2026-10-14", account=None) == NOT_CONNECTED
        assert calendar.calls == []

    async def test_an_account_that_stopped_working(self) -> None:
        failing = Failing(CalendarNotConnectedError(401))
        assert await _read(failing, "2026-10-13", "2026-10-14") == NOT_CONNECTED

    async def test_a_refusal(self) -> None:
        failing = Failing(CalendarProviderRejectedError(400))
        assert await _read(failing, "2026-10-13", "2026-10-14") == UNAVAILABLE

    async def test_unavailable_is_raised_for_the_retry(self) -> None:
        with pytest.raises(CalendarProviderUnavailableError):
            await _read(
                Failing(CalendarProviderUnavailableError("down")), "2026-10-13", "2026-10-14"
            )


class TestRespond:
    @staticmethod
    async def _respond(llm: ScriptedLLM, account: CalendarConnection | None = ACCOUNT) -> str:
        return await respond(
            [],
            "מה יש לי מחר?",
            TENANT,
            llm=llm,
            calendar=InMemoryCalendar(),
            account=account,
            now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
        )

    async def test_the_tool_result_reaches_the_model(self) -> None:
        llm = ScriptedLLM(
            said("", call("list_events", start="2026-10-13T00:00", end="2026-10-14T00:00")),
            said("אין לך כלום מחר."),
        )
        assert await self._respond(llm) == "אין לך כלום מחר."
        assert llm.calls[1][-1].content == '{"events": []}'

    async def test_not_connected_adds_the_fixed_line(self) -> None:
        llm = ScriptedLLM(
            said("", call("list_events", start="2026-10-13T00:00", end="2026-10-14T00:00")),
            said("לא הצלחתי לקרוא את היומן."),
        )
        assert await self._respond(llm, account=None) == (
            f"לא הצלחתי לקרוא את היומן.\n\n{t('calendar_disconnected', 'he')}"
        )

    async def test_no_tool_call_no_fixed_line(self) -> None:
        assert await self._respond(ScriptedLLM(said("שלום!")), account=None) == "שלום!"

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


DENTIST = call(
    "propose_event", title="רופא שיניים", start="2026-10-13T09:00", end="2026-10-13T10:00"
)


class Creating(InMemoryCalendar):
    def __init__(self, error: Exception | None = None, link: str | None = None) -> None:
        super().__init__()
        self.error, self.link = error, link

    async def create_event(self, account_id: str, user_id: str, event: CalendarEvent) -> Any:
        if self.error is not None:
            raise self.error
        return replace(await super().create_event(account_id, user_id, event), link=self.link)


class TestPropose:
    @staticmethod
    async def _respond(
        llm: ScriptedLLM,
        proposed: list[CalendarEvent],
        account: CalendarConnection | None = ACCOUNT,
        calendar: InMemoryCalendar | None = None,
    ) -> str:
        return await respond(
            [],
            "תוסיף רופא שיניים מחר בתשע",
            TENANT,
            llm=llm,
            calendar=calendar or InMemoryCalendar(),
            account=account,
            now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
            proposed=proposed,
        )

    async def test_code_puts_the_choice_under_the_model_words(self) -> None:
        proposed: list[CalendarEvent] = []
        calendar = InMemoryCalendar()
        llm = ScriptedLLM(said("", DENTIST), said("להוסיף את זה?"))
        reply = await self._respond(llm, proposed, calendar=calendar)
        [event] = proposed
        assert (event.start.isoformat(), event.end.isoformat()) == (
            "2026-10-13T09:00:00+03:00",
            "2026-10-13T10:00:00+03:00",
        )
        summary = "\n".join(["רופא שיניים", "יום שלישי 2026-10-13, 09:00-10:00"])
        assert reply == f"להוסיף את זה?\n\n{confirm_prompt(summary, 'he')}"
        assert calendar.events == []  # nothing is written before the confirm
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

    async def test_no_connection_proposes_nothing(self) -> None:
        proposed: list[CalendarEvent] = []
        llm = ScriptedLLM(said("", DENTIST), said("אין חיבור ליומן."))
        reply = await self._respond(llm, proposed, account=None)
        assert proposed == []
        assert llm.calls[1][-1].content == NOT_CONNECTED
        assert reply == f"אין חיבור ליומן.\n\n{t('calendar_disconnected', 'he')}"

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

    async def _add(
        self, calendar: InMemoryCalendar, account: CalendarConnection | None = ACCOUNT
    ) -> str:
        return await add_event(
            self.EVENT, calendar=calendar, account=account, zone=JERUSALEM, language="en"
        )

    async def test_added_with_the_link(self) -> None:
        calendar = Creating(link="https://www.google.com/calendar/event?eid=abc")
        summary = event_summary(self.EVENT, JERUSALEM, "en")
        assert await self._add(calendar) == (
            f"{t('event_added', 'en', summary=summary)}"
            "\nhttps://www.google.com/calendar/event?eid=abc"
        )
        assert len(calendar.events) == 1

    async def test_added_without_a_link(self) -> None:
        summary = event_summary(self.EVENT, JERUSALEM, "en")
        assert await self._add(Creating()) == t("event_added", "en", summary=summary)

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
        assert await self._add(Creating(error)) == t(key, "en")

    async def test_no_connection(self) -> None:
        calendar = Creating()
        assert await self._add(calendar, account=None) == t("calendar_disconnected", "en")
        assert calendar.events == []
