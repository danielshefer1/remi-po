"""The group turn's pure pieces: busy blocks carry no event content, and the prompt."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.db.models.tenant import LANGUAGES
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.groups.prompt import group_prompt
from personal_organizer.messaging.groups.turn import GROUP_TOOLS, _clean, busy_blocks, person_entry
from personal_organizer.messaging.onboarding_text import t

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


def test_group_prompt_lists_open_outings() -> None:
    now = datetime(2026, 10, 21, 13, 0, tzinfo=ZONE)
    line = "Family dinner, Friday 2026-10-23, 19:00-21:00"
    assert line in group_prompt("en", now, "Dana", True, outings=(line,))
    assert "still open for votes" not in group_prompt("en", now, "Dana", True)


def test_a_group_has_no_file_tool() -> None:
    assert "send_event_file" not in {tool["name"] for tool in GROUP_TOOLS}


def _meeting(**kw: object) -> CalendarEvent:
    fields: dict[str, object] = {
        "external_id": "e1",
        "title": "Board meeting",
        "start": _at(23, 9),
        "end": _at(23, 10),
        "location": "Herzl 5",
    }
    return CalendarEvent(**(fields | kw))  # type: ignore[arg-type]


def test_no_details_is_busy_blocks_only() -> None:
    events = [_meeting()]
    entry = person_entry("Noa", events, ZONE, details=False)
    assert entry == {"name": "Noa", "busy": busy_blocks(events, ZONE)}
    dumped = json.dumps(entry)
    assert "Board" not in dumped
    assert "Herzl" not in dumped


def test_details_show_title_time_place() -> None:
    entry = person_entry("Noa", [_meeting()], ZONE, details=True)
    assert entry["busy"] == []
    [shown] = entry["events"]
    assert shown == {
        "title": "Board meeting",
        "start": "2026-10-23T09:00",
        "end": "2026-10-23T10:00",
        "all_day": False,
        "location": "Herzl 5",
    }


def test_private_stays_in_busy() -> None:
    entry = person_entry("Noa", [_meeting(private=True)], ZONE, details=True)
    assert "events" not in entry or entry["events"] == []
    assert "Board" not in json.dumps(entry)
    assert entry["busy"] == [
        {"start": "2026-10-23T09:00", "end": "2026-10-23T10:00", "all_day": False}
    ]


def test_a_title_left_empty_stays_busy() -> None:
    entry = person_entry("Noa", [_meeting(title="https://x.example/a")], ZONE, details=True)
    assert not entry.get("events")
    assert len(entry["busy"]) == 1


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("Call https://zoom.us/j/1 Dana", "Call Dana"),
        ("www.evil.example now", "now"),
        ("meet.google.com/abc-def", ""),
        ("evil.com", ""),
        ("Yoga at St.Louis", "Yoga at"),
        ("evil.biz", ""),
        ("evil.ru evil.shop evil.dev evil.ai evil.de", ""),
        ("youtu.be/x goo.gl/x forms.gle/x lnkd.in/x", ""),
        ("Evil.Biz", ""),
        ("1.2.3.4/x", ""),
        ("maps.app.goo.gl/x", ""),
        ("evil\uff0ebiz", ""),
        ("evil\u3002biz", ""),
        ("https:// evil.biz/a", ""),
        ("http:/evil.biz", ""),
        ("HTTP workshop", ""),
        ("Dr. Cohen", "Dr. Cohen"),
        ("Meeting at 10.30", "Meeting at 10.30"),
        ("ת.א", "ת.א"),
        ("ארוחת ערב אצל סבתא", "ארוחת ערב אצל סבתא"),
        ("a\nb", "a b"),
        ("x" * 200, "x" * 80),
    ],
)
def test_clean(text: str, cleaned: str) -> None:
    assert _clean(text) == cleaned


@pytest.mark.parametrize("text", ["_evil.com_", ".evil.com", "a" * 76 + ".comx"])
def test_clean_leaves_no_link(text: str) -> None:
    assert ".com" not in _clean(text)


def test_a_title_with_http_stays_busy() -> None:
    entry = person_entry(
        "Noa", [_meeting(title="HTTP workshop", location="http:/x")], ZONE, details=True
    )
    assert entry["events"] == []
    assert len(entry["busy"]) == 1
    assert "workshop" not in json.dumps(entry)


def test_group_tools_offer_show_my_events() -> None:
    [tool] = [tool for tool in GROUP_TOOLS if tool["name"] == "show_my_events"]
    schema = tool["input_schema"]
    assert schema["required"] == ["on"]
    assert set(schema["properties"]) == {"on"}


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", ["group_details_on", "group_details_off"])
def test_group_details_copy(key: str, language: str) -> None:
    text = t(key, language)
    assert text
    assert "{" not in text
