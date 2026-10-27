from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.messaging.recurrence import check_rule, describe_rule, next_after

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
