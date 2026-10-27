"""Reading an okay said in the group, not to Remi (#51).

After ``who_is_free`` asks a member to okay sharing (``repositories.groups.ask``), their next
messages, while the ask is open, go to :func:`approves`: one small model call that sees only
that message and answers YES or NO, the same kind as ``confirm.decide``. Code acts on the
answer, and only ever on the writer's own sharing. Anything but a clear YES is ``False``.
"""

from __future__ import annotations

from typing import Final

from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.conversation import MAX_TOKENS

_APPROVES: Final = """\
In a family's WhatsApp group, you asked someone whether you may show the group when they are \
busy, from their own calendar: only the times, never what the events are.

Their next message in the group follows, in Hebrew or English, and may be about anything. \
Read it as a person would, in either language, however they phrase it. Answer with exactly \
one word: YES if the writer themselves okays showing their own busy times here, or NO if they \
decline, talk about something else, speak for someone else, or you are not sure."""


async def approves(text: str, *, llm: LLMProvider) -> bool:
    """Does ``text`` okay showing its writer's own busy times in the group?

    Provider errors pass through, as in ``confirm.decide``: the caller retries or stays silent.
    """
    messages = [Message("system", _APPROVES), Message("user", text)]
    completion = await llm.complete(messages, max_tokens=MAX_TOKENS)
    if completion.finish_reason == "max_tokens":
        return False
    return completion.content.strip().strip(".").upper() == "YES"


__all__ = ["approves"]
