"""Every fixed line onboarding and the agent send, in both languages (D11).

One table, so a missing translation is a failing test (``test_onboarding_text.py`` checks
that the key sets and the ``{placeholders}`` match) rather than an English line in a Hebrew
conversation. The templates hold no braces besides their placeholders, and values are user-
or system-supplied strings (a zone name, a URL) inserted with ``format_map``. A value is
never itself parsed as a template.

``connect_link`` ends with the URL on a line of its own. WhatsApp linkifies a bare URL
reliably, but not one with a character glued to it. ``all_set`` is sent by the
``onboarding:connected`` task (PR 4). ``agent_unavailable``, ``calendar_disconnected`` and
the confirm outcomes (``event_*``, ``proposal_closed``) are the agent's
(``messaging.agent``). ``reminder`` is sent by the reminder job, and the ``briefing_*``
lines by the morning briefing (Iteration 05). ``{zone}`` is a ``timezones.zone_label``
("Jerusalem"), never an IANA name. No line offers numbered options: people answer in words.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import ENGLISH

_EN: Final = {
    "welcome": (
        "Hi! I'm your personal organizer. I can check your Google Calendar and add things to "
        "it, right here in WhatsApp."
    ),
    "zone_guessed": (
        "I've set your clock to {zone} time, going by your number. If you're somewhere else, "
        "just tell me which city."
    ),
    "zone_ask_city": "First, which city are you in? That way I'll get your times right.",
    "zone_retry": "Hmm, I don't know that one. What's the nearest big city?",
    "zone_set": "Got it, you're on {zone} time.",
    "connect_link": (
        "Now let's connect your Google Calendar. Open this link and sign in with Google. "
        "It works once and expires soon:\n{url}"
    ),
    "all_set": (
        "You're all set, your Google Calendar is connected! Ask me things like "
        '"what do I have tomorrow?" or "add the dentist on Thursday at 9". '
        "I'll also send you a quick look at your day every morning at 7:30. Just tell me if "
        "you'd like another time, or none."
    ),
    "agent_unavailable": "Sorry, I couldn't answer just now. Try me again in a minute.",
    "calendar_disconnected": (
        "Your Google Calendar connection has stopped working. Please ask the person who "
        "invited you to reconnect it."
    ),
    "event_added": "Done, it's in your calendar:\n{summary}",
    "event_cancelled": "No problem, I didn't add it.",
    "proposal_closed": (
        "That suggestion is no longer open, so I didn't add anything. Tell me again what to add."
    ),
    "event_not_added": (
        "I couldn't add it just now. Check your calendar, and ask me again if it isn't there."
    ),
    "reminder": "Just a reminder: {text}",
    "briefing_intro": "Good morning! Here's your day:",
    "briefing_reminders_heading": "Reminders:",
    "briefing_empty": "Good morning! Nothing on your calendar today. Have a good one.",
    "briefing_calendar_unavailable": (
        "I couldn't reach your calendar this morning, so today's events aren't here. "
        "Ask me later and I'll check again."
    ),
}

_HE: Final = {
    "welcome": (
        "היי! אני העוזר האישי שלך. אפשר לשאול אותי מה יש ביומן Google שלך ולבקש ממני להוסיף "
        "אליו דברים, ישר מכאן בוואטסאפ."
    ),
    "zone_guessed": (
        "כיוונתי לך את השעון לשעון {zone}, לפי המספר שלך. ואם זה לא נכון, "
        "אפשר פשוט לכתוב לי באיזו עיר."
    ),
    "zone_ask_city": "קודם כל, מה העיר שלך? ככה אדע לתת לך את השעות הנכונות.",
    "zone_retry": "לא הכרתי את המקום הזה. מה העיר הגדולה הכי קרובה?",
    "zone_set": "מעולה, כיוונתי לשעון {zone}.",
    "connect_link": (
        "עכשיו נחבר את יומן Google שלך. צריך לפתוח את הקישור ולהתחבר עם Google. "
        "הוא עובד פעם אחת ותוקפו קצר:\n{url}"
    ),
    "all_set": (
        'הכול מוכן, יומן Google שלך מחובר! אפשר לשאול אותי למשל "מה יש לי מחר?" '
        'או "תוסיף רופא שיניים ביום חמישי בתשע". '
        "וכל בוקר ב-7:30 אשלח לך הצצה קצרה ליום שלך. אם נוח לך שעה אחרת, או שעדיף בלי, "
        "אפשר פשוט לכתוב לי."
    ),
    "agent_unavailable": "סליחה, לא הצלחתי לענות כרגע. אפשר לנסות שוב בעוד דקה.",
    "calendar_disconnected": (
        "החיבור ליומן Google שלך הפסיק לעבוד. כדאי לבקש ממי שהזמין אותך לחבר אותו מחדש."
    ),
    "event_added": "בוצע, זה ביומן:\n{summary}",
    "event_cancelled": "אין בעיה, לא הוספתי.",
    "proposal_closed": "ההצעה הזו כבר לא בתוקף, אז לא הוספתי כלום. אפשר לבקש שוב מה להוסיף.",
    "event_not_added": (
        "לא הצלחתי להוסיף את זה כרגע. כדאי לבדוק ביומן, ולבקש שוב אם האירוע לא שם."
    ),
    "reminder": "תזכורת: {text}",
    "briefing_intro": "בוקר טוב! זה מה שיש לך היום:",
    "briefing_reminders_heading": "תזכורות:",
    "briefing_empty": "בוקר טוב! היומן פנוי היום. שיהיה יום טוב.",
    "briefing_calendar_unavailable": (
        "לא הצלחתי להגיע ליומן שלך הבוקר, אז האירועים של היום לא מופיעים כאן. "
        "אפשר לשאול אותי שוב בהמשך ואבדוק."
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
