"""Splitting long replies to fit a channel's per-message limit.

The channel adapter refuses an over-long body rather than truncating it (a truncated
"Your appointment is at" is worse than an error), so splitting is the caller's decision and
happens here, before anything is recorded for sending.
"""

from __future__ import annotations

from typing import Final

#: WhatsApp's text body limit.
WHATSAPP_TEXT_LIMIT: Final = 4096

#: A line holding only this ends one text and starts the next.
BREAK: Final = "---"

#: The most texts one reply becomes; the rest ride in the last.
MAX_BUBBLES: Final = 3

#: Preferred break points, best first: a paragraph, a line, a sentence, a word.
_BREAKS: Final = ("\n\n", "\n", ". ", " ")


def split_text(body: str, limit: int = WHATSAPP_TEXT_LIMIT) -> list[str]:
    """Split ``body`` into chunks of at most ``limit`` characters.

    Breaks at the latest paragraph, line, sentence or word boundary that fits, falling back
    to a hard cut only for a single unbroken run longer than ``limit``. Joining the chunks
    reproduces ``body`` exactly -- no separator is dropped or added -- so nothing the user
    was meant to read is lost at a boundary. An empty body is one empty chunk.
    """
    if limit < 1:
        msg = "limit must be positive"
        raise ValueError(msg)
    chunks: list[str] = []
    rest = body
    while len(rest) > limit:
        window = rest[:limit]
        cut = 0
        for separator in _BREAKS:
            index = window.rfind(separator)
            if index > 0:
                cut = index + len(separator)
                break
        if cut == 0:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    chunks.append(rest)
    return chunks


def bubbles(reply: str) -> list[str]:
    """Split ``reply`` into the texts to send, at lines that are exactly :data:`BREAK`.

    Parts are stripped and empty ones dropped; past :data:`MAX_BUBBLES` the rest is joined
    into the last with a blank line. An empty reply is one empty part.
    """
    parts: list[str] = []
    current: list[str] = []
    for line in [*reply.splitlines(), BREAK]:
        if line.strip() == BREAK:
            if text := "\n".join(current).strip():
                parts.append(text)
            current = []
        else:
            current.append(line)
    if len(parts) > MAX_BUBBLES:
        parts[MAX_BUBBLES - 1 :] = ["\n\n".join(parts[MAX_BUBBLES - 1 :])]
    return parts or [""]


__all__ = ["BREAK", "MAX_BUBBLES", "WHATSAPP_TEXT_LIMIT", "bubbles", "split_text"]
