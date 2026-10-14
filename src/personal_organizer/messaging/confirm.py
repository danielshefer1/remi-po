"""The Add/Cancel choice under a proposed write (Iteration 04, D5).

Like the zone choice, it is never stored: the caller rebuilds it from
``repositories.pending.current`` on every message, so "1" means Add exactly while a proposal
is pending. Add is first, so "1" adds and "2" cancels. The lines live here in both languages,
in the same shape as ``onboarding_text.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.choices import Choice, Option
from personal_organizer.messaging.language import ENGLISH

CONFIRM_ADD: Final = "confirm:add"
CONFIRM_CANCEL: Final = "confirm:cancel"

#: Both languages, whatever the tenant's: people answer in the language they think in.
_ADD_ALIASES: Final = ("yes", "y", "ok", "add", "כן", "הוסף", "להוסיף", "👍")
_CANCEL_ALIASES: Final = ("no", "n", "cancel", "לא", "ביטול", "בטל")

_EN: Final = {
    "confirm_question": "Add this to your calendar?",
    "option_add": "Add",
    "option_cancel": "Cancel",
}

_HE: Final = {
    "confirm_question": "להוסיף ליומן?",
    "option_add": "להוסיף",
    "option_cancel": "ביטול",
}

CONFIRM_TEXT: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"en": MappingProxyType(_EN), "he": MappingProxyType(_HE)}
)


def _t(key: str, language: str) -> str:
    return CONFIRM_TEXT.get(language, CONFIRM_TEXT[ENGLISH])[key]


def confirm_choice(summary: str, language: str) -> Choice:
    """``summary`` (what would be written, in the tenant's words) and "1 Add / 2 Cancel"."""
    return Choice(
        prompt=f"{summary}\n\n{_t('confirm_question', language)}",
        options=(
            Option(CONFIRM_ADD, _t("option_add", language), _ADD_ALIASES),
            Option(CONFIRM_CANCEL, _t("option_cancel", language), _CANCEL_ALIASES),
        ),
    )


__all__ = ["CONFIRM_ADD", "CONFIRM_CANCEL", "CONFIRM_TEXT", "confirm_choice"]
