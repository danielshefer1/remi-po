"""Who a group message is talking to: Remi by name, in either language, or by mention."""

from __future__ import annotations

import pytest

from personal_organizer.messaging.groups.addressing import names_remi
from tests.fixtures.gowa_payloads import load

BOT_PHONE = "+972552640741"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remi, free friday?", True),
        ("hey REMI", True),
        ("remind me", False),
        ("רמי מה קורה", True),
        ("ורמי", True),
        ("שאלתי את ברמי?", True),
        ("רמיקס", False),
        ("@972552640741 hi", True),
        ("@9725526407412 hi", False),
        (load("group_mention")["payload"]["body"], True),
        ("dinner at 7?", False),
        (None, False),
    ],
)
def test_names_remi(text: str | None, expected: bool) -> None:
    assert names_remi(text, bot_phone=BOT_PHONE) is expected


def test_no_bot_phone_means_no_mention() -> None:
    assert names_remi("@972552640741 hi", bot_phone=None) is False
