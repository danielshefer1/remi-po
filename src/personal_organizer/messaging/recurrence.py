"""Repeating-reminder rules: a small, never-sub-daily subset of RFC 5545 RRULE.

Occurrences are computed on local wall time and then made aware, so an 08:00 reminder stays
08:00 across DST. ``UNTIL`` is local wall time (no ``Z``). A stored rule never has ``COUNT``.
"""

from __future__ import annotations

import re
from datetime import datetime

from dateutil.rrule import rrulestr

_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
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


def next_after(rule: str, after: datetime) -> datetime | None:
    """First occurrence strictly after ``after`` (which is also DTSTART), or None past UNTIL."""
    naive = after.replace(tzinfo=None)
    nxt = rrulestr(rule, dtstart=naive).after(naive, inc=False)
    return None if nxt is None else nxt.replace(tzinfo=after.tzinfo)


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_rule(rule: str) -> str:
    """Plain English for the model to restate in the person's language."""
    parts = _parse(rule)
    freq, n = parts["FREQ"], int(parts.get("INTERVAL", "1"))
    days = [_DAY_NAMES[_DAYS.index(d)] for d in parts.get("BYDAY", "").split(",") if d]
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}[freq]
    if freq == "MONTHLY" and n == 1 and "BYMONTHDAY" in parts:
        day = int(parts["BYMONTHDAY"])
        text = "monthly on the " + ("last day" if day == -1 else _ordinal(day))
    elif freq == "WEEKLY" and n == 1 and len(days) == 1:
        text = f"every {days[0]}"
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
