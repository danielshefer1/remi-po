"""Copy for the connect pages, in both languages (D11).

The chat strings are in :mod:`personal_organizer.messaging.onboarding_text` and are written for
WhatsApp. These are page copy, rendered into autoescaped HTML. A page shown before we know who
is asking (a refused link, a refused callback) carries both languages, Hebrew first.

``connect.number_hint`` is the label the page shows above the number's last digits.
"""

from __future__ import annotations

from typing import Final

LANGUAGES: Final = ("he", "en")
BILINGUAL: Final = LANGUAGES
_RIGHT_TO_LEFT: Final = frozenset({"he"})

PAGE_TEXT: Final[dict[str, dict[str, dict[str, str]]]] = {
    "connect": {
        "he": {
            "title": "חיבור יומן Google",
            "body": "ב-Google תתבקשו לבחור חשבון ולאשר גישה ליומן.",
            "number_hint": "עבור מספר הוואטסאפ שמסתיים בספרות",
            "button": "המשך ל-Google",
            "revoke": "אפשר לבטל את הגישה בכל עת בחשבון Google.",
            "apple": "משתמשים ביומן של Apple?",
            "apple_body": "הכי פשוט לנהל את האירועים ביומן Google ולראות אותם גם באייפון. "
            "ככה מסנכרנים:",
            "apple_step_1": "באייפון: הגדרות ← אפליקציות ← לוח שנה ← חשבונות לוח שנה ← "
            "הוספת חשבון ← Google.",
            "apple_step_2": "מתחברים עם חשבון Google, ומוודאים ש'לוחות שנה' מופעל.",
            "apple_step_3": "באפליקציית לוח השנה של Apple יופיעו מעכשיו גם האירועים מ-Google, "
            "כולל מה שאני מוסיף.",
            "apple_after": "ואז מחברים כאן את אותו חשבון Google:",
        },
        "en": {
            "title": "Connect Google Calendar",
            "body": "Google will ask which account to use and for access to your calendar.",
            "number_hint": "For the WhatsApp number ending in",
            "button": "Continue to Google",
            "revoke": "You can remove access at any time in your Google Account.",
            "apple": "Use Apple Calendar?",
            "apple_body": "The easiest way is to keep your events in Google Calendar and see "
            "them on your iPhone too. Here's how to sync them:",
            "apple_step_1": "On your iPhone, go to Settings, Apps, Calendar, Calendar Accounts, "
            "Add Account, then Google.",
            "apple_step_2": "Sign in with your Google account and make sure Calendars is on.",
            "apple_step_3": "Your Google events, including the ones I add, now show up in "
            "Apple Calendar.",
            "apple_after": "Then connect that same Google account here:",
        },
    },
    "connected": {
        "he": {"title": "היומן מחובר", "body": "הכול מוכן. אפשר לחזור לוואטסאפ."},
        "en": {"title": "Calendar connected", "body": "All set. You can go back to WhatsApp now."},
    },
    "already_connected": {
        "he": {
            "title": "היומן כבר מחובר",
            "body": "אין צורך בקישור הזה. אפשר לחזור לוואטסאפ.",
        },
        "en": {
            "title": "Your calendar is already connected",
            "body": "There is nothing more to do with this link. You can go back to WhatsApp.",
        },
    },
    "link_unusable": {
        "he": {
            "title": "הקישור כבר לא בתוקף",
            "body": "פג תוקפו או שכבר השתמשו בו. שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "This link has expired",
            "body": "It has expired or was already used. "
            "Send any message to the bot on WhatsApp and it will send you a new one.",
        },
    },
    "failed": {
        "he": {
            "title": "היומן לא חובר",
            "body": "שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "Calendar not connected",
            "body": "Send any message to the bot on WhatsApp and it will send you a new link.",
        },
    },
    "not_ready": {
        "he": {
            "title": "החיבור עוד לא הושלם",
            "body": "אם סיימתם להתחבר ב-Google, רעננו את הדף בעוד כמה שניות. "
            "אחרת, שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "Not connected yet",
            "body": "If you finished signing in with Google, reload this page in a few seconds. "
            "Otherwise, send any message to the bot on WhatsApp for a new link.",
        },
    },
    "unavailable_new_link": {
        "he": {
            "title": "משהו השתבש אצלנו",
            "body": "נסו שוב בעוד דקה: שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "Something went wrong on our side",
            "body": "Please try again in a minute: "
            "send any message to the bot on WhatsApp for a new link.",
        },
    },
    "unavailable_retry": {
        "he": {"title": "משהו השתבש אצלנו", "body": "רעננו את הדף בעוד דקה."},
        "en": {
            "title": "Something went wrong on our side",
            "body": "Please reload this page in a minute.",
        },
    },
}


def page_language(language: str | None) -> str:
    """The tenant's language if a page exists in it, else English."""
    return language if language in LANGUAGES else "en"


def text_direction(language: str) -> str:
    return "rtl" if language in _RIGHT_TO_LEFT else "ltr"


def page_text(key: str, language: str | None) -> dict[str, str]:
    return PAGE_TEXT[key][page_language(language)]


__all__ = ["BILINGUAL", "LANGUAGES", "PAGE_TEXT", "page_language", "page_text", "text_direction"]
