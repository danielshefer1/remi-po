"""The confirm under a proposed write (Iteration 04, D5): the model reads the answer.

The proposal goes out as its summary and a plain question, with no numbered options. While it
is pending (``repositories.pending.current``), the next message goes to :func:`decide` first:
one small model call that sees only the summary and that message, never the history or a
calendar, and answers ADD, CANCEL or OTHER. Code acts on the answer; the agent's tools never
write. Anything but a clear ADD or CANCEL is ``None``, which the caller treats as any message.
The question lives here in both languages, in the same shape as ``onboarding_text.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.conversation import MAX_TOKENS
from personal_organizer.messaging.language import ENGLISH

CONFIRM_ADD: Final = "confirm:add"
CONFIRM_CANCEL: Final = "confirm:cancel"

_EN: Final = {
    "confirm_question": "Add this to your calendar?",
    "confirm_question_many": "Add all of these to your calendar?",
}
_HE: Final = {"confirm_question": "להוסיף ליומן?", "confirm_question_many": "להוסיף את הכול ליומן?"}

CONFIRM_TEXT: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"en": MappingProxyType(_EN), "he": MappingProxyType(_HE)}
)

_DECIDE: Final = """\
You asked someone whether to add this to their calendar (one event, or several):

{summary}

Their reply follows, in Hebrew or English. Answer with exactly one word: ADD if they agree to \
add all of it as it is, CANCEL if they decline all of it, or OTHER if they want any of it \
changed or left out, ask something, talk about something else, or you are not sure."""

_ANSWERS: Final = MappingProxyType({"ADD": CONFIRM_ADD, "CANCEL": CONFIRM_CANCEL})


def _t(key: str, language: str) -> str:
    return CONFIRM_TEXT.get(language, CONFIRM_TEXT[ENGLISH])[key]


def confirm_prompt(summary: str, language: str, *, many: bool = False) -> str:
    """``summary`` (what would be written, in the tenant's words) and the question, asked
    about all of them when ``many`` events are proposed at once."""
    question = _t("confirm_question_many" if many else "confirm_question", language)
    return f"{summary}\n\n{question}"


async def decide(summary: str, text: str, *, llm: LLMProvider) -> str | None:
    """:data:`CONFIRM_ADD`, :data:`CONFIRM_CANCEL`, or ``None`` when ``text`` is neither.

    Provider errors pass through, as in ``converse``: the caller retries or falls back.
    """
    messages = [Message("system", _DECIDE.format(summary=summary)), Message("user", text)]
    completion = await llm.complete(messages, max_tokens=MAX_TOKENS)
    if completion.finish_reason == "max_tokens":
        return None
    return _ANSWERS.get(completion.content.strip().strip(".").upper())


__all__ = ["CONFIRM_ADD", "CONFIRM_CANCEL", "CONFIRM_TEXT", "confirm_prompt", "decide"]
