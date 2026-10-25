"""The group turn's pure pieces: busy blocks carry no event content, and the prompt."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.db.models.tenant import LANGUAGES
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.groups.prompt import group_prompt
from personal_organizer.messaging.groups.turn import busy_blocks

ZONE = ZoneInfo("Asia/Jerusalem")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def test_busy_blocks_have_no_title_or_location() -> None:
    events = [
        CalendarEvent("e1", "Dentist", _at(23, 9), _at(23, 10), location="Herzl 5, Tel Aviv"),
        CalendarEvent("e2", "Dentist", _at(24, 0), _at(25, 0), all_day=True, location="Haifa"),
    ]
    blocks = busy_blocks(events, ZONE)
    dumped = json.dumps(blocks, ensure_ascii=False)
    assert "Dentist" not in dumped
    assert "Herzl" not in dumped
    assert "Haifa" not in dumped
    assert all(set(block) == {"start", "end", "all_day"} for block in blocks)
    assert blocks == [
        {"start": "2026-10-23T09:00", "end": "2026-10-23T10:00", "all_day": False},
        {"start": "2026-10-24", "end": "2026-10-24", "all_day": True},
    ]


def test_busy_blocks_merge_overlaps() -> None:
    events = [
        CalendarEvent("e1", "a", _at(23, 9), _at(23, 10)),
        CalendarEvent("e2", "b", _at(23, 9, 30), _at(23, 11)),
        CalendarEvent("e3", "c", _at(23, 11), _at(23, 12)),  # touches: one block
        CalendarEvent("e4", "d", _at(23, 15), _at(23, 16)),
        # 15:15 in Jerusalem, given in UTC: merged by the instant, not the wall clock.
        CalendarEvent("e5", "e", datetime(2026, 10, 23, 12, 15, tzinfo=UTC), _at(23, 16, 30)),
    ]
    assert busy_blocks(events, ZONE) == [
        {"start": "2026-10-23T09:00", "end": "2026-10-23T12:00", "all_day": False},
        {"start": "2026-10-23T15:00", "end": "2026-10-23T16:30", "all_day": False},
    ]


@pytest.mark.parametrize("language", LANGUAGES)
def test_group_prompt(language: str) -> None:
    prompt = group_prompt(language, datetime(2026, 10, 21, 13, 0, tzinfo=ZONE), "Dana", True)
    assert "Dana" in prompt
    assert "Rami" in prompt  # "רמי" may be a person, not Remi
    assert "{" not in prompt
    assert "Wednesday 2026-10-21, 13:00" in prompt
