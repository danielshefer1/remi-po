"""Every fixed line onboarding and the agent send, in both languages (D11).

One table, so a missing translation is a failing test (``test_onboarding_text.py`` checks
that the key sets and the ``{placeholders}`` match) rather than an English line in a Hebrew
conversation. The templates hold no braces besides their placeholders, and values are user-
or system-supplied strings (a zone name, a URL) inserted with ``format_map``. A value is
never itself parsed as a template.

``connect_link`` ends setup: the calendar is optional, so it offers the link rather than
asking for it, and ends with the URL on a line of its own. WhatsApp linkifies a bare URL
reliably, but not one with a character glued to it. ``all_set`` is sent by the
``onboarding:connected`` task once Google connects, whenever that is (``all_set_pending``
instead when the import on connect failed), and says that it is synced about hourly and can be
read live instead, on request (#50). ``agent_unavailable``,
``calendar_not_connected`` (the link to connect Google, or reconnect it),
``calendar_disconnected`` (a failed add, with no link) and the confirm outcomes (``event_*``,
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
        "Hi! I'm your personal organizer, right here in WhatsApp. I can keep your calendar, "
        "remind you of things and remember what matters to you. If you use Google Calendar, "
        "you can connect it too."
    ),
    "zone_guessed": (
        "I've set your clock to {zone} time, going by your number. If you're somewhere else, "
        "just tell me which city."
    ),
    "zone_ask_city": "First, which city are you in? That way I'll get your times right.",
    "zone_retry": "Hmm, I don't know that one. What's the nearest big city?",
    "zone_set": "Got it, you're on {zone} time.",
    "connect_link": (
        "That's it, you're all set! Every morning at 7:30 I'll send you a quick look at your "
        "day; just tell me if you'd like another time, or none. Want me to see your Google "
        "Calendar too? Connect it here whenever you like. The link works once and expires "
        "soon, but I can always send you a new one:\n{url}"
    ),
    "all_set": (
        "Your Google Calendar is connected, and your events are in! I'll keep them in sync "
        "about every hour. If you'd rather I check Google live every time, just say so."
    ),
    "all_set_pending": (
        "Your Google Calendar is connected! Your events should be in within the hour, and after "
        "that I'll keep them in sync about every hour. If you'd rather I check Google live every "
        "time, just say so."
    ),
    "agent_unavailable": "Sorry, I couldn't answer just now. Try me again in a minute.",
    "calendar_not_connected": (
        "To connect your Google Calendar, just open the link below, and your Google events "
        "will show right alongside what I keep for you:\n{url}"
    ),
    "calendar_disconnected": (
        "I couldn't add it: your Google Calendar connection has stopped working. Just ask me "
        "and I'll send you a link to reconnect it."
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
    "group_details": "Here's what's on, just for you:",
    "group_shared_on": (
        "Done, from now on I'll show here when you're busy. Only the times, never what it is."
    ),
    "group_shared_off": "Okay, I won't show your schedule here anymore.",
    "outing_kept": (
        "Got it. It's still in your calendar, though, since I can't take things out yet. "
        "You can delete it there whenever you like."
    ),
    "outing_nudge_fallback": (
        "Hey, just checking if you're coming: {summary}. No pressure, just let me know."
    ),
}

_HE: Final = {
    "welcome": (
        "היי! אני העוזר האישי שלך, ישר כאן בוואטסאפ. אני יכול לנהל לך את היומן, לשלוח לך "
        "תזכורות ולזכור את מה שחשוב לך. ואם יש לך יומן Google, אפשר לחבר גם אותו."
    ),
    "zone_guessed": (
        "כיוונתי לך את השעון לשעון {zone}, לפי המספר שלך. ואם זה לא נכון, "
        "אפשר פשוט לכתוב לי באיזו עיר."
    ),
    "zone_ask_city": "קודם כל, מה העיר שלך? ככה אדע לתת לך את השעות הנכונות.",
    "zone_retry": "לא הכרתי את המקום הזה. מה העיר הגדולה הכי קרובה?",
    "zone_set": "מעולה, כיוונתי לשעון {zone}.",
    "connect_link": (
        "זהו, הכול מוכן! כל בוקר ב-7:30 אשלח לך הצצה קצרה ליום שלך, ואם נוח לך שעה אחרת, "
        "או שעדיף בלי, אפשר פשוט לכתוב לי. רוצה שאראה גם את יומן Google שלך? אפשר לחבר אותו "
        "כאן מתי שנוח. הקישור עובד פעם אחת ותוקפו קצר, אבל תמיד אפשר לבקש ממני חדש:\n{url}"
    ),
    "all_set": (
        "יומן Google שלך מחובר, והאירועים שלך כבר אצלי! אני מעדכן אותם מ-Google בערך פעם "
        "בשעה. אם עדיף שאבדוק ישר ב-Google בכל פעם, מספיק לבקש."
    ),
    "all_set_pending": (
        "יומן Google שלך מחובר! האירועים שלך אמורים להגיע אליי תוך שעה, ומשם אעדכן אותם "
        "מ-Google פעם בשעה בערך. אם עדיף שאבדוק ישר ב-Google בכל פעם, מספיק לבקש."
    ),
    "agent_unavailable": "סליחה, לא הצלחתי לענות כרגע. אפשר לנסות שוב בעוד דקה.",
    "calendar_not_connected": (
        "כדי לחבר את יומן Google, נכנסים לקישור כאן למטה, ואז האירועים מ-Google יופיעו לצד "
        "מה שכבר שמור אצלי:\n{url}"
    ),
    "calendar_disconnected": (
        "לא הצלחתי להוסיף את זה, כי החיבור ליומן Google שלך הפסיק לעבוד. אפשר לבקש ממני "
        "ואשלח קישור לחבר אותו מחדש."
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
    "group_details": "הנה מה שיש, רק בשבילך:",
    "group_shared_on": "סגור, מעכשיו אראה כאן מתי יש לך משהו. רק את השעות, אף פעם לא מה זה.",
    "group_shared_off": "בסדר, מעכשיו לא אראה כאן מתי יש לך משהו.",
    "outing_kept": (
        "קיבלתי. רק שזה עדיין ביומן שלך, כי אני עוד לא יכול להוריד משם דברים. "
        "אפשר למחוק את זה ביומן מתי שנוח."
    ),
    "outing_nudge_fallback": (
        "היי, רציתי לבדוק אם מסתדר לך להגיע: {summary}. בלי לחץ, רק לעדכן אותי."
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
