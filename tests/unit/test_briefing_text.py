"""The morning briefing's text: the fixed list code builds, and the one model call."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from personal_organizer.db.models.tenant import Reminder
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.llm import Completion, Message
from personal_organizer.messaging.agent import shown_event
from personal_organizer.messaging.briefing import compose_briefing, fallback_briefing
from personal_organizer.messaging.onboarding_text import t
from tests.fixtures.llm import ScriptedLLM, said

ZONE = ZoneInfo("Asia/Jerusalem")
DAY = date(2026, 10, 15)
DENTIST = CalendarEvent(
    "e1",
    "Dentist",
    datetime(2026, 10, 15, 6, 0, tzinfo=UTC),
    datetime(2026, 10, 15, 7, 0, tzinfo=UTC),
)
OFFSITE = CalendarEvent(
    "e2",
    "Offsite",
    datetime(2026, 10, 15, tzinfo=ZONE),
    datetime(2026, 10, 16, tzinfo=ZONE),
    all_day=True,
)
CALL_MOM = Reminder(text="call mom", due_at=datetime(2026, 10, 15, 15, 0, tzinfo=UTC))


class TestFallback:
    def test_events_then_reminders_with_local_times(self) -> None:
        text = fallback_briefing([DENTIST, OFFSITE], [CALL_MOM], zone=ZONE, language="he")
        assert text == (
            f"{t('briefing_intro', 'he')}\n09:00 Dentist\nOffsite\n\n"
            f"{t('briefing_reminders_heading', 'he')}\n18:00 call mom"
        )

    def test_an_empty_day_is_one_line(self) -> None:
        assert fallback_briefing([], [], zone=ZONE, language="en") == t("briefing_empty", "en")

    def test_reminders_only(self) -> None:
        text = fallback_briefing([], [CALL_MOM], zone=ZONE, language="en")
        heading = t("briefing_reminders_heading", "en")
        assert text == f"{t('briefing_intro', 'en')}\n\n{heading}\n18:00 call mom"

    def test_links_are_stripped_from_titles(self) -> None:
        invite = replace(DENTIST, title="Dentist https://evil.example/x?y=1 www.evil.example")
        link_only = replace(OFFSITE, title="http://evil.example")
        text = fallback_briefing([invite, link_only], [], zone=ZONE, language="en")
        assert text == f"{t('briefing_intro', 'en')}\n09:00 Dentist"


class TestCompose:
    async def _compose(self, llm: ScriptedLLM) -> str:
        return await compose_briefing(
            [DENTIST], [CALL_MOM], zone=ZONE, language="he", day=DAY, llm=llm
        )

    async def test_one_call_with_no_tools_and_the_day_as_json(self) -> None:
        llm = ScriptedLLM(said("  Morning! Dentist at 9.  "))
        assert await self._compose(llm) == "Morning! Dentist at 9."
        [[system, user]] = llm.calls
        assert (system.role, user.role) == ("system", "user")
        assert "Thursday 2026-10-15" in system.content
        assert "Hebrew" in system.content
        assert json.loads(user.content) == {
            "events": [shown_event(DENTIST, ZONE)],
            "reminders": [{"text": "call mom", "at": "18:00"}],
        }

    async def test_hebrew_gender_comes_from_the_note_or_is_phrased_around(self) -> None:
        llm = ScriptedLLM(said("Morning!"), said("Morning!"))
        await compose_briefing([DENTIST], [], zone=ZONE, language="he", day=DAY, llm=llm)
        await compose_briefing(
            [DENTIST], [], zone=ZONE, language="he", day=DAY, llm=llm, gendered="woman"
        )
        [[unknown, _], [woman, _]] = llm.calls
        assert "phrase around their gender" in unknown.content
        assert "feminine" in woman.content
        assert "phrase around" not in woman.content

    async def test_the_location_never_reaches_the_model(self) -> None:
        llm = ScriptedLLM(said("Morning!"))
        located = replace(DENTIST, location="Room 4, see https://evil.example")
        await compose_briefing([located], [], zone=ZONE, language="en", day=DAY, llm=llm)
        [[_, user]] = llm.calls
        [event] = json.loads(user.content)["events"]
        assert "location" not in event
        assert "evil" not in user.content

    async def test_no_tools_are_offered(self) -> None:
        offered: list[object] = []

        class Recording(ScriptedLLM):
            async def complete(
                self,
                messages: Sequence[Message],
                *,
                tools: Sequence[Mapping[str, Any]] | None = None,
                max_tokens: int,
            ) -> Completion:
                offered.append(tools)
                return await super().complete(messages, max_tokens=max_tokens)

        await self._compose(Recording(said("Morning!")))
        assert offered == [None]

    async def test_a_link_makes_it_unusable(self) -> None:
        assert await self._compose(ScriptedLLM(said("See HTTP://x"))) == ""

    async def test_a_cut_off_reply_is_unusable(self) -> None:
        cut = replace(said("Morning! Dentist at"), finish_reason="max_tokens")
        assert await self._compose(ScriptedLLM(cut)) == ""
