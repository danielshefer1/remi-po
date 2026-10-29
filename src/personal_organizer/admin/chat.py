"""``po-chat --tenant <uuid>`` -- talk to the onboarding conversation or the agent from a terminal.

The same decision code the worker runs, with the real model, starting from the tenant's stored
state: :func:`~personal_organizer.messaging.onboarding.next_reply` for an ``onboarding`` tenant,
:func:`~personal_organizer.messaging.agent.respond` for an ``active`` one. The history lives in
memory and replies are printed, not sent. Nothing about the tenant is written: a step the
conversation would take is applied in memory only. The one write is a connect link, issued or
reused exactly as the worker would, so the printed link works; and, with live reading on, the
stored copy of their Google events, refreshed as the worker would.

An active tenant's calendar is the worker's: Remi's stored events, with Google read through
Composio when it is on and they are connected. ``--fake-calendar`` reads a few made-up events
held in memory instead, so the agent can be tried without a database calendar or a Composio
key. A proposed event or change waits in memory for the next line, and a "yes" (as the model
reads it, like the worker) applies it to that fake calendar only: po-chat never writes to a real
one.

For the model check of docs/plan-iteration-04.md: on Railway, ``railway ssh --service worker
--environment staging``, then ``po-chat --tenant <uuid>``. It needs ``AGENT__API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Final
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from personal_organizer.core.errors import ConfigError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import active_connection
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.agent import (
    AGENT_REPLY,
    ProposedChange,
    add_events,
    apply_change,
    events_summary,
    respond,
)
from personal_organizer.messaging.calendar import Calendar, InMemoryTenantCalendar, TenantCalendar
from personal_organizer.messaging.confirm import CONFIRM_ADD, confirm_prompt, decide
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.notes import InMemoryNotes
from personal_organizer.messaging.onboarding import next_reply
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.schedule import InMemorySchedule
from personal_organizer.messaging.tenancy import TenantState, load_state, phone_of
from personal_organizer.messaging.text import bubbles
from personal_organizer.observability.logging import configure_logging
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.providers.calendar.composio import ComposioConnector
from personal_organizer.providers.llm.anthropic import AnthropicLLM
from personal_organizer.settings import Settings, get_settings

OK: Final = 0
REFUSED: Final = 1

#: Printed for an agent turn with nothing to send, where the worker sends its fixed line.
NO_ANSWER: Final = "(no usable answer: the worker sends its fallback)"

#: Printed for a confirmed event when the calendar is a real one.
NOT_ADDED: Final = "(not added: po-chat adds events to --fake-calendar only)"

#: Printed for a confirmed change when the calendar is a real one.
NOT_CHANGED: Final = "(not changed: po-chat changes --fake-calendar only)"

#: The channel name the in-memory rows carry. No channel by that name exists to send on.
CHAT_CHANNEL: Final = "po-chat"


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def _row(phone: str, body: str) -> InboxRow:
    return InboxRow(
        id=uuid4(),
        channel=CHAT_CHANNEL,
        provider_message_id=CHAT_CHANNEL,
        sender_key=f"tel:{phone}",
        sender_user_id=None,
        sender_phone=phone,
        message_type="text",
        body=body,
        reply_id=None,
        sent_at=datetime.now(UTC),
        processed_at=None,
        disposition=None,
    )


async def chat(
    db: Database,
    settings: Settings,
    tenant: TenantState,
    phone: str,
    *,
    llm: LLMProvider,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = _out,
) -> int:
    """Turns until end of input. ``read`` blocks, so it runs on a thread."""
    history: list[Message] = []
    while True:
        try:
            line = await asyncio.to_thread(read, "you> ")
        except EOFError:
            return OK
        if not line.strip():
            continue
        reply = await next_reply(
            db,
            _row(phone, line),
            tenant,
            to=phone,
            settings=settings,
            now=datetime.now(UTC),
            llm=llm,
            history=history,
        )
        write(f"bot [{reply.kind}]> {reply.text}")
        change = reply.advance
        if change.timezone is not None:
            write(f"(time zone {change.timezone}, in memory only)")
        if change.activate:
            write("(setup done, in memory only: from here the agent answers)")
            return OK
        history += [Message("user", line), Message("assistant", change.reply or reply.text)]
        tenant = replace(
            tenant,
            language=change.language or tenant.language,
            first_message=False,
        )


async def agent_chat(
    tenant: TenantState,
    *,
    llm: LLMProvider,
    calendar: Calendar,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = _out,
    connect_link: Callable[[], Awaitable[str]] | None = None,
) -> int:
    """An active tenant's turns until end of input. Touches no database but ``connect_link``'s."""
    history: list[Message] = []
    zone, language = ZoneInfo(tenant.timezone or "UTC"), tenant.language
    waiting: list[CalendarEvent] = []
    changing: list[ProposedChange] = []
    # Reminders and the morning time live in memory for the session, like the history.
    schedule = InMemorySchedule(datetime.now(UTC))
    notes = InMemoryNotes()
    while True:
        try:
            line = await asyncio.to_thread(read, "you> ")
        except EOFError:
            return OK
        if not line.strip():
            continue
        picked = None
        if waiting:
            picked = await decide(events_summary(waiting, zone, language), line, llm=llm)
        elif changing:
            asked = confirm_prompt(changing[0].summary, language, question=changing[0].question)
            picked = await decide(asked, line, llm=llm)
        if picked is not None:
            events, changes = waiting[:], changing[:]
            waiting.clear()
            changing.clear()
            fake = isinstance(calendar, InMemoryTenantCalendar)
            if changes and picked != CONFIRM_ADD:
                text = t("change_cancelled", language)
            elif changes:
                text = (
                    await apply_change(changes[0], calendar=calendar, zone=zone, language=language)
                    if fake
                    else NOT_CHANGED
                )
            elif picked != CONFIRM_ADD:
                text = t("event_cancelled", language)
            elif fake:
                text = await add_events(events, calendar=calendar, zone=zone, language=language)
            else:
                text = NOT_ADDED
        else:
            waiting.clear()
            changing.clear()
            text = await respond(
                history,
                line,
                tenant,
                llm=llm,
                calendar=calendar,
                now=datetime.now(UTC),
                proposed=waiting,
                changed=changing,
                schedule=schedule,
                notes=notes,
                connect_link=connect_link,
                strong=None,
            )
        text = "\n\n".join(bubbles(text))  # as production records it
        write(f"bot [{AGENT_REPLY}]> {text or NO_ANSWER}")
        history += [Message("user", line), Message("assistant", text)]


def sample_events(now: datetime, zone: ZoneInfo) -> list[CalendarEvent]:
    """What ``--fake-calendar`` holds: a few events from today on, in the tenant's zone."""
    today = datetime.combine(now.astimezone(zone).date(), time(), zone)

    def at(days: int, hour: int) -> datetime:
        return today + timedelta(days=days, hours=hour)

    return [
        CalendarEvent("fake-1", "Team meeting", at(0, 15), at(0, 16)),
        CalendarEvent("fake-2", "Dentist", at(1, 9), at(1, 9) + timedelta(minutes=30)),
        CalendarEvent("fake-3", "Dinner with Noa", at(3, 20), at(3, 22), location="Tel Aviv"),
        CalendarEvent("fake-4", "Grandma's birthday", at(5, 0), at(6, 0), all_day=True),
    ]


async def _agent(
    db: Database, settings: Settings, tenant: TenantState, *, llm: LLMProvider, fake: bool
) -> int:
    async with db.tenant_session(TenantId(tenant.id)) as session:
        account = await active_connection(session, tenant.id)
    calendar: Calendar
    if fake:
        zone = ZoneInfo(tenant.timezone or "UTC")
        calendar = InMemoryTenantCalendar(
            sample_events(datetime.now(UTC), zone),
            connected=account is not None,
            live=tenant.live_calendar,
        )
        _out("(fake calendar: a few made-up events from today on)")
    else:
        # As the worker: with Composio off, Remi's own calendar is the whole calendar.
        provider = (
            ComposioConnector.from_settings(settings.composio)
            if settings.composio.enabled
            else None
        )
        calendar = TenantCalendar(
            db, tenant.id, provider=provider, account=account, live=tenant.live_calendar
        )

    async def connect_link() -> str:
        return await issue_or_reuse_link(db, tenant.id, settings=settings, now=datetime.now(UTC))

    return await agent_chat(tenant, llm=llm, calendar=calendar, connect_link=connect_link)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="po-chat",
        description="talk to the onboarding conversation or the agent; prints, never sends",
    )
    parser.add_argument("--tenant", type=UUID, required=True, help="the tenant's uuid")
    parser.add_argument(
        "--fake-calendar",
        action="store_true",
        help="an active tenant reads made-up events held in memory, not Google",
    )
    return parser


async def _run(tenant_id: UUID, settings: Settings, *, fake_calendar: bool = False) -> int:
    try:
        llm = AnthropicLLM.from_settings(settings)
    except ConfigError:
        _out("AGENT__API_KEY is not set")
        return REFUSED
    db = Database(settings)
    try:
        tenant = await load_state(db, tenant_id)
        if tenant is not None and tenant.status == "active":
            return await _agent(db, settings, tenant, llm=llm, fake=fake_calendar)
        if tenant is None or tenant.status != "onboarding":
            _out("no onboarding or active tenant with that id")
            return REFUSED
        phone = await phone_of(db, tenant_id)
        if phone is None:
            _out("the tenant has no number on file, so there is no zone guess to start from")
            return REFUSED
        return await chat(db, settings, tenant, phone, llm=llm)
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    return asyncio.run(_run(args.tenant, settings, fake_calendar=args.fake_calendar))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
