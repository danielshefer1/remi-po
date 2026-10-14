"""The fixed replies Iteration 02 sends, and the time windows that govern them.

Constants rather than settings: Iteration 04 replaces the acknowledgement with the agent, and
the invite-only line is not something an operator should tune per environment.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

#: What an allowlisted sender gets until there is an agent to answer.
ACK_TEXT: Final = "Thanks, got it! I can't answer messages yet, but I will soon."

#: What everyone else gets, at most once per :data:`INVITE_ONLY_MUTE`.
INVITE_ONLY_TEXT: Final = "Hi! Sorry, this assistant is invite-only, so I can't help here."

#: WhatsApp's customer-service window: a free-form reply is only allowed within 24 hours of
#: the user's last message. Outside it, the send is rejected (Graph error 131047).
SERVICE_WINDOW: Final = timedelta(hours=24)

#: Replies are not attempted this close to the window's end, so a message redelivered after
#: an outage is not answered with a send that fails halfway through a retry.
SERVICE_WINDOW_MARGIN: Final = timedelta(minutes=5)

#: Without this a stranger could make us send one reply per message they send.
INVITE_ONLY_MUTE: Final = timedelta(hours=24)

__all__ = [
    "ACK_TEXT",
    "INVITE_ONLY_MUTE",
    "INVITE_ONLY_TEXT",
    "SERVICE_WINDOW",
    "SERVICE_WINDOW_MARGIN",
]
