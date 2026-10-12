"""Onboarding scenarios against the real model (D10): ``pytest -m eval`` with AGENT__API_KEY.

Run before merging and on every model change. Each asserts what the code relies on, not the
wording: the tool call and its argument, the reply's language, and that it holds no URL (the
code would drop it). ``MODELS__CHAT_MODEL`` picks the model, so the step-up rule of D1 is
``MODELS__CHAT_MODEL=claude-sonnet-5-5 pytest -m eval``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anthropic
import pytest

from personal_organizer.messaging.conversation import converse
from personal_organizer.messaging.language import detect_language
from personal_organizer.messaging.onboarding import TOOLS
from personal_organizer.messaging.onboarding_prompt import onboarding_prompt
from personal_organizer.messaging.timezones import match_zone
from personal_organizer.providers.llm.anthropic import AnthropicLLM

pytestmark = pytest.mark.eval


@dataclass(frozen=True)
class Scenario:
    name: str
    language: str  # the tenant's
    connect: bool
    guess: str | None
    text: str
    reply_language: str
    #: The one tool call expected, as (name, argument), or None for no call at all.
    tool: tuple[str, str] | None = None


SCENARIOS = [
    Scenario("what is this, first", "en", False, "Europe/Amsterdam", "what is this?", "en"),
    Scenario("what is this, first, he", "he", False, "Asia/Jerusalem", "מה זה?", "he"),
    Scenario("is this safe", "en", True, None, "is this safe?", "en"),
    Scenario("is this safe, he", "he", True, None, "זה בטוח לחבר את היומן?", "he"),
    Scenario(
        "two places, he",
        "he",
        False,
        "Asia/Jerusalem",
        "אני בתל אביב אבל בניו יורק החודש",
        "he",
        ("set_timezone", "America/New_York"),
    ),
    Scenario(
        "speak English",
        "he",
        False,
        "Asia/Jerusalem",
        "speak English please",
        "en",
        ("set_language", "en"),
    ),
    Scenario("I use Outlook", "en", True, None, "I use Outlook, not Google", "en"),
    Scenario("the link doesn't work, he", "he", True, None, "הקישור לא עובד", "he"),
    Scenario(
        "a calendar request before connecting",
        "en",
        False,
        "Europe/London",
        "can you add my dentist tomorrow at 10?",
        "en",
    ),
    Scenario(
        "a calendar request before connecting, he",
        "he",
        True,
        None,
        "תוסיף לי פגישה עם נועה ביום חמישי בשמונה",
        "he",
    ),
    Scenario("gibberish", "en", False, "Europe/Amsterdam", "asdkjh qwpoe zzz", "en"),
]


@pytest.fixture
def llm() -> AnthropicLLM:
    client = anthropic.AsyncAnthropic(api_key=os.environ["AGENT__API_KEY"], max_retries=2)
    return AnthropicLLM(client, model=os.environ.get("MODELS__CHAT_MODEL", "claude-haiku-5-5"))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_onboarding(scenario: Scenario, llm: AnthropicLLM) -> None:
    calls: list[tuple[str, str]] = []

    async def run_tool(name: str, arguments: Mapping[str, Any]) -> str:
        value = str(next(iter(arguments.values()), ""))
        calls.append((name, match_zone(value) or value if name == "set_timezone" else value))
        return f"ok: {name} {value}"

    reply = await converse(
        [],
        scenario.text,
        system=onboarding_prompt(scenario.language, connect=scenario.connect, guess=scenario.guess),
        tools=TOOLS,
        run_tool=run_tool,
        llm=llm,
    )
    assert calls == ([scenario.tool] if scenario.tool else []), reply
    assert reply.strip(), "an empty reply falls back to the fixed text"
    assert detect_language(reply) == scenario.reply_language, reply
    assert "http" not in reply.lower(), reply
