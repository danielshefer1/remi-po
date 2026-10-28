from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.recurrence import (
    check_rule,
    describe_rule,
    first_at,
    next_after,
    occurrences,
)

J = ZoneInfo("Asia/Jerusalem")
FIRST = datetime(2026, 11, 1, 9, 0, tzinfo=J)


def test_weekly_byday_steps() -> None:
    after = datetime(2026, 10, 26, 7, 0, tzinfo=J)
    assert next_after("FREQ=WEEKLY;BYDAY=MO,WE", after) == datetime(2026, 10, 28, 7, 0, tzinfo=J)


def test_keeps_wall_time_across_dst() -> None:
    # DST ends 2026-10-25 in Israel.
    nxt = next_after("FREQ=DAILY", datetime(2026, 10, 24, 8, 0, tzinfo=J))
    assert nxt is not None
    assert (nxt.day, nxt.hour, nxt.tzinfo) == (25, 8, J)


def test_count_becomes_until() -> None:
    assert check_rule("FREQ=DAILY;COUNT=3", FIRST) == "FREQ=DAILY;UNTIL=20261103T090000"


def test_first_at_moves_to_the_rules_first_day() -> None:
    tuesday = datetime(2026, 10, 27, 8, 0, tzinfo=J)
    rule = check_rule("FREQ=WEEKLY;BYDAY=SU;COUNT=3", tuesday)
    times = [first_at(rule, tuesday)]
    while (nxt := next_after(rule, times[-1])) is not None:
        times.append(nxt)
    assert times == [datetime(2026, 11, d, 8, 0, tzinfo=J) for d in (1, 8, 15)]


def test_first_at_on_a_rule_day_stays() -> None:
    assert first_at("FREQ=WEEKLY;BYDAY=SU", FIRST) == FIRST


def test_until_before_the_first_time_is_an_error() -> None:
    with pytest.raises(ValueError, match="UNTIL"):
        first_at(check_rule("FREQ=DAILY;UNTIL=20261001T090000", FIRST), FIRST)


def test_normalises_key_order() -> None:
    assert (
        check_rule("byday=mo,we;interval=2;freq=weekly", FIRST)
        == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE"
    )


def test_none_after_until() -> None:
    last = datetime(2026, 11, 3, 9, 0, tzinfo=J)
    assert next_after("FREQ=DAILY;UNTIL=20261103T090000", last) is None


def test_monthly_and_yearly_step() -> None:
    assert next_after("FREQ=MONTHLY;BYMONTHDAY=-1", FIRST) == datetime(2026, 11, 30, 9, 0, tzinfo=J)
    assert next_after("FREQ=YEARLY", FIRST) == datetime(2027, 11, 1, 9, 0, tzinfo=J)


@pytest.mark.parametrize(
    "bad",
    [
        "FREQ=HOURLY",
        "FREQ=MINUTELY",
        "FREQ=DAILY;BYHOUR=9,10",
        "FREQ=WEEKLY;BYDAY=1MO",
        "FREQ=DAILY;BYDAY=MO",
        "FREQ=WEEKLY;BYMONTHDAY=1",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;INTERVAL=53",
        "FREQ=DAILY;COUNT=401",
        "FREQ=DAILY;UNTIL=tomorrow",
        "FREQ=DAILY;UNTIL=20261399T000000",
        "FREQ=DAILY;FREQ=WEEKLY",
        "DTSTART=20260101T000000;FREQ=DAILY",
        "FREQ=DAILY;COUNT=2;UNTIL=20270101T000000",
        "INTERVAL=2",
        "nonsense",
    ],
)
def test_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match=r"\w"):
        check_rule(bad, FIRST)


def test_describe() -> None:
    assert describe_rule("FREQ=DAILY") == "every day"
    assert describe_rule("FREQ=WEEKLY;BYDAY=SU") == "every Sunday"
    assert describe_rule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE") == (
        "every 2 weeks on Monday and Wednesday"
    )
    assert describe_rule("FREQ=MONTHLY;BYMONTHDAY=1") == "monthly on the 1st"
    assert describe_rule("FREQ=MONTHLY;BYMONTHDAY=-1") == "monthly on the last day"
    assert describe_rule("FREQ=YEARLY;UNTIL=20270630T090000") == "every year until 2027-06-30"


def test_describe_weekly_several_days() -> None:
    assert describe_rule("FREQ=WEEKLY;BYDAY=MO,WE") == "every Monday and Wednesday"


def test_describe_hebrew() -> None:
    def he(rule: str) -> str:
        return describe_rule(rule, "he")

    assert he("FREQ=WEEKLY;BYDAY=MO,WE") == "כל יום שני ורביעי"
    assert he("FREQ=DAILY") == "כל יום"
    assert he("FREQ=WEEKLY;BYDAY=SU") == "כל יום ראשון"
    assert he("FREQ=WEEKLY;BYDAY=MO,FR,SA") == "כל יום שני, שישי ושבת"
    assert he("FREQ=WEEKLY;INTERVAL=2") == "כל שבועיים"
    assert he("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU") == "כל שבועיים ביום שלישי"
    assert he("FREQ=MONTHLY;BYMONTHDAY=15") == "ב-15 לחודש"
    # Israelis count the week from Sunday, so Saturday comes last.
    assert he("FREQ=WEEKLY;BYDAY=SA,MO") == "כל יום שני ושבת"
    assert he("FREQ=WEEKLY;BYDAY=MO,SU") == "כל יום ראשון ושני"
    assert he("FREQ=MONTHLY;BYMONTHDAY=-1") == "ביום האחרון של כל חודש"
    assert he("FREQ=YEARLY") == "כל שנה"
    assert he("FREQ=WEEKLY;BYDAY=MO;UNTIL=20270131T180000") == "כל יום שני עד 2027-01-31"


JLM = ZoneInfo("Asia/Jerusalem")


def _class(rule: str, first: datetime, hours: int = 1) -> CalendarEvent:
    return CalendarEvent(
        "series", "Class", first, first.replace(hour=first.hour + hours), rule=rule
    )


def test_occurrences_plain_event_passes_through() -> None:
    plain = CalendarEvent(
        "x", "Dentist", datetime(2026, 11, 2, 9, tzinfo=JLM), datetime(2026, 11, 2, 10, tzinfo=JLM)
    )
    assert occurrences(
        plain, datetime(2026, 1, 1, tzinfo=JLM), datetime(2026, 2, 1, tzinfo=JLM), JLM
    ) == [plain]


def test_occurrences_keep_wall_time_across_dst() -> None:
    # Israel went back to winter time on 2026-10-25.
    series = _class("FREQ=WEEKLY;BYDAY=MO", datetime(2026, 10, 19, 18, tzinfo=JLM))
    got = occurrences(
        series, datetime(2026, 10, 19, tzinfo=JLM), datetime(2026, 11, 3, tzinfo=JLM), JLM
    )
    assert [(o.start.hour, o.start.day, o.end.hour) for o in got] == [
        (18, 19, 19),
        (18, 26, 19),
        (18, 2, 19),
    ]
    assert got[1].external_id == "series:20261026T1800"
    assert got[1].rule == series.rule
    assert got[0].start.utcoffset() != got[1].start.utcoffset()


def test_occurrences_open_series_only_window_and_overlap_from_before() -> None:
    night = CalendarEvent(
        "s",
        "Shift",
        datetime(2026, 1, 1, 22, tzinfo=JLM),
        datetime(2026, 1, 2, 2, tzinfo=JLM),
        rule="FREQ=DAILY",
    )
    got = occurrences(
        night, datetime(2026, 6, 10, 1, tzinfo=JLM), datetime(2026, 6, 10, 23, tzinfo=JLM), JLM
    )
    # the 9th's shift is still on at 01:00; the 10th's starts at 22:00
    assert [o.start.day for o in got] == [9, 10]


def test_occurrences_until_respected() -> None:
    series = _class("FREQ=DAILY;UNTIL=20261102T180000", datetime(2026, 11, 1, 18, tzinfo=JLM))
    got = occurrences(
        series, datetime(2026, 11, 1, tzinfo=JLM), datetime(2026, 12, 1, tzinfo=JLM), JLM
    )
    assert [o.start.day for o in got] == [1, 2]
