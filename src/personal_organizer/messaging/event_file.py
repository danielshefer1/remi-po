"""An .ics file for one calendar event, so a person can add it to any calendar app."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.channel import Attachment
from personal_organizer.messaging.onboarding_text import t

_STEM_MAX = 60


def _stem(title: str, language: str) -> str:
    kept = "".join(c for c in title if c.isalnum() or c in " -_")
    return " ".join(kept.split())[:_STEM_MAX].strip() or t("event_file_name", language)


def event_file(event: CalendarEvent, zone: ZoneInfo, language: str, *, now: datetime) -> Attachment:
    vevent = Event()
    vevent.add("UID", f"{event.external_id}@remi")
    vevent.add("DTSTAMP", now)
    vevent.add("SUMMARY", event.title)
    if event.all_day:
        # the stored end is already the exclusive next midnight, which is what iCalendar wants
        vevent.add("DTSTART", event.start.astimezone(zone).date())
        vevent.add("DTEND", event.end.astimezone(zone).date())
    else:
        vevent.add("DTSTART", event.start.astimezone(zone))
        vevent.add("DTEND", event.end.astimezone(zone))
    if event.location:
        vevent.add("LOCATION", event.location)
    # shortcut: a repeating event's file holds only the date listed; add RRULE (UNTIL in UTC,
    # RFC 5545) if someone asks for the series.
    cal = Calendar()
    cal.add("PRODID", "-//Remi//Personal organizer//EN")
    cal.add("VERSION", "2.0")
    cal.add("METHOD", "PUBLISH")
    cal.add_component(vevent)
    cal.add_missing_timezones()
    return Attachment(
        content=cal.to_ical(),
        filename=f"{_stem(event.title, language)}.ics",
        mime_type="text/calendar",
    )
