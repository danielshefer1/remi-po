"""A channel-neutral choice, rendered as numbered text (D9).

Linked-device WhatsApp cannot be relied on to show buttons. Onboarding's time zone step
depends on a choice. So a choice is a messaging concept, not a
channel feature. It goes out as plain text through ``send_once``, and ``OutboundChannel`` does
not change. Native rendering (Meta buttons, GOWA polls) is a later ``send_choice`` on the
channels that can do it. :func:`resolve` already accepts what those send back: the picked
option's id arrives as the inbox row's ``reply_id``.

**The caller owns which choice is open.** That is ``tenants.onboarding_step`` now and
``pending_actions`` later. The caller rebuilds the same :class:`Choice` from that state on
every message. No choice is ever stored, so there is nothing to expire and nothing that can
disagree with the state. A reply that resolves to nothing returns ``None``, and the caller
decides between re-prompting and treating it as free text.

At most nine options, so every numbered reply is a single digit and "12" can never be read
as "1" plus noise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import ENGLISH, strip_bidi

MAX_OPTIONS: Final = 9

#: "1", "1." or "1)", with surrounding whitespace ignored. Nothing else counts as a number.
_NUMBERED: Final = re.compile(r"\s*([1-9])\s*[.)]?\s*")

#: Edges that decorate a word reply without changing it: "Yes!", "ok.", "'no'".
_EDGES: Final = " \t\r\n.!?,;:'\""

_HINTS: Final = MappingProxyType({"en": "Reply with a number.", "he": "נא להשיב במספר."})


@dataclass(frozen=True, slots=True)
class Option:
    #: Stable and channel-neutral. A native button or list row carries it as its id.
    id: str
    #: What the numbered line shows, in the tenant's language.
    label: str
    #: Other words that mean this option, matched case-folded. Callers list both languages,
    #: because a Hebrew reader may answer an English prompt in Hebrew and the other way round.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Choice:
    prompt: str
    options: tuple[Option, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.options) <= MAX_OPTIONS:
            msg = f"a choice has 1 to {MAX_OPTIONS} options, not {len(self.options)}"
            raise ValueError(msg)
        ids = [option.id for option in self.options]
        if len(set(ids)) != len(ids):
            msg = "option ids must be distinct"
            raise ValueError(msg)


def _fold(text: str) -> str:
    return " ".join(text.strip(_EDGES).split()).casefold()


def render_text(choice: Choice, language: str) -> str:
    """The prompt, one numbered line per option, and "Reply with a number" (or its Hebrew)."""
    numbered = [f"{number}. {option.label}" for number, option in enumerate(choice.options, 1)]
    hint = _HINTS.get(language, _HINTS[ENGLISH])
    return "\n".join([choice.prompt, "", *numbered, "", hint])


def resolve(choice: Choice, *, reply_id: str | None, text: str | None) -> Option | None:
    """The option a reply picks, or ``None``.

    Tried in order: ``reply_id`` equal to an option id (a Meta button or a GOWA selection);
    then the text as a single digit in range; then the text as a label or alias, case-folded.
    An unknown ``reply_id``, from a button of an older message, falls through to the text.
    """
    if reply_id is not None:
        for option in choice.options:
            if option.id == reply_id:
                return option
    if text is None:
        return None
    text = strip_bidi(text)
    if (numbered := _NUMBERED.fullmatch(text)) is not None:
        index = int(numbered.group(1)) - 1
        return choice.options[index] if index < len(choice.options) else None
    wanted = _fold(text)
    if not wanted:
        return None
    for option in choice.options:
        if wanted == _fold(option.label) or any(wanted == _fold(a) for a in option.aliases):
            return option
    return None


__all__ = ["MAX_OPTIONS", "Choice", "Option", "render_text", "resolve"]
