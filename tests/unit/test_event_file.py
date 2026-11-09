from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.event_file import event_file

JERUSALEM = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _event(**kw: Any) -> CalendarEvent:
    base: dict[str, Any] = {
        "external_id": "abc123",
        "title": "Dentist",
        "start": datetime(2026, 10, 13, 6, 0, tzinfo=UTC),
        "end": datetime(2026, 10, 13, 7, 0, tzinfo=UTC),
    }
    return CalendarEvent(**{**base, **kw})


def _parse(event: CalendarEvent) -> tuple[Calendar, bytes]:
    attachment = event_file(event, JERUSALEM, "en", now=NOW)
    assert attachment.mime_type == "text/calendar"
    return Calendar.from_ical(attachment.content), attachment.content


def _vevents(cal: Calendar) -> list[Any]:
    return list(cal.walk("VEVENT"))


def test_a_hebrew_title_comes_back_whole() -> None:
    title = "ארוחת ערב משפחתית עם כל החברים מהעבודה ומהצבא ומהלימודים ומהשכונה"
    assert len(title.encode()) > 75
    cal, _ = _parse(_event(title=title))
    assert str(_vevents(cal)[0]["SUMMARY"]) == title


def test_times_are_in_their_zone() -> None:
    cal, content = _parse(_event())
    start = _vevents(cal)[0]["DTSTART"].dt
    assert (start.hour, start.utcoffset()) == (9, timedelta(hours=3))
    assert b"TZID=Asia/Jerusalem" in content
    assert len(cal.walk("VTIMEZONE")) == 1


def test_after_the_clocks_change() -> None:
    start = datetime(2026, 10, 27, 18, 0, tzinfo=JERUSALEM)
    cal, _ = _parse(_event(start=start, end=start + timedelta(hours=1)))
    got = _vevents(cal)[0]["DTSTART"].dt
    assert (got.hour, got.utcoffset()) == (18, timedelta(hours=2))


@pytest.mark.parametrize(("days", "last"), [(1, date(2026, 10, 16)), (3, date(2026, 10, 18))])
def test_all_day_uses_dates(days: int, last: date) -> None:
    start = datetime(2026, 10, 15, tzinfo=JERUSALEM)
    cal, _ = _parse(_event(all_day=True, start=start, end=start + timedelta(days=days)))
    vevent = _vevents(cal)[0]
    assert vevent["DTSTART"].dt == date(2026, 10, 15)
    assert vevent["DTEND"].dt == last
    assert not cal.walk("VTIMEZONE")


def test_location_only_when_set() -> None:
    cal, _ = _parse(_event())
    assert "LOCATION" not in _vevents(cal)[0]
    cal, _ = _parse(_event(location="Clinic"))
    assert str(_vevents(cal)[0]["LOCATION"]) == "Clinic"


def test_uid_is_stable_and_stamp_is_now() -> None:
    first = _vevents(_parse(_event())[0])[0]
    second = _vevents(_parse(_event())[0])[0]
    assert first["UID"] == second["UID"] == "abc123@remi"
    assert first["DTSTAMP"].dt == NOW


def test_a_repeating_event_is_one_date() -> None:
    cal, _ = _parse(_event(rule="FREQ=WEEKLY;BYDAY=SU"))
    assert "RRULE" not in _vevents(cal)[0]


def test_a_newline_cannot_forge_an_event() -> None:
    title = "a\nEND:VEVENT\nBEGIN:VEVENT\nSUMMARY:x"
    cal, _ = _parse(_event(title=title))
    events = _vevents(cal)
    assert len(events) == 1
    assert str(events[0]["SUMMARY"]) == title


@pytest.mark.parametrize(
    ("title", "language", "expected"),
    [
        ("Dentist", "en", "Dentist.ics"),
        ("ארוחת ערב עם נועה", "he", "ארוחת ערב עם נועה.ics"),
        ('../../etc/"pass"', "en", "etcpass.ics"),
        ("a\u202eb", "en", "ab.ics"),
        ("a\u200fb", "en", "ab.ics"),
        ("a\u2066b", "en", "ab.ics"),
        ("***", "he", "אירוע.ics"),
        ("***", "en", "event.ics"),
    ],
)
def test_filenames(title: str, language: str, expected: str) -> None:
    assert event_file(_event(title=title), JERUSALEM, language, now=NOW).filename == expected


def test_a_long_title_is_cut() -> None:
    name = event_file(_event(title="א" * 200), JERUSALEM, "he", now=NOW).filename
    assert name == "א" * 60 + ".ics"
