"""Repeating rules for reminders and calendar events: a never-sub-daily subset of RFC 5545 RRULE.

Occurrences (``occurrences`` expands a series for reads) are computed on local wall time and
then made aware, so an 08:00 reminder or event stays 08:00 across DST.
``UNTIL`` is local wall time (no ``Z``). A stored rule never has ``COUNT``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

from dateutil.rrule import rrulestr

from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.language import HEBREW

_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Without "יום": Saturday has none, and a list reads "יום שני ורביעי".
_HE_DAYS = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")
_KEYS = ("FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "UNTIL")
_UNTIL = re.compile(r"\d{8}T\d{6}")
_MAX_COUNT = 400


def _parse(rule: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for pair in rule.split(";"):
        key, sep, value = pair.partition("=")
        key = key.strip().upper()
        if not sep or not value.strip():
            raise ValueError(f"'{pair}' is not KEY=VALUE")
        if key not in (*_KEYS, "COUNT"):
            raise ValueError(
                f"{key} is not allowed; use FREQ, INTERVAL, BYDAY, BYMONTHDAY, UNTIL or COUNT"
            )
        if key in parts:
            raise ValueError(f"{key} appears twice")
        parts[key] = value.strip().upper()
    return parts


def _int(parts: dict[str, str], key: str) -> int:
    try:
        return int(parts[key])
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None


def check_rule(rule: str, first: datetime) -> str:
    """Return the normalised rule (fixed key order, COUNT turned into UNTIL) or raise ValueError."""
    parts = _parse(rule)
    freq = parts.get("FREQ")
    if freq not in _FREQS:
        raise ValueError(
            "FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY (never more often than daily)"
        )
    if "INTERVAL" in parts and not 1 <= _int(parts, "INTERVAL") <= 52:
        raise ValueError("INTERVAL must be between 1 and 52")
    if "BYDAY" in parts:
        if freq != "WEEKLY":
            raise ValueError("BYDAY only goes with FREQ=WEEKLY")
        if any(d not in _DAYS for d in parts["BYDAY"].split(",")):
            raise ValueError("BYDAY takes plain day codes MO, TU, WE, TH, FR, SA, SU")
    if "BYMONTHDAY" in parts:
        if freq != "MONTHLY":
            raise ValueError("BYMONTHDAY only goes with FREQ=MONTHLY")
        day = _int(parts, "BYMONTHDAY")
        if day != -1 and not 1 <= day <= 31:
            raise ValueError("BYMONTHDAY must be 1 to 31, or -1 for the last day")
    if "UNTIL" in parts and "COUNT" in parts:
        raise ValueError("use UNTIL or COUNT, not both")
    if "UNTIL" in parts:
        try:
            if not _UNTIL.fullmatch(parts["UNTIL"]):
                raise ValueError
            datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%S")  # noqa: DTZ007 - local wall time
        except ValueError:
            raise ValueError(
                "UNTIL must be a real date like 20270630T090000 (local time, no Z)"
            ) from None
    if "COUNT" in parts:
        count = _int(parts, "COUNT")
        if not 1 <= count <= _MAX_COUNT:
            raise ValueError(f"COUNT must be between 1 and {_MAX_COUNT}")
        normal = ";".join(f"{k}={parts[k]}" for k in _KEYS if k in parts)
        start = first_at(normal, first).replace(tzinfo=None)
        series = list(rrulestr(f"{normal};COUNT={count}", dtstart=start))
        parts["UNTIL"] = series[-1].strftime("%Y%m%dT%H%M%S")
        del parts["COUNT"]
    return ";".join(f"{k}={parts[k]}" for k in _KEYS if k in parts)


def first_at(rule: str, at: datetime) -> datetime:
    """The rule's first occurrence at or after ``at``, so a Tuesday ``at`` for a Sundays rule
    becomes the Sunday. Raises ValueError when there is none (``UNTIL`` is before it)."""
    naive = at.replace(tzinfo=None)
    first = rrulestr(rule, dtstart=naive).after(naive, inc=True)
    if first is None:
        raise ValueError("UNTIL is before the first time, so it would never go off")
    return first.replace(tzinfo=at.tzinfo)


def _with_until(rule: str, until: datetime) -> str:
    parts = _parse(rule) | {"UNTIL": until.strftime("%Y%m%dT%H%M%S")}
    return ";".join(f"{k}={parts[k]}" for k in _KEYS if k in parts)


def until_before(rule: str, at: datetime) -> str:
    """``rule`` ending one second before ``at``'s local wall time (any ``UNTIL`` replaced)."""
    return _with_until(rule, at.replace(tzinfo=None) - timedelta(seconds=1))


def shift_until(rule: str, by: timedelta) -> str:
    """``rule`` with its ``UNTIL`` moved by ``by``, so a series moved later keeps its last date."""
    until = _parse(rule).get("UNTIL")
    if until is None:
        return rule
    wall = datetime.strptime(until, "%Y%m%dT%H%M%S")  # noqa: DTZ007 - local wall time
    return _with_until(rule, wall + by)


def _rrule(lines: Sequence[str]) -> int | None:
    return next((i for i, line in enumerate(lines) if line.upper().startswith("RRULE:")), None)


def local_rule(lines: Sequence[str], zone: tzinfo, all_day: bool) -> str | None:
    """Google's ``RRULE:`` line as our rule (``UNTIL`` on the wall clock in ``zone``), or
    ``None`` when there is none or it is outside what :func:`check_rule` takes."""
    i = _rrule(lines)
    if i is None:
        return None
    parts = []
    for part in lines[i].partition(":")[2].split(";"):
        key, _, value = part.partition("=")
        if key.upper() == "UNTIL":
            if all_day or len(value) == 8:  # a date: through the end of that day
                value = value[:8] + "T235959"
            elif value.upper().endswith("Z"):
                moment = datetime.strptime(value.upper(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                value = moment.astimezone(zone).strftime("%Y%m%dT%H%M%S")
        parts.append(f"{key}={value}")
    rule = ";".join(parts)
    try:
        # COUNT needs the series' first date to become UNTIL; we can't copy it faithfully.
        if "COUNT" in _parse(rule):
            return None
        return check_rule(rule, datetime.now(zone))
    except ValueError:
        return None


def end_lines(lines: Sequence[str], before: datetime, all_day: bool) -> list[str]:
    """``lines`` with the ``RRULE:`` ending just before ``before``, in Google's form: ``UNTIL``
    in UTC with a ``Z``, or the day before as a date for an all-day series. ``COUNT`` is
    dropped; every other part and line is kept as it was."""
    if all_day:
        until = (before.date() - timedelta(days=1)).strftime("%Y%m%d")
    else:
        until = (before - timedelta(seconds=1)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    ended = list(lines)
    i = _rrule(lines)
    if i is not None:
        parts = [
            p
            for p in lines[i].partition(":")[2].split(";")
            if p.partition("=")[0].upper() not in ("COUNT", "UNTIL")
        ]
        ended[i] = "RRULE:" + ";".join([*parts, f"UNTIL={until}"])
    return ended


def next_after(rule: str, after: datetime) -> datetime | None:
    """First occurrence strictly after ``after`` (which is also DTSTART), or None past UNTIL."""
    naive = after.replace(tzinfo=None)
    nxt = rrulestr(rule, dtstart=naive).after(naive, inc=False)
    return None if nxt is None else nxt.replace(tzinfo=after.tzinfo)


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def occurrences(
    event: CalendarEvent,
    start: datetime,
    end: datetime,
    zone: tzinfo,
    skipped: Collection[datetime] = (),
) -> list[CalendarEvent]:
    """The occurrences of ``event`` overlapping ``[start, end)``, in ``zone``; an event with no
    rule comes back as it is. Each is named ``<series id>:<local start>``, keeps the series'
    wall-clock length, and is computed on naive local time so 18:00 stays 18:00 across DST.
    Those starting at a ``skipped`` instant are dropped; each carries ``series_id``."""
    if not event.rule:
        return [event]
    first = event.start.astimezone(zone).replace(tzinfo=None)
    length = event.end.astimezone(zone).replace(tzinfo=None) - first
    lo, hi = start.astimezone(zone).replace(tzinfo=None), end.astimezone(zone).replace(tzinfo=None)
    series = rrulestr(event.rule, dtstart=first)
    found = []
    for at in series.between(lo - length, hi, inc=True):
        begin, finish = at.replace(tzinfo=zone), (at + length).replace(tzinfo=zone)
        if begin < end and finish > start and begin not in skipped:
            found.append(
                replace(
                    event,
                    external_id=f"{event.external_id}:{at:%Y%m%dT%H%M}",
                    start=begin,
                    end=finish,
                    series_id=event.external_id,
                )
            )
    return found


def _he_days(days: list[int], *, on: bool) -> str:
    """ "יום שני ורביעי"; with ``on``, "ביום שני ורביעי"."""
    names = [("יום " if d != 5 else "") + _HE_DAYS[d] for d in days]
    names[1:] = [n.removeprefix("יום ") for n in names[1:]]
    text = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " ו" + names[-1]  # noqa: RUF001
    return ("ב" if on else "") + text


def _describe_he(parts: dict[str, str]) -> str:
    freq, n = parts["FREQ"], int(parts.get("INTERVAL", "1"))
    # Sunday first, as the week is counted here, so Saturday comes last.
    days = sorted(
        (_DAYS.index(d) for d in parts.get("BYDAY", "").split(",") if d), key=lambda d: (d + 1) % 7
    )
    singular, dual, plural = {
        "DAILY": ("יום", "יומיים", "ימים"),
        "WEEKLY": ("שבוע", "שבועיים", "שבועות"),
        "MONTHLY": ("חודש", "חודשיים", "חודשים"),
        "YEARLY": ("שנה", "שנתיים", "שנים"),
    }[freq]
    every = f"כל {singular}" if n == 1 else f"כל {dual}" if n == 2 else f"כל {n} {plural}"
    if days:
        text = f"כל {_he_days(days, on=False)}" if n == 1 else f"{every} {_he_days(days, on=True)}"
    elif "BYMONTHDAY" in parts:
        day = int(parts["BYMONTHDAY"])
        on = "ביום האחרון" if day == -1 else f"ב-{day}"
        month = "של כל חודש" if day == -1 else "לחודש"  # "ב-15 לחודש"
        text = f"{on} {month}" if n == 1 else f"{every} {on}"
    else:
        text = every
    if "UNTIL" in parts:
        u = parts["UNTIL"]
        text += f" עד {u[:4]}-{u[4:6]}-{u[6:8]}"
    return text


def describe_rule(rule: str, language: str = "en") -> str:
    """Plain words for the model to restate (English), or for the person (Hebrew)."""
    parts = _parse(rule)
    if language == HEBREW:
        return _describe_he(parts)
    freq, n = parts["FREQ"], int(parts.get("INTERVAL", "1"))
    days = [_DAY_NAMES[_DAYS.index(d)] for d in parts.get("BYDAY", "").split(",") if d]
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}[freq]
    if freq == "MONTHLY" and n == 1 and "BYMONTHDAY" in parts:
        day = int(parts["BYMONTHDAY"])
        text = "monthly on the " + ("last day" if day == -1 else _ordinal(day))
    elif freq == "WEEKLY" and n == 1 and days:
        text = "every " + " and ".join([", ".join(days[:-1]), days[-1]] if len(days) > 1 else days)
    else:
        text = f"every {unit}" if n == 1 else f"every {n} {unit}s"
        if days:
            text += " on " + " and ".join(
                [", ".join(days[:-1]), days[-1]] if len(days) > 1 else days
            )
        if freq == "MONTHLY" and "BYMONTHDAY" in parts:
            day = int(parts["BYMONTHDAY"])
            text += " on the " + ("last day" if day == -1 else _ordinal(day))
    if "UNTIL" in parts:
        u = parts["UNTIL"]
        text += f" until {u[:4]}-{u[4:6]}-{u[6:8]}"
    return text
