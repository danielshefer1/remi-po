"""Outings (Iteration 07): a plan the group agreed on, posted as a poll; a vote "in" adds it.

**The poll.** ``set_up_outing`` checks the event as ``propose_event`` does, stores it, and
posts one poll in the group: the plan as one line, and "I'm in" / "Can't make it". The poll
is the plan the family agreed on in the group, so its title, time and place are the only event
content Remi ever writes there. The poll joins the group's context as Remi's own line.

**Votes.** A vote names the option picked (``body``) and the poll it is on (``reply_id``). Only
a tenant's vote counts. "In" adds the event to the voter's own calendar, read in their own
session, at most once: :func:`~personal_organizer.db.repositories.outings.claim_event` commits
before the create, so a second "in", a vote back in after "out", or a re-run never adds it
again. The line saying how it went goes to the voter privately, never to the group. "Out" after
the event was added says, privately, that it stayed in their calendar.

**Answers said, not voted.** A message not for Remi, from a tenant with an open outing they
haven't answered, gets one small model call (:func:`classify`), shaped like
``confirm.decide``: it sees only the outings and that message. A reason they can't come, or a
plain "I'm in", is stored as their answer so follow-ups stop. Nothing is sent, and an "in" read
from text adds nothing: only a vote confirms.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.group import GroupPoll
from personal_organizer.db.repositories.connections import active_connection
from personal_organizer.db.repositories.groups import add_context
from personal_organizer.db.repositories.outings import (
    answer_of,
    claim_event,
    create_poll,
    open_polls,
    poll_by_message,
    set_answer,
    set_poll_message,
)
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.calendar import CalendarProvider
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.agent import (
    MAX_FIELD,
    add_event,
    check_event,
    fit_message,
    outing_event,
    outing_line,
)
from personal_organizer.messaging.calendar import TenantCalendar
from personal_organizer.messaging.conversation import MAX_TOKENS
from personal_organizer.messaging.groups.addressing import group_key
from personal_organizer.messaging.groups.answers import unanswered
from personal_organizer.messaging.groups.gate import REMI, GroupTurn
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once, sent_message_id
from personal_organizer.messaging.tenancy import phone_of

#: The poll's two options, by the language of the message that asked for it.
OUTING_OPTIONS: Final = {"en": ("I'm in", "Can't make it"), "he": ("אני בפנים", "לא מסתדר לי")}
#: The outbox kind of the poll, the reply to the message that asked for it.
POLL_KIND: Final = "group:poll"
#: The outbox kind of the private line a vote gets, the reply to the vote.
VOTE_KIND: Final = "group:vote"

POSTED: Final = "ok: the poll is posted"
_UTC: Final = ZoneInfo("UTC")

_CLASSIFY: Final = """\
Someone in a family's WhatsApp group wrote the message that follows, in Hebrew or English. It \
was written to the group, not to you. These outings are planned, waiting for their answer:

{outings}

Answer with exactly one line: IN n if they say they're coming to outing n; OUT n: <why, in a \
few of their own words> if they say they can't come to outing n (just OUT n if they gave no \
reason); or OTHER if the message is about something else, or you are not sure."""

_ANSWER: Final = re.compile(r"(IN|OUT)\s+(\d+)\.?(?::\s*(.*))?", re.IGNORECASE | re.DOTALL)


async def set_up_outing(
    arguments: Mapping[str, Any],
    *,
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    turn: GroupTurn,
    zone: ZoneInfo,
    language: str,
) -> str:
    """The ``set_up_outing`` tool: post the plan as a poll. Returns what the model reads."""
    event = check_event(arguments, zone)
    if isinstance(event, str):
        return event
    # An all-day outing starts at local midnight: today's still counts until the day ends.
    if (event.end if event.all_day else event.start) <= turn.now:
        return "error: that time has passed"
    in_option, out_option = OUTING_OPTIONS[language]
    async with db.system_session() as session:
        poll = await create_poll(
            session,
            group_id=turn.group_id,
            inbox_id=row.id,
            event=event,
            language=language,
            in_option=in_option,
            out_option=out_option,
        )
    if poll.provider_message_id is not None:
        return POSTED  # a re-run of this message: the poll is out
    # Built from the stored poll, so a re-run posts the plan that was stored.
    question = outing_line(poll, zone)
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=POLL_KIND,
        recipient_key=group_key(turn.jid),
        to=turn.jid,
        text=question,
        poll_options=(poll.in_option, poll.out_option),
    )
    message_id = await sent_message_id(db, row.id, POLL_KIND)
    if message_id is None:
        return "error: the poll didn't go out; say you couldn't post it just now"
    async with db.system_session() as session:
        await set_poll_message(session, poll.id, message_id)
        await add_context(
            session,
            turn.group_id,
            sender_name=REMI,
            body=f"{question} ({poll.in_option} / {poll.out_option})",
            sent_at=turn.now,
        )
    return POSTED


async def handle_vote(
    row: InboxRow,
    channel: OutboundChannel,
    turn: GroupTurn,
    *,
    db: Database,
    calendar: CalendarProvider | None,
) -> None:
    """Store a tenant's vote; on "in", add the event to their calendar, once."""
    if turn.tenant_id is None or row.reply_id is None:
        return
    async with db.system_session() as session:
        poll = await poll_by_message(session, turn.group_id, row.reply_id)
    # By its end, not its start: an all-day outing still counts on its day.
    if poll is None or poll.status != "open" or poll.ends_at <= turn.now:
        return
    options = {poll.in_option: "in", poll.out_option: "out", None: None}
    if row.body not in options:
        return
    answer = options[row.body]
    tenant_id = TenantId(turn.tenant_id)
    async with db.tenant_session(tenant_id) as session:
        earlier = await answer_of(session, tenant_id, poll.id)
        # A reason said in the chat stays with a vote "out", and with a cleared vote.
        reason = earlier.reason if earlier is not None and answer != "in" else None
        await set_answer(session, tenant_id, poll.id, answer=answer, reason=reason)
        tenant = await get_tenant(session, tenant_id)
        account = await active_connection(session, tenant_id)
    if tenant is None:
        return
    if answer == "out" and earlier is not None and earlier.claimed_at is not None:
        text = t("outing_kept", tenant.language)
    elif answer == "in":
        # Committed before the create: a crash or retry after this never adds it again.
        # shortcut: as in agent_turn, a send that fails for sure after the create leaves them
        # without the "added" line. Store the line with the claim if that shows up in the logs.
        async with db.tenant_session(tenant_id) as session:
            claimed = await claim_event(session, tenant_id, poll.id, turn.now)
        if not claimed:
            return
        zone = ZoneInfo(tenant.timezone or "UTC")
        text = await add_event(
            outing_event(poll, zone),
            calendar=TenantCalendar(db, tenant_id, provider=calendar, account=account),
            zone=zone,
            language=tenant.language,
        )
    else:
        return  # counted; nothing to add or say
    phone = await phone_of(db, tenant_id)
    if phone is None:
        return
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=VOTE_KIND,
        recipient_key=f"tel:{phone}",
        to=phone,
        text=fit_message(text),
    )


async def read_answer(row: InboxRow, turn: GroupTurn, *, db: Database, llm: LLMProvider) -> None:
    """Store an answer said in the group, not to Remi, to an outing the sender hasn't answered.

    Costs no model call unless there is such an outing. Provider errors pass through."""
    if turn.tenant_id is None or not row.body:
        return
    async with db.system_session() as session:
        polls = await open_polls(session, turn.group_id, turn.now)
    if not polls:
        return
    tenant_id = TenantId(turn.tenant_id)
    async with db.tenant_session(tenant_id) as session:
        tenant = await get_tenant(session, tenant_id)
        answers = [await answer_of(session, tenant_id, poll.id) for poll in polls]
    waiting = [poll for poll, given in zip(polls, answers, strict=True) if unanswered(given)]
    if not waiting or tenant is None:
        return
    found = await classify(waiting, row.body, llm=llm, zone=ZoneInfo(tenant.timezone or "UTC"))
    if found is None:
        return
    poll_id, answer, reason = found
    async with db.tenant_session(tenant_id) as session:
        await set_answer(session, tenant_id, poll_id, answer=answer, reason=reason)


async def classify(
    polls: Sequence[GroupPoll], text: str, *, llm: LLMProvider, zone: ZoneInfo = _UTC
) -> tuple[UUID, str, str | None] | None:
    """``(poll_id, 'in', None)``, ``(poll_id, 'out', reason)``, or ``None`` when ``text``
    answers none of ``polls``. Provider errors pass through, as in ``confirm.decide``."""
    outings = "\n".join(f"{n}. {outing_line(poll, zone)}" for n, poll in enumerate(polls, 1))
    messages = [Message("system", _CLASSIFY.format(outings=outings)), Message("user", text)]
    completion = await llm.complete(messages, max_tokens=MAX_TOKENS)
    if completion.finish_reason == "max_tokens":
        return None
    match = _ANSWER.fullmatch(completion.content.strip())
    if match is None or not 1 <= int(match[2]) <= len(polls):
        return None
    poll = polls[int(match[2]) - 1]
    if match[1].upper() == "IN":
        return poll.id, "in", None
    return poll.id, "out", (match[3] or "").strip()[:MAX_FIELD] or None


__all__ = [
    "OUTING_OPTIONS",
    "POLL_KIND",
    "VOTE_KIND",
    "classify",
    "handle_vote",
    "read_answer",
    "set_up_outing",
]
