"""Every fixed line onboarding and the agent send, in both languages (D11).

One table, so a missing translation is a failing test (``test_onboarding_text.py`` checks
that the key sets and the ``{placeholders}`` match) rather than an English line in a Hebrew
conversation. The templates hold no braces besides their placeholders, and values are user-
or system-supplied strings (a zone name, a URL) inserted with ``format_map``. A value is
never itself parsed as a template.

``connect_link`` ends setup: the calendar is optional, so it offers the link rather than
asking for it, and ends with the URL on a line of its own. WhatsApp linkifies a bare URL
reliably, but not one with a character glued to it. ``all_set`` is sent by the
``onboarding:connected`` task once a calendar connects, whenever that is. ``agent_unavailable``,
``calendar_not_connected`` (with a fresh link), ``calendar_disconnected`` (no link: the
briefing and a failed add send it) and the confirm outcomes (``event_*``,
``proposal_closed``) are the agent's (``messaging.agent``). ``reminder`` is sent by the
reminder job, and the ``briefing_*`` lines by the morning briefing (Iteration 05).
``voice_unheard`` answers a voice note that couldn't be heard (Iteration 06). The ``group_*``
and ``outing_*`` lines are the family groups' (Iteration 07). ``{zone}`` is a
``timezones.zone_label``
("Jerusalem"), never an IANA name. No line offers numbered options: people answer in words.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import ENGLISH

_EN: Final = {
    "welcome": (
        "Hi! I'm your personal organizer, right here in WhatsApp. I can remind you of things, "
        "remember what matters to you and, if you connect it, keep an eye on your Google Calendar."
    ),
    "zone_guessed": (
        "I've set your clock to {zone} time, going by your number. If you're somewhere else, "
        "just tell me which city."
    ),
    "zone_ask_city": "First, which city are you in? That way I'll get your times right.",
    "zone_retry": "Hmm, I don't know that one. What's the nearest big city?",
    "zone_set": "Got it, you're on {zone} time.",
    "connect_link": (
        "You're all set! Every morning at 7:30 I'll send you a quick look at your "
        "day; just tell me if you'd like another time, or none. Want me to see your Google "
        "Calendar too? Connect it here whenever you like. The link works once and expires "
        "soon, but I can always send you a new one:\n{url}"
    ),
    "all_set": (
        "Your Google Calendar is connected! Ask me things like "
        '"what do I have tomorrow?" or "add the dentist on Thursday at 9", and your morning '
        "message will show your day's events too."
    ),
    "agent_unavailable": "Sorry, I couldn't answer just now. Try me again in a minute.",
    "calendar_not_connected": (
        "Your calendar isn't connected, so I can't see it. You can connect it here:\n{url}"
    ),
    "calendar_disconnected": (
        "Your Google Calendar connection has stopped working. Just ask me and I'll send you a "
        "link to reconnect it."
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
    "voice_unheard": "Sorry, I couldn't quite catch that voice note. Could you type it for me?",
    "briefing_intro": "Good morning! Here's your day:",
    "briefing_reminders_heading": "Reminders:",
    "briefing_empty": "Good morning! Nothing on your calendar today. Have a good one.",
    "briefing_calendar_unavailable": (
        "I couldn't reach your calendar this morning, so today's events aren't here. "
        "Ask me later and I'll check again."
    ),
    "group_details": "Here's what's on, just for you:",
    "group_shared_on": (
        "Done, from now on I'll show here when you're busy. Only the times, never what it is."
    ),
    "group_shared_off": "Okay, I won't show your schedule here anymore.",
    "outing_kept": (
        "Got it. It's still in your calendar, though, since I can't take things out yet. "
        "You can delete it there whenever you like."
    ),
    "outing_nudge_fallback": "Hey, just checking if you're coming: {summary}. No pressure.",
}

_HE: Final = {
    "welcome": (
        "היי! אני העוזר האישי שלך, ישר כאן בוואטסאפ. אפשר לבקש ממני תזכורות, לספר לי דברים "
        "שכדאי שאזכור, ואם מחברים את יומן Google, גם לשאול מה יש בו ולהוסיף אליו דברים."
    ),
    "zone_guessed": (
        "כיוונתי לך את השעון לשעון {zone}, לפי המספר שלך. ואם זה לא נכון, "
        "אפשר פשוט לכתוב לי באיזו עיר."
    ),
    "zone_ask_city": "קודם כל, מה העיר שלך? ככה אדע לתת לך את השעות הנכונות.",
    "zone_retry": "לא הכרתי את המקום הזה. מה העיר הגדולה הכי קרובה?",
    "zone_set": "מעולה, כיוונתי לשעון {zone}.",
    "connect_link": (
        "הכול מוכן! כל בוקר ב-7:30 אשלח לך הצצה קצרה ליום שלך, ואם נוח לך שעה אחרת, "
        "או שעדיף בלי, אפשר פשוט לכתוב לי. רוצה שאראה גם את יומן Google שלך? אפשר לחבר אותו "
        "כאן מתי שנוח. הקישור עובד פעם אחת ותוקפו קצר, אבל תמיד אפשר לבקש ממני חדש:\n{url}"
    ),
    "all_set": (
        'יומן Google שלך מחובר! אפשר לשאול אותי למשל "מה יש לי מחר?" '
        'או "תוסיף רופא שיניים ביום חמישי בתשע", ומעכשיו גם ההודעה של הבוקר תראה '
        "את האירועים של היום."
    ),
    "agent_unavailable": "סליחה, לא הצלחתי לענות כרגע. אפשר לנסות שוב בעוד דקה.",
    "calendar_not_connected": (
        "היומן שלך לא מחובר, אז אני לא רואה אותו. אפשר לחבר אותו כאן:\n{url}"
    ),
    "calendar_disconnected": (
        "החיבור ליומן Google שלך הפסיק לעבוד. אפשר לבקש ממני ואשלח קישור לחבר אותו מחדש."
    ),
    "event_added": "בוצע, זה ביומן:\n{summary}",
    "event_cancelled": "אין בעיה, לא הוספתי.",
    "proposal_closed": "ההצעה הזו כבר לא בתוקף, אז לא הוספתי כלום. אפשר לבקש שוב מה להוסיף.",
    "event_not_added": (
        "לא הצלחתי להוסיף את זה כרגע. כדאי לבדוק ביומן, ולבקש שוב אם האירוע לא שם."
    ),
    "reminder": "תזכורת: {text}",
    "voice_unheard": "סליחה, לא הצלחתי לשמוע טוב את ההודעה הקולית. אפשר לכתוב לי אותה?",
    "briefing_intro": "בוקר טוב! זה מה שיש לך היום:",
    "briefing_reminders_heading": "תזכורות:",
    "briefing_empty": "בוקר טוב! היומן פנוי היום. שיהיה יום טוב.",
    "briefing_calendar_unavailable": (
        "לא הצלחתי להגיע ליומן שלך הבוקר, אז האירועים של היום לא מופיעים כאן. "
        "אפשר לשאול אותי שוב בהמשך ואבדוק."
    ),
    "group_details": "הנה מה שיש, רק בשבילך:",
    "group_shared_on": "סגור, מעכשיו אראה כאן מתי יש לך משהו. רק את השעות, אף פעם לא מה זה.",
    "group_shared_off": "בסדר, מעכשיו לא אראה כאן מתי יש לך משהו.",
    "outing_kept": (
        "קיבלתי. רק שזה עדיין ביומן שלך, כי אני עוד לא יכול להוריד משם דברים. "
        "אפשר למחוק את זה ביומן מתי שנוח."
    ),
    "outing_nudge_fallback": "היי, רציתי לבדוק אם מסתדר לך להגיע: {summary}. בלי לחץ.",
}

ONBOARDING_TEXT: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"en": MappingProxyType(_EN), "he": MappingProxyType(_HE)}
)


def t(key: str, language: str, **values: str) -> str:
    """The ``key`` line in ``language`` (English if unknown), with ``values`` filled in."""
    table = ONBOARDING_TEXT.get(language, ONBOARDING_TEXT[ENGLISH])
    return table[key].format_map(values)


__all__ = ["ONBOARDING_TEXT", "t"]
