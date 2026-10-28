"""The group turn (Iteration 07): Remi answering in a family group when addressed.

**No event content in a group, by code.** ``who_is_free`` reads each sharing member's
calendar in their own tenant session and keeps only start, end and all-day, merged into
:func:`busy_blocks`; titles and locations are dropped before the model sees anything, so no
prompt or reply can leak them. Members who haven't okayed this group are never read: only
their names come back, in ``not_shared`` (asked to okay it) or ``stopped`` (said to stop).

**Details, privately.** After a turn in which ``who_is_free`` ran, an asker who is a member
gets the events of the people who granted them details, read then from ``detail_grantors``
in the same windows, each in its owner's own session. Code writes that message, never the
model, and sends it to the asker's phone, not the group, before the group reply: until that
reply is out a retry runs the turn again, and the details' own key keeps them to one send.

**Outings** (:mod:`.outings`). A poll vote goes to ``handle_vote`` and never to the model. A
message not for Remi goes to ``read_answer``, which costs a model call only when its sender
has an open outing to answer. ``set_up_outing`` posts one poll per message, members only.

**An okay said in the group** (#51). A message not for Remi, from a member ``who_is_free``
asked to okay sharing, goes to :func:`.approval.approves` first. A yes turns on that sender's
sharing, and only theirs, and the turn then answers as if the message were for Remi, so the
model can answer the who's-free question again; a no goes on to ``read_answer``.

**Silence.** The model may reply with nothing (the message was for someone else, or for a
person named Rami): nothing is sent. Not-addressed messages cost no model call beyond
``approves``' and ``read_answer``'s, each only when its sender has an open ask or outing. An
unavailable model or Composio raises for the job to retry, and the last attempt answers with
the fixed ``agent_unavailable`` line, as the agent does.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog

from personal_organizer.core.errors import (
    CalendarProviderUnavailableError,
    LLMRejectedError,
    LLMUnavailableError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Tenant
from personal_organizer.db.repositories.connections import active_connection
from personal_organizer.db.repositories.groups import (
    add_context,
    ask,
    clear_ask,
    detail_grantors,
    group_tenants,
    open_ask,
    recent_context,
    set_display_name,
    set_sharing,
)
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.calendar import CalendarEvent, CalendarProvider
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.agent import day_label, fit_message, read_window, shown_event
from personal_organizer.messaging.calendar import TenantCalendar
from personal_organizer.messaging.conversation import converse
from personal_organizer.messaging.groups.addressing import group_key
from personal_organizer.messaging.groups.approval import approves
from personal_organizer.messaging.groups.gate import GROUP_REPLY, POLL_VOTE, REMI, GroupTurn
from personal_organizer.messaging.groups.outings import handle_vote, read_answer, set_up_outing
from personal_organizer.messaging.groups.prompt import group_prompt
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.language import detect_language
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import already_sent, send_once
from personal_organizer.messaging.tenancy import phone_of

log = structlog.get_logger(__name__)

#: The outbox kind of the private message with granted details.
DETAILS_KIND: Final = "group:details"
#: Anyone with no name: a context line, or a member with no ``display_name``.
SOMEONE: Final = "Someone"
#: Where a group turn's times are when neither the asker nor any sharer has a zone.
DEFAULT_ZONE: Final = "Asia/Jerusalem"
MAX_NAME: Final = 40

NOT_A_MEMBER: Final = "error: only people set up with Remi can do that"

GROUP_TOOLS: Final[tuple[Mapping[str, Any], ...]] = (
    {
        "name": "who_is_free",
        "description": (
            "When each person in this group who okayed sharing here is busy within [start, "
            "end): their name and busy times, never what the events are. not_shared names the "
            "members who haven't okayed, stopped those who said to stop sharing here. start and "
            "end are local times, ISO 8601 with no offset, e.g. 2026-10-23T00:00; for one day, "
            "that day's 00:00 to the next day's. "
            "An all-day block's start and end are dates, end being its last day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Local start, e.g. 2026-10-23T00:00"},
                "end": {"type": "string", "description": "Local end, e.g. 2026-10-24T00:00"},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "share_my_calendar",
        "description": (
            "The person who wrote okays (on true) or stops (on false) showing when they are "
            "busy, in this group only. Only when they say so themselves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true to share, false to stop"}
            },
            "required": ["on"],
        },
    },
    {
        "name": "set_up_outing",
        "description": (
            "Post a poll in this group for a plan the group agreed on, when someone asks you to "
            "set it up. Everyone who votes in gets it in their own calendar. The poll shows the "
            "title, day, time and place, so give a short title in the group's words. start and "
            "end are local times, ISO 8601 with no offset, e.g. 2026-10-23T19:00; with no end "
            "given, make it two hours. For an all-day plan, set all_day and give dates, end "
            "being its last day. One per message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What it is, e.g. 'Family dinner'"},
                "start": {"type": "string", "description": "Local start, e.g. 2026-10-23T19:00"},
                "end": {"type": "string", "description": "Local end, e.g. 2026-10-23T21:00"},
                "location": {"type": "string", "description": "Where, only if they said"},
                "all_day": {"type": "boolean", "description": "True for an all-day plan"},
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "call_me",
        "description": "What the person who wrote wants to be called, e.g. 'Dani'.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The name, as they said"}},
            "required": ["name"],
        },
    },
)


def busy_blocks(events: Sequence[CalendarEvent], zone: ZoneInfo) -> list[dict[str, object]]:
    """``events`` as start, end and all-day only, merged where they overlap or touch."""
    merged: list[tuple[datetime, datetime, bool]] = []
    for event in sorted(events, key=lambda event: event.start):
        if merged and event.start <= merged[-1][1]:
            start, end, all_day = merged[-1]
            merged[-1] = (start, max(end, event.end), all_day and event.all_day)
        else:
            merged.append((event.start, event.end, event.all_day))
    blocks: list[dict[str, object]] = []
    for start, end, all_day in merged:
        # Built from the times alone: there is no title or location to show.
        block = shown_event(CalendarEvent("", "", start, end, all_day), zone)
        del block["title"]
        blocks.append(block)
    return blocks


def _name(tenant: Tenant | None) -> str:
    return (tenant.display_name if tenant else None) or SOMEONE


async def free_busy(
    db: Database,
    group_id: UUID,
    start: datetime,
    end: datetime,
    *,
    calendar: CalendarProvider | None,
    now: datetime,
) -> dict[str, object]:
    """Who is busy when in ``[start, end)``, among the group's members who okayed it; the
    others by name only: in ``not_shared``, and asked to okay it, or in ``stopped`` if they said
    to stop. Times are in ``start``'s zone."""
    zone = start.tzinfo
    assert isinstance(zone, ZoneInfo), "start carries its zone"
    async with db.system_session() as session:
        members = await group_tenants(session, group_id)
    people: list[dict[str, object]] = []
    not_shared: list[str] = []
    stopped: list[str] = []
    for tenant_id, sharing in members:
        async with db.tenant_session(TenantId(tenant_id)) as session:
            tenant = await get_tenant(session, tenant_id)
            name = _name(tenant)
            account = await active_connection(session, tenant_id) if sharing else None
            asked = not sharing and await ask(session, tenant_id, group_id, now)
        if not sharing:
            (not_shared if asked else stopped).append(name)
            continue
        theirs = TenantCalendar(
            db,
            tenant_id,
            provider=calendar,
            account=account,
            live=tenant.live_calendar if tenant else False,
        )
        people.append({"name": name, "busy": busy_blocks(await theirs.events(start, end), zone)})
    return {"people": people, "not_shared": not_shared, "stopped": stopped}


def _detail(name: str, event: CalendarEvent, zone: ZoneInfo, language: str) -> str:
    start, end = event.start.astimezone(zone), event.end.astimezone(zone)
    day = day_label(start.date(), language)
    if event.all_day:
        return f"{name}: {event.title}, {day}"
    return f"{name}: {event.title}, {day} {start:%H:%M}–{end:%H:%M}"  # noqa: RUF001


async def _send_details(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    group_id: UUID,
    asker: UUID,
    windows: Sequence[tuple[datetime, datetime]],
    *,
    calendar: CalendarProvider | None,
    zone: ZoneInfo,
    language: str,
) -> None:
    """The granted people's events in ``windows``, privately to ``asker``, if there are any."""
    async with db.system_session() as session:
        grantors = await detail_grantors(session, asker, group_id)
    lines: list[str] = []
    for grantor in grantors:
        async with db.tenant_session(TenantId(grantor)) as session:
            tenant = await get_tenant(session, grantor)
            account = await active_connection(session, grantor)
        name = _name(tenant)
        theirs = TenantCalendar(
            db,
            grantor,
            provider=calendar,
            account=account,
            live=tenant.live_calendar if tenant else False,
        )
        found: dict[tuple[str, datetime], CalendarEvent] = {}
        for start, end in windows:
            events = await theirs.events(start, end)
            found.update(((event.external_id, event.start), event) for event in events)
        ordered = sorted(found.values(), key=lambda event: event.start)
        lines += [_detail(name, event, zone, language) for event in ordered]
    if not lines:
        return
    phone = await phone_of(db, asker)
    if phone is None:
        return
    await send_once(
        db,
        channel,
        inbox_id=None,
        idempotency_key=f"details:{row.id}",
        kind=DETAILS_KIND,
        recipient_key=f"tel:{phone}",
        to=phone,
        text=fit_message("\n".join([t("group_details", language), *lines])),
    )


async def _zone(db: Database, group_id: UUID, asker: Tenant | None) -> ZoneInfo:
    """The asker's zone, else the group's first sharer's, else :data:`DEFAULT_ZONE`."""
    if asker is not None and asker.timezone:
        return ZoneInfo(asker.timezone)
    async with db.system_session() as session:
        members = await group_tenants(session, group_id)
    sharers = [tenant_id for tenant_id, sharing in members if sharing]
    if sharers:
        async with db.tenant_session(TenantId(sharers[0])) as session:
            first = await get_tenant(session, sharers[0])
        if first is not None and first.timezone:
            return ZoneInfo(first.timezone)
    return ZoneInfo(DEFAULT_ZONE)


async def _history(db: Database, row: InboxRow, turn: GroupTurn) -> tuple[Message, ...]:
    """The group's recent conversation before ``row``, as the model reads it."""
    async with db.system_session() as session:
        context = await recent_context(session, turn.group_id)
    # The gate already added this message; it is the turn's text, not its history.
    return tuple(
        Message("assistant", line.body)
        if line.inbox_id is None
        else Message("user", f"{line.sender_name or SOMEONE}: {line.body}")
        for line in context
        if line.inbox_id != row.id
    )


async def group_turn(
    row: InboxRow,
    channel: OutboundChannel,
    turn: GroupTurn,
    *,
    db: Database,
    llm: LLMProvider,
    calendar: CalendarProvider | None,
    last_attempt: bool = False,
) -> None:
    """Count a vote; read an okay or an answer said to the group; or answer ``row`` in the
    group if it was for Remi or okayed sharing, sending any granted details privately."""
    if row.message_type == POLL_VOTE:
        await handle_vote(row, channel, turn, db=db, calendar=calendar)
        return
    okayed = False
    if not turn.addressed:
        try:
            okayed = await _okayed(row, turn, db=db, llm=llm)
            if not okayed:
                await read_answer(row, turn, db=db, llm=llm)
        except LLMUnavailableError:
            if not last_attempt:
                raise
            log.warning("group.answer_unread", reason="unavailable")
            return
        except LLMRejectedError:
            log.warning("group.answer_unread", reason="rejected")
            return
        if not okayed:
            return
    await _answer(
        row,
        channel,
        turn,
        db=db,
        llm=llm,
        calendar=calendar,
        last_attempt=last_attempt,
        approved=not turn.addressed,
    )
    if okayed and turn.tenant_id is not None:
        # Answered (or already was): the ask is done. Until now a retry reads the okay again.
        # Sharing stays as the turn left it: the model may have turned it off again.
        async with db.tenant_session(TenantId(turn.tenant_id)) as session:
            await clear_ask(session, turn.tenant_id, turn.group_id)


async def _okayed(row: InboxRow, turn: GroupTurn, *, db: Database, llm: LLMProvider) -> bool:
    """Did ``row``, not for Remi, okay sharing after Remi asked its sender? Saved if so, for
    the sender only, with the ask left open until the answer is out. Costs no model call
    unless they have an open ask and Remi's asking line, naming them, is still in the context."""
    if turn.tenant_id is None or not row.body:
        return False
    tenant_id = TenantId(turn.tenant_id)
    async with db.tenant_session(tenant_id) as session:
        asked = await open_ask(session, tenant_id, turn.group_id, turn.now)
        tenant = await get_tenant(session, tenant_id)
    if not asked:
        return False
    if await already_sent(db, row.id, GROUP_REPLY):
        # A retry after the answer went out: only closing the ask was left.
        async with db.tenant_session(tenant_id) as session:
            await clear_ask(session, tenant_id, turn.group_id)
        return False
    context = await _history(db, row, turn)
    # Remi asked by the name free_busy gave; without that line in view an "ok" can't be read.
    asked_as = re.compile(rf"\b{re.escape(_name(tenant).casefold())}\b")
    if not any(m.role == "assistant" and asked_as.search(m.content.casefold()) for m in context):
        return False
    name = (tenant.display_name if tenant else None) or row.sender_name or SOMEONE
    if not await approves(f"{name}: {row.body}", context=context, llm=llm):
        return False
    async with db.tenant_session(tenant_id) as session:
        await set_sharing(session, tenant_id, turn.group_id, True, close_ask=False)
    return True


async def _answer(
    row: InboxRow,
    channel: OutboundChannel,
    turn: GroupTurn,
    *,
    db: Database,
    llm: LLMProvider,
    calendar: CalendarProvider | None,
    last_attempt: bool,
    approved: bool,
) -> None:
    """Answer ``row`` in the group, sending any granted details privately. ``approved``: its
    sender just okayed sharing, which is already saved."""
    if await already_sent(db, row.id, GROUP_REPLY):
        log.info("group.already_sent", inbox_id=str(row.id))
        return
    asker: Tenant | None = None
    if turn.tenant_id is not None:
        async with db.tenant_session(TenantId(turn.tenant_id)) as session:
            asker = await get_tenant(session, turn.tenant_id)
    asker_name = (asker.display_name if asker else None) or row.sender_name or SOMEONE
    language = detect_language(row.body)
    zone = await _zone(db, turn.group_id, asker)
    history = await _history(db, row, turn)
    windows: list[tuple[datetime, datetime]] = []
    shared: list[bool] = [True] if approved else []
    posted: list[bool] = []

    async def run_tool(name: str, arguments: Mapping[str, Any]) -> str:
        if name == "who_is_free":
            window = read_window(arguments, zone)
            if isinstance(window, str):
                return window
            start, end, note = window
            windows.append((start, end))
            result = await free_busy(db, turn.group_id, start, end, calendar=calendar, now=turn.now)
            if note:
                result["note"] = note
            return json.dumps(result, ensure_ascii=False)
        if name not in {"share_my_calendar", "call_me", "set_up_outing"}:
            return "error: unknown tool"
        if turn.tenant_id is None:
            return NOT_A_MEMBER
        if name == "set_up_outing":
            if posted:
                return "error: one outing per message; this message's poll is already posted"
            outcome = await set_up_outing(
                arguments, db=db, channel=channel, row=row, turn=turn, zone=zone, language=language
            )
            if outcome.startswith("ok"):
                posted.append(True)
            return outcome
        tenant = TenantId(turn.tenant_id)
        if name == "share_my_calendar":
            on = arguments.get("on")
            if not isinstance(on, bool):
                return "error: on is true or false"
            async with db.tenant_session(tenant) as session:
                await set_sharing(session, turn.tenant_id, turn.group_id, on)
            shared.append(on)
            return "ok: done; a short confirmation goes under your reply, so don't write one"
        called = arguments.get("name")
        if not isinstance(called, str) or not called.strip() or len(called) > MAX_NAME:
            return f"error: name is required, at most {MAX_NAME} characters"
        async with db.tenant_session(tenant) as session:
            await set_display_name(session, turn.tenant_id, called.strip())
        return f"ok: they are {called.strip()} from now on"

    text = f"{asker_name}: {row.body or f'[{row.message_type}]'}"
    system = group_prompt(
        language, turn.now.astimezone(zone), asker_name, asker is not None, approved=approved
    )
    with structlog.contextvars.bound_contextvars(inbox_id=str(row.id)):
        try:
            reply = (
                await converse(
                    history, text, system=system, tools=GROUP_TOOLS, run_tool=run_tool, llm=llm
                )
            ).strip()
            if "http" in reply.lower():
                log.info("group.reply_dropped")
                reply = t("agent_unavailable", language)
        except LLMUnavailableError, CalendarProviderUnavailableError:
            if not last_attempt:
                raise
            log.warning("group.fallback", reason="unavailable")
            reply = t("agent_unavailable", language)
        except LLMRejectedError:
            log.warning("group.fallback", reason="rejected")
            reply = t("agent_unavailable", language)
    # Sharing is saved by now, whatever the model did after: they hear that it was.
    if shared:
        confirmed = t("group_shared_on" if shared[-1] else "group_shared_off", language)
        reply = f"{reply}\n\n{confirmed}".strip()
    # Details first: a retry runs the turn again until the group reply is out, and the
    # details' own key keeps them to one send.
    if windows and turn.tenant_id is not None:
        await _send_details(
            db,
            channel,
            row,
            turn.group_id,
            turn.tenant_id,
            windows,
            calendar=calendar,
            zone=zone,
            language=language,
        )
    if reply:
        sent = fit_message(reply)
        await send_once(
            db,
            channel,
            inbox_id=row.id,
            kind=GROUP_REPLY,
            recipient_key=group_key(turn.jid),
            to=turn.jid,
            text=sent,
        )
        async with db.system_session() as session:
            await add_context(session, turn.group_id, sender_name=REMI, body=sent, sent_at=turn.now)


__all__ = [
    "DETAILS_KIND",
    "GROUP_TOOLS",
    "busy_blocks",
    "free_busy",
    "group_turn",
]
