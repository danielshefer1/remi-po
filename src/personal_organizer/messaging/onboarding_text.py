"""Every fixed line onboarding and the agent send, in both languages (D11).

One table, so a missing translation is a failing test (``test_onboarding_text.py`` checks
that the key sets and the ``{placeholders}`` match) rather than an English line in a Hebrew
conversation. The templates hold no braces besides their placeholders, and values are user-
or system-supplied strings (a zone name, a URL) inserted with ``format_map``. A value is
never itself parsed as a template.

``connect_link`` ends with the URL on a line of its own. WhatsApp linkifies a bare URL
reliably, but not one with a character glued to it. ``all_set`` is sent by the
``onboarding:connected`` task (PR 4). ``agent_unavailable`` and ``calendar_disconnected``
are the agent's (``messaging.agent``). ``zone_retry_keep`` says "reply 1", which relies on
Correct being the first option of ``messaging.onboarding.zone_choice``. A test there pins
that order.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import ENGLISH

_EN: Final = {
    "welcome": "Hi! I'm your personal organizer. Two quick steps and you're set up.",
    "zone_confirm": "Is your time zone {zone}?",
    "option_correct": "Correct",
    "option_change": "Change",
    "zone_ask_city": "Which city are you in? For example: London, or Europe/London.",
    "zone_retry": (
        "I couldn't match that to a time zone. Send a city, for example London, "
        "or a zone such as Europe/London."
    ),
    "zone_retry_keep": "Or reply 1 to keep {zone}.",
    "zone_set": "Your time zone is set to {zone}.",
    "connect_link": (
        "Now connect your Google Calendar. Open this link and follow the steps. "
        "It works once and expires soon:\n{url}"
    ),
    "all_set": "You're all set! Your Google Calendar is connected.",
    "agent_unavailable": "Sorry, I couldn't answer just now. Please try again in a minute.",
    "calendar_disconnected": (
        "Your Google Calendar connection has stopped working. Please ask the person who "
        "invited you to reconnect it."
    ),
}

_HE: Final = {
    "welcome": "היי! אני העוזר האישי שלך. עוד שני צעדים קצרים וסיימנו.",
    "zone_confirm": "האם אזור הזמן שלך הוא {zone}?",
    "option_correct": "נכון",
    "option_change": "לשנות",
    "zone_ask_city": "באיזו עיר את/ה? לדוגמה: תל אביב, או Europe/London.",
    "zone_retry": (
        "לא הצלחתי להתאים את זה לאזור זמן. אפשר לשלוח שם של עיר, לדוגמה תל אביב, "
        "או אזור כמו Europe/London."
    ),
    "zone_retry_keep": "או להשיב 1 כדי להשאיר את {zone}.",
    "zone_set": "אזור הזמן שלך נקבע ל-{zone}.",
    "connect_link": (
        "עכשיו נחבר את יומן Google שלך. יש לפתוח את הקישור ולעקוב אחרי השלבים. "
        "הוא עובד פעם אחת ותוקפו קצר:\n{url}"
    ),
    "all_set": "הכול מוכן! יומן Google שלך מחובר.",
    "agent_unavailable": "סליחה, לא הצלחתי לענות כרגע. כדאי לנסות שוב בעוד דקה.",
    "calendar_disconnected": (
        "החיבור ליומן Google שלך הפסיק לעבוד. כדאי לבקש ממי שהזמין אותך לחבר אותו מחדש."
    ),
}

ONBOARDING_TEXT: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"en": MappingProxyType(_EN), "he": MappingProxyType(_HE)}
)


def t(key: str, language: str, **values: str) -> str:
    """The ``key`` line in ``language`` (English if unknown), with ``values`` filled in."""
    table = ONBOARDING_TEXT.get(language, ONBOARDING_TEXT[ENGLISH])
    return table[key].format_map(values)


__all__ = ["ONBOARDING_TEXT", "t"]
