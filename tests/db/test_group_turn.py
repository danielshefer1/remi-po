"""The group turn (Iteration 07), through the gate, with a scripted model and fake calendars.

Done-When: the model and the group see busy times and names, never what an event is; a
member who hasn't okayed the group is never read; okaying takes a member's own words; and a
person granted details gets them privately, by the grants as they stand at that moment.
"""

from __future__ import annotations

import functools
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from personal_organizer.core.errors import (
    CalendarProviderUnavailableError,
    LLMRejectedError,
    LLMUnavailableError,
    TransientChannelError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Event
from personal_organizer.db.repositories import events as event_rows
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.connections import (
    active_connection,
    bind_connection,
    revoke_connection,
)
from personal_organizer.db.repositories.tenants import get_tenant, set_live_calendar, set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.channel import GroupRoster
from personal_organizer.interfaces.llm import ToolCall
from personal_organizer.messaging.groups import turn as turn_module
from personal_organizer.messaging.groups.gate import GROUP_REPLY, REMI
from personal_organizer.messaging.groups.grants import TenantGrants
from personal_organizer.messaging.groups.turn import group_turn
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.connect import suspend
from tests.fixtures.llm import NoLLM, ScriptedLLM, call, said
from tests.fixtures.tenants import new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)  # Wednesday, 13:00 in Jerusalem
ZONE = ZoneInfo("Asia/Jerusalem")
JID = "120363402106000001@g.us"
DANA, NOA, EDEN = "+972501111111", "+972502222222", "+972503333333"
STRANGER = "+972500000009"
EVERYONE = GroupRoster("Family", frozenset({DANA, NOA, EDEN}), True)
FRIDAY = call("who_is_free", start="2026-10-23T00:00", end="2026-10-24T00:00")


def _at(hour: int) -> datetime:
    return datetime(2026, 10, 23, hour, tzinfo=ZONE)


class Calendars(InMemoryCalendar):
    """Each account its own events."""

    def __init__(self, events: dict[str, list[CalendarEvent]]) -> None:
        super().__init__()
        self.by_account = events

    async def list_events(
        self, account_id: str, user_id: str, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]:
        self.calls.append((account_id, user_id, start, end))
        events = self.by_account.get(account_id, [])
        return [event for event in events if event.start < end and event.end > start]


CALENDARS = {
    "ca_dana": [CalendarEvent("d1", "Dentist", _at(9), _at(10), location="Herzl 5")],
    "ca_noa": [CalendarEvent("n1", "Board meeting", _at(14), _at(16))],
    "ca_eden": [CalendarEvent("e1", "Therapy", _at(11), _at(12))],
}


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def group(db: Database) -> UUID:
    async with db.system_session() as session:
        found = await groups.ensure_group(session, channel="gowa", jid=JID)
        await groups.mark_served(session, found.id, NOW)
    return found.id


async def _member(
    db: Database,
    group_id: UUID,
    phone: str,
    name: str,
    account: str | None,
    *,
    sharing: bool,
    live: bool = True,
    details: bool = False,
) -> UUID:
    """``live``: Remi reads their Google every time, which these tests' fake provider serves."""
    tenant_id = await new_tenant(db, phone=phone, status="active", step=None)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
        if account is not None:
            await bind_connection(
                session, tenant_id, connected_account_id=account, auth_config_id="ac_test"
            )
        await set_live_calendar(session, tenant_id, live)
        await groups.set_display_name(session, tenant_id, name)
        await groups.touch_member(session, tenant_id, group_id, NOW)
        if sharing:  # a member starts out not sharing; set_sharing(False) is an explicit stop
            await groups.set_sharing(session, tenant_id, group_id, True)
        if details:
            await groups.set_details(session, tenant_id, group_id, True)
    return tenant_id


async def _say(
    db: Database,
    conn: Any,
    settings: Settings,
    llm: Any,
    *,
    phone: str = DANA,
    body: str = "Remi, who's free on Friday?",
    calendar: InMemoryCalendar | None = None,
    channel: FakeOutbound | None = None,
    inbox_id: UUID | None = None,
    last_attempt: bool = False,
    jid: str = JID,
) -> tuple[FakeOutbound, InMemoryCalendar]:
    """Run one message through the gate; pass ``channel`` and ``inbox_id`` to retry it."""
    calendar = calendar or Calendars(CALENDARS)
    channel = channel or FakeOutbound(name="gowa")
    if inbox_id is None:
        inbox_id = await insert_inbox(
            conn, phone=phone, body=body, group_id=jid, sender_name="WhatsApp name", sent_at=NOW
        )
    await handle_inbound(
        inbox_id,
        db=db,
        channels={channel.name: channel}.__getitem__,
        allowlist=frozenset(),
        on_allowed=Spy(),
        settings=settings,
        on_group=functools.partial(
            group_turn, db=db, llm=llm, calendar=calendar, last_attempt=last_attempt
        ),
        now=lambda: NOW,
    )
    return channel, calendar


def _seen(llm: ScriptedLLM) -> str:
    """Everything the model was shown, every call."""
    return "\n".join(message.content for messages in llm.calls for message in messages)


def _tool_result(llm: ScriptedLLM) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(llm.calls[1][-1].content)
    return result


async def test_group_turn_never_sees_titles(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True)
    llm = ScriptedLLM(said("", FRIDAY), said("Friday: Dana's busy 9 to 10, Noa 2 to 4."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm)

    seen = _seen(llm)
    for secret in ("Dentist", "Board meeting", "Herzl"):
        assert secret not in seen
    busy = {person["name"]: person["busy"] for person in _tool_result(llm)["people"]}
    assert busy == {
        "Dana": [{"start": "2026-10-23T09:00", "end": "2026-10-23T10:00", "all_day": False}],
        "Noa": [{"start": "2026-10-23T14:00", "end": "2026-10-23T16:00", "all_day": False}],
    }
    [sent] = channel.sent
    assert sent.recipient == JID
    assert "Dentist" not in sent.body
    assert "Board meeting" not in sent.body
    context = await owner_conn.fetch("SELECT sender_name, body FROM group_context ORDER BY sent_at")
    assert [tuple(row) for row in context][-1] == ("Remi", sent.body)


async def test_group_reply_split(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    llm = ScriptedLLM(said("Friday looks open.\n---\nWant me to ask Noa too?"))
    channel, _ = await _say(db, owner_conn, onboarding_settings, llm)
    assert [m.body for m in channel.sent] == ["Friday looks open.", "Want me to ask Noa too?"]
    assert {m.recipient for m in channel.sent} == {JID}
    context = await owner_conn.fetch("SELECT sender_name, body FROM group_context ORDER BY sent_at")
    assert [tuple(row) for row in context][-1] == (
        "Remi",
        "Friday looks open.\n\nWant me to ask Noa too?",
    )


async def test_group_reply_of_only_break_lines_sends_nothing(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    channel, _ = await _say(db, owner_conn, onboarding_settings, ScriptedLLM(said("---")))
    assert channel.sent == []


async def test_not_shared_member_is_not_read(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _member(db, group, EDEN, "Eden", "ca_eden", sharing=False)
    llm = ScriptedLLM(said("", FRIDAY), said("Dana's busy 9 to 10."))

    _, calendar = await _say(db, owner_conn, onboarding_settings, llm)

    assert [account for account, *_ in calendar.calls] == ["ca_dana"]
    result = _tool_result(llm)
    assert [person["name"] for person in result["people"]] == ["Dana"]
    assert result["not_shared"] == ["Eden"]
    assert "Therapy" not in _seen(llm)


async def test_free_busy_asks_not_shared(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    eden = await _member(db, group, EDEN, "Eden", "ca_eden", sharing=False)
    llm = ScriptedLLM(said("", FRIDAY), said("Dana's busy 9 to 10."))

    await _say(db, owner_conn, onboarding_settings, llm)

    for tenant_id, asked in ((dana, False), (eden, True)):
        async with db.tenant_session(TenantId(tenant_id)) as session:
            assert await groups.open_ask(session, tenant_id, group, NOW) is asked


async def test_share_my_calendar_sets_sharing(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=False)
    llm = ScriptedLLM(said("", call("share_my_calendar", on=True)), said(""))

    channel, _ = await _say(
        db, owner_conn, onboarding_settings, llm, body="Remi, you can share my calendar here"
    )

    async with db.system_session() as session:
        assert await groups.group_tenants(session, group) == [(dana, True)]
    assert [message.body for message in channel.sent] == [t("group_shared_on", "en")]


@pytest.mark.parametrize(
    ("after", "last_attempt"),
    [
        (LLMRejectedError(400), False),
        (LLMUnavailableError("down"), True),
        (said("see http://x"), False),
    ],
    ids=["rejected", "unavailable", "link"],
)
async def test_sharing_is_confirmed_even_when_the_reply_falls_back(
    db: Database,
    owner_conn: Any,
    onboarding_settings: Settings,
    group: UUID,
    after: Any,
    last_attempt: bool,
) -> None:
    """Sharing was saved before the model failed: they still hear that it was."""
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=False)
    llm = ScriptedLLM(said("", call("share_my_calendar", on=True)), after)

    channel, _ = await _say(
        db,
        owner_conn,
        onboarding_settings,
        llm,
        body="Remi, you can share my calendar here",
        last_attempt=last_attempt,
    )

    assert [message.body for message in channel.sent] == [
        f"{t('agent_unavailable', 'en')}\n\n{t('group_shared_on', 'en')}"
    ]


async def test_stranger_cannot_share(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=False)
    llm = ScriptedLLM(
        said("", call("share_my_calendar", on=True)), said("Only people set up with me can.")
    )

    channel, _ = await _say(
        db,
        owner_conn,
        onboarding_settings,
        llm,
        phone=STRANGER,
        body="Remi, share my calendar here",
    )

    assert llm.calls[1][-1].content == "error: only people set up with Remi can do that"
    async with db.system_session() as session:
        assert await groups.group_tenants(session, group) == [(dana, False)]
    assert [message.body for message in channel.sent] == ["Only people set up with me can."]


async def _granted(db: Database, group: UUID) -> tuple[UUID, UUID]:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    noa = await _member(db, group, NOA, "Noa", "ca_noa", sharing=True)
    async with db.tenant_session(TenantId(noa)) as session:
        await groups.grant(session, noa, dana)
    return dana, noa


async def test_details_sent_privately_to_grantee(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _granted(db, group)
    llm = ScriptedLLM(said("", FRIDAY), said("Noa's busy 2 to 4 on Friday."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm)

    private, in_group = channel.sent
    assert in_group.recipient == JID
    assert "Board meeting" not in in_group.body
    assert private.recipient == DANA
    assert private.body.startswith(t("group_details", "en"))
    assert "Noa: Board meeting, Friday 2026-10-23 14:00–16:00" in private.body  # noqa: RUF001
    assert "Dentist" not in private.body  # her own events are not a detail she was granted
    assert "Board meeting" not in _seen(llm)


async def test_details_survive_a_failed_send(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """The details send fails for now: the retry sends them, then the group reply."""
    await _granted(db, group)
    channel = FakeOutbound(TransientChannelError("gateway down"), name="gowa")
    answer = said("Noa's busy 2 to 4 on Friday.")
    llm = ScriptedLLM(said("", FRIDAY), answer, said("", FRIDAY), answer)

    with pytest.raises(TransientChannelError):
        await _say(db, owner_conn, onboarding_settings, llm, channel=channel)
    assert channel.sent == []
    [inbox_id] = await owner_conn.fetchval("SELECT array_agg(id) FROM channel_inbox")
    await _say(db, owner_conn, onboarding_settings, llm, channel=channel, inbox_id=inbox_id)

    assert [message.recipient for message in channel.sent] == [DANA, JID]
    assert "Board meeting" in channel.sent[0].body


class NoaDown(Calendars):
    """Noa's Google is down whenever it is read."""

    async def list_events(
        self, account_id: str, user_id: str, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]:
        if account_id == "ca_noa":
            raise CalendarProviderUnavailableError("composio down")
        return await super().list_events(account_id, user_id, start=start, end=end)


async def test_grantor_google_outage_serves_the_stored_copy(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """No retry: Remi's copy of Noa's calendar answers, and nothing is raised."""
    _, noa = await _granted(db, group)
    async with db.tenant_session(TenantId(noa)) as session:
        await event_rows.add_event(
            session, noa, CalendarEvent("", "Stored meeting", _at(14), _at(16)), source="remi"
        )
    channel = FakeOutbound(name="gowa")

    await _say(
        db,
        owner_conn,
        onboarding_settings,
        ScriptedLLM(said("", FRIDAY), said("Noa's busy 2 to 4 on Friday.")),
        calendar=NoaDown(CALENDARS),
        channel=channel,
    )

    private, _ = channel.sent
    assert "Noa: Stored meeting" in private.body


async def test_free_busy_reads_native_events(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """No Google and no Composio at all: Remi's own events are what they are busy with."""
    dana = await _member(db, group, DANA, "Dana", None, sharing=True)
    async with db.tenant_session(TenantId(dana)) as session:
        await event_rows.add_event(
            session, dana, CalendarEvent("", "Dentist", _at(9), _at(10)), source="remi"
        )
    llm = ScriptedLLM(said("", FRIDAY), said("Dana's busy 9 to 10."))
    calendar = InMemoryCalendar()

    await _say(db, owner_conn, onboarding_settings, llm, calendar=calendar)

    [person] = _tool_result(llm)["people"]
    assert person["busy"] == [
        {"start": "2026-10-23T09:00", "end": "2026-10-23T10:00", "all_day": False}
    ]
    assert "note" not in person
    assert "Dentist" not in _seen(llm)
    assert calendar.calls == []


async def test_free_busy_live_member_refreshes(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """One who asked for live reading is read in Google; one who did not is read from Remi."""
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, live=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, live=False)
    llm = ScriptedLLM(said("", FRIDAY), said("Dana's busy 9 to 10."))

    _, calendar = await _say(db, owner_conn, onboarding_settings, llm)

    assert [account for account, *_ in calendar.calls] == ["ca_dana"]
    busy = {person["name"]: person["busy"] for person in _tool_result(llm)["people"]}
    assert busy["Dana"] == [
        {"start": "2026-10-23T09:00", "end": "2026-10-23T10:00", "all_day": False}
    ]
    assert busy["Noa"] == []


async def test_details_follow_current_grants(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana, noa = await _granted(db, group)
    async with db.tenant_session(TenantId(noa)) as session:
        await groups.revoke(session, noa, dana)
    llm = ScriptedLLM(said("", FRIDAY), said("Noa's busy 2 to 4 on Friday."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm)

    assert [message.recipient for message in channel.sent] == [JID]


async def test_free_busy_doesnt_ask_who_stopped(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    eden = await _member(db, group, EDEN, "Eden", "ca_eden", sharing=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=False)
    async with db.tenant_session(TenantId(eden)) as session:
        await groups.set_sharing(session, eden, group, False)
    llm = ScriptedLLM(said("", FRIDAY), said("Dana's busy 9 to 10."))

    _, calendar = await _say(db, owner_conn, onboarding_settings, llm)

    result = _tool_result(llm)
    assert (result["not_shared"], result["stopped"]) == (["Noa"], ["Eden"])
    assert [account for account, *_ in calendar.calls] == ["ca_dana"]
    async with db.tenant_session(TenantId(eden)) as session:
        assert not await groups.open_ask(session, eden, group, NOW)


async def test_unaddressed_message_calls_no_model(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    channel, calendar = await _say(
        db, owner_conn, onboarding_settings, NoLLM(), body="who's free on Friday?"
    )
    assert channel.sent == []
    assert calendar.calls == []


async def _asked(
    db: Database,
    group: UUID,
    phone: str,
    name: str,
    account: str,
    *,
    at: datetime = NOW,
    asking_at: datetime | None = NOW,
) -> UUID:
    """A member who doesn't share yet and was asked to okay it at ``at``, by Remi's line in the
    group at ``asking_at`` (none if ``None``)."""
    tenant_id = await _member(db, group, phone, name, account, sharing=False)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await groups.ask(session, tenant_id, group, at)
    if asking_at is not None:
        async with db.system_session() as session:
            await groups.add_context(
                session,
                group,
                sender_name=REMI,
                body=f"{name}, I need your okay before I look at your calendar.",
                sent_at=asking_at,
            )
    return tenant_id


async def _sharing(db: Database, group: UUID) -> dict[UUID, bool]:
    async with db.system_session() as session:
        return dict(await groups.group_tenants(session, group))


async def _asked_now(db: Database, group: UUID, tenant_id: UUID) -> bool:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await groups.open_ask(session, tenant_id, group, NOW)


async def test_approval_turns_sharing_on_and_answers(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    answer = "ביום שישי עדן עסוקה מ-11 עד 12."
    llm = ScriptedLLM(said("YES"), said("", FRIDAY), said(answer))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm, phone=EDEN, body="מאשרת")

    assert (await _sharing(db, group))[eden] is True
    assert llm.calls[1][-1].content == "Eden: מאשרת"
    assert "Eden just okayed sharing here" in llm.calls[1][0].content
    people = json.loads(llm.calls[2][-1].content)["people"]
    assert sorted(person["name"] for person in people) == ["Dana", "Eden"]
    [sent] = channel.sent
    assert sent.recipient == JID
    assert sent.body == f"{answer}\n\n{t('group_shared_on', 'he')}"
    kinds = await owner_conn.fetch("SELECT kind FROM channel_outbox")
    assert [row["kind"] for row in kinds] == [GROUP_REPLY]


async def test_non_approval_is_silent(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    llm = ScriptedLLM(said("NO"))

    channel, calendar = await _say(db, owner_conn, onboarding_settings, llm, phone=EDEN, body="lol")

    assert len(llm.calls) == 1
    assert channel.sent == []
    assert calendar.calls == []
    assert (await _sharing(db, group))[eden] is False
    assert await _asked_now(db, group, eden)


async def test_not_asked_costs_no_call(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    eden = await _member(db, group, EDEN, "Eden", "ca_eden", sharing=False)

    channel, _ = await _say(db, owner_conn, onboarding_settings, NoLLM(), phone=EDEN, body="מאשרת")

    assert channel.sent == []
    assert (await _sharing(db, group))[eden] is False


async def test_approval_changes_only_sender(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """Okaying for someone else changes nothing for them, asked or not."""
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    noa = await _asked(db, group, NOA, "Noa", "ca_noa")
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")

    # Dana has no open ask: her message isn't read at all.
    await _say(db, owner_conn, onboarding_settings, NoLLM(), phone=DANA, body="Eden says it's ok")
    assert (await _sharing(db, group))[eden] is False

    # Noa has one: only Noa's own sharing can change, whatever the model reads.
    llm = ScriptedLLM(said("YES"), said("Noa's in."))
    await _say(db, owner_conn, onboarding_settings, llm, phone=NOA, body="Eden says it's ok")
    sharing = await _sharing(db, group)
    assert (sharing[noa], sharing[eden]) == (True, False)
    assert await _asked_now(db, group, eden)


async def test_approval_llm_down_last_attempt_is_silent(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    channel = FakeOutbound(name="gowa")
    down = LLMUnavailableError("down")

    with pytest.raises(LLMUnavailableError):
        await _say(
            db,
            owner_conn,
            onboarding_settings,
            ScriptedLLM(down),
            phone=EDEN,
            body="מאשרת",
            channel=channel,
        )
    [inbox_id] = await owner_conn.fetchval("SELECT array_agg(id) FROM channel_inbox")
    await _say(
        db,
        owner_conn,
        onboarding_settings,
        ScriptedLLM(down),
        channel=channel,
        inbox_id=inbox_id,
        last_attempt=True,
    )

    assert channel.sent == []
    assert (await _sharing(db, group))[eden] is False
    assert await _asked_now(db, group, eden)


async def test_approval_survives_a_retry(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """The model is down after the okay is saved: the retry still answers, once."""
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    channel = FakeOutbound(name="gowa")
    answer = "ביום שישי עדן עסוקה מ-11 עד 12."

    with pytest.raises(LLMUnavailableError):
        await _say(
            db,
            owner_conn,
            onboarding_settings,
            ScriptedLLM(said("YES"), LLMUnavailableError("down")),
            phone=EDEN,
            body="מאשרת",
            channel=channel,
        )
    [inbox_id] = await owner_conn.fetchval("SELECT array_agg(id) FROM channel_inbox")
    llm = ScriptedLLM(said("YES"), said("", FRIDAY), said(answer))
    await _say(db, owner_conn, onboarding_settings, llm, channel=channel, inbox_id=inbox_id)

    assert [message.body for message in channel.sent] == [
        f"{answer}\n\n{t('group_shared_on', 'he')}"
    ]
    kinds = await owner_conn.fetch("SELECT kind FROM channel_outbox")
    assert [row["kind"] for row in kinds] == [GROUP_REPLY]
    assert (await _sharing(db, group))[eden] is True
    assert not await _asked_now(db, group, eden)


async def test_approval_is_read_in_its_chat(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """An "ok" answering a relative goes to the classifier with that relative's line."""
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    await _say(
        db, owner_conn, onboarding_settings, NoLLM(), phone=DANA, body="Eden, can you bring cake?"
    )
    llm = ScriptedLLM(said("NO"))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm, phone=EDEN, body="ok")

    [messages] = llm.calls
    # Context lines carry the WhatsApp name, as in the group prompt.
    assert ("user", "WhatsApp name: Eden, can you bring cake?") in [
        (message.role, message.content) for message in messages
    ]
    assert (messages[-1].role, messages[-1].content) == ("user", "Eden: ok")
    assert channel.sent == []
    assert (await _sharing(db, group))[eden] is False


async def test_approved_turn_can_still_turn_sharing_off(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """The okay is read, but the model hears "not yet" and stops: closing the ask leaves it off."""
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    llm = ScriptedLLM(said("YES"), said("", call("share_my_calendar", on=False)), said(""))

    channel, _ = await _say(
        db, owner_conn, onboarding_settings, llm, phone=EDEN, body="ok, but not yet"
    )

    assert (await _sharing(db, group))[eden] is False
    assert not await _asked_now(db, group, eden)
    assert [message.body for message in channel.sent] == [t("group_shared_off", "en")]


async def test_okay_without_the_ask_in_view_calls_no_model(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """The ask is open, but Remi's asking line has left the context: "ok" can't be judged."""
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden", asking_at=NOW - timedelta(hours=7))

    channel, _ = await _say(db, owner_conn, onboarding_settings, NoLLM(), phone=EDEN, body="ok")

    assert channel.sent == []
    assert (await _sharing(db, group))[eden] is False
    assert await _asked_now(db, group, eden)


async def test_okay_after_the_ask_expired_calls_no_model(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden", at=NOW - timedelta(hours=49))

    channel, _ = await _say(db, owner_conn, onboarding_settings, NoLLM(), phone=EDEN, body="ok")

    assert channel.sent == []
    assert (await _sharing(db, group))[eden] is False


async def test_okay_already_answered_closes_the_ask(
    db: Database,
    owner_conn: Any,
    onboarding_settings: Settings,
    group: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply went out but the job died before the ask was closed: the retry just closes it."""
    eden = await _asked(db, group, EDEN, "Eden", "ca_eden")
    channel = FakeOutbound(name="gowa")

    async def dies(*_: object) -> None:
        raise ConnectionError("worker died")

    with monkeypatch.context() as patch:
        patch.setattr(turn_module, "clear_ask", dies)
        with pytest.raises(ConnectionError):
            await _say(
                db,
                owner_conn,
                onboarding_settings,
                ScriptedLLM(said("YES"), said("Thanks, Eden!")),
                phone=EDEN,
                body="ok",
                channel=channel,
            )
    [inbox_id] = await owner_conn.fetchval("SELECT array_agg(id) FROM channel_inbox")
    assert await _asked_now(db, group, eden)
    # Remi answered Dana in between, so the retried "ok" is no follow-up to Remi.
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _say(db, owner_conn, onboarding_settings, ScriptedLLM(said("Hi!")), channel=channel)

    await _say(db, owner_conn, onboarding_settings, NoLLM(), channel=channel, inbox_id=inbox_id)

    assert len(channel.sent) == 2
    assert not await _asked_now(db, group, eden)
    assert (await _sharing(db, group))[eden] is True


async def test_okay_needs_the_whole_name_asked(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    """Remi asked Dana, not Dan: "Dan" inside "Dana" is no ask of his."""
    dan = await _asked(db, group, STRANGER, "Dan", "ca_dan", asking_at=None)
    async with db.system_session() as session:
        await groups.add_context(
            session, group, sender_name=REMI, body="Dana, I need your okay first.", sent_at=NOW
        )

    channel, _ = await _say(db, owner_conn, onboarding_settings, NoLLM(), phone=STRANGER, body="ok")

    assert channel.sent == []
    assert (await _sharing(db, group))[dan] is False
    assert await _asked_now(db, group, dan)


def _entry(llm: ScriptedLLM, name: str) -> dict[str, Any]:
    entry: dict[str, Any] = next(p for p in _tool_result(llm)["people"] if p["name"] == name)
    return entry


async def _ask(
    db: Database,
    conn: Any,
    settings: Settings,
    *,
    roster: GroupRoster | None = EVERYONE,
    calendar: InMemoryCalendar | None = None,
    jid: str = JID,
) -> tuple[ScriptedLLM, FakeOutbound]:
    llm = ScriptedLLM(said("", FRIDAY), said("Here is Friday."))
    channel = FakeOutbound(name="gowa", roster=roster)
    await _say(db, conn, settings, llm, channel=channel, calendar=calendar, jid=jid)
    return llm, channel


async def _opened_pair(db: Database, group: UUID) -> tuple[UUID, UUID]:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, details=True)
    noa = await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    return dana, noa


async def _is_on(db: Database, tenant_id: UUID, group: UUID) -> bool:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await groups.details_on(session, tenant_id, group)


async def test_member_who_didnt_opt_in_shows_no_title(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, details=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True)
    llm, _ = await _ask(db, owner_conn, onboarding_settings)
    assert "Board meeting" not in _seen(llm)
    assert "Dentist" in _seen(llm)
    assert "events" not in _entry(llm, "Noa")


async def test_opened_events_read_under_their_owners_tenant(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", None, sharing=True)
    noa = await _member(db, group, NOA, "Noa", None, sharing=True, details=True)
    async with db.tenant_session(TenantId(noa)) as session:
        await event_rows.add_event(
            session, noa, CalendarEvent("", "Noa's yoga", _at(14), _at(15)), source="remi"
        )
    llm, _ = await _ask(db, owner_conn, onboarding_settings)
    assert _entry(llm, "Noa")["events"][0]["title"] == "Noa's yoga"


async def test_opted_in_one_group_only(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    other = "120363402106000002@g.us"
    async with db.system_session() as session:
        b = await groups.ensure_group(session, channel="gowa", jid=other)
        await groups.mark_served(session, b.id, NOW)
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    noa = await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    for member in (dana, noa):
        async with db.tenant_session(TenantId(member)) as session:
            await groups.touch_member(session, member, b.id, NOW)
            await groups.set_sharing(session, member, b.id, True)
    llm, _ = await _ask(db, owner_conn, onboarding_settings, jid=other)
    assert "Board meeting" not in _seen(llm)


async def test_private_event_stays_busy(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    secret = CalendarEvent("n1", "Board meeting", _at(14), _at(16), private=True)
    llm, _ = await _ask(
        db, owner_conn, onboarding_settings, calendar=Calendars({"ca_noa": [secret]})
    )
    assert "Board meeting" not in _seen(llm)
    assert _entry(llm, "Noa")["busy"][0]["start"] == "2026-10-23T14:00"


async def test_imported_before_the_change_stays_busy(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    noa = await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    async with db.tenant_session(TenantId(noa)) as session:
        await event_rows.upsert_google(
            session, noa, CalendarEvent("n1", "Board meeting", _at(14), _at(16))
        )
        # As a row imported before #82 reads: Google's flag was never stored.
        await session.execute(update(Event).values(private=None))
    llm, _ = await _ask(db, owner_conn, onboarding_settings, calendar=NoaDown(CALENDARS))
    assert "Board meeting" not in _seen(llm)


async def test_opened_member_is_read_fresh(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True, live=False)
    calendar = Calendars(CALENDARS)
    await _ask(db, owner_conn, onboarding_settings, calendar=calendar)
    assert "ca_noa" in [account for account, *_ in calendar.calls]


async def test_a_link_in_a_title_never_reaches_the_model(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    linked = CalendarEvent(
        "n1",
        "Sync https://zoom.us/j/1 call",
        _at(14),
        _at(15),
        location="https://zoom.us/j/1",
        private=False,
    )
    llm, channel = await _ask(
        db, owner_conn, onboarding_settings, calendar=Calendars({"ca_noa": [linked]})
    )
    assert "http" not in _seen(llm)
    assert [m.body for m in channel.sent] == ["Here is Friday."]


async def test_member_who_left_goes_back_to_busy(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    _, noa = await _opened_pair(db, group)
    llm, _ = await _ask(
        db, owner_conn, onboarding_settings, roster=GroupRoster("F", frozenset({DANA}), True)
    )
    assert "Board meeting" not in _seen(llm)
    assert not await _is_on(db, noa, group)


async def test_incomplete_roster_keeps_the_choice(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    _, noa = await _opened_pair(db, group)
    llm, _ = await _ask(
        db, owner_conn, onboarding_settings, roster=GroupRoster("F", frozenset({DANA}), False)
    )
    assert "Board meeting" not in _seen(llm)
    assert await _is_on(db, noa, group)


async def test_no_roster_means_busy_only(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    _, noa = await _opened_pair(db, group)
    llm, channel = await _ask(db, owner_conn, onboarding_settings, roster=None)
    assert "Board meeting" not in _seen(llm)
    assert [m.body for m in channel.sent] == ["Here is Friday."]
    assert await _is_on(db, noa, group)


async def test_roster_asked_once_and_only_when_needed(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _opened_pair(db, group)
    _, channel = await _ask(db, owner_conn, onboarding_settings)
    assert len(channel.roster_calls) == 1


async def test_roster_not_asked_when_nobody_opened(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    _, channel = await _ask(db, owner_conn, onboarding_settings)
    assert channel.roster_calls == []


async def test_suspended_member_is_not_read(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    _, noa = await _opened_pair(db, group)
    await suspend(db, noa)
    llm, _ = await _ask(db, owner_conn, onboarding_settings)
    assert [p["name"] for p in _tool_result(llm)["people"]] == ["Dana"]


async def test_disconnected_member_keeps_only_native_titles(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    noa = await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    async with db.tenant_session(TenantId(noa)) as session:
        await event_rows.upsert_google(
            session, noa, CalendarEvent("n1", "Google thing", _at(14), _at(15), private=False)
        )
        await event_rows.add_event(
            session, noa, CalendarEvent("", "Remi thing", _at(16), _at(17)), source="remi"
        )
        connection = await active_connection(session, noa)
        assert connection is not None
        await revoke_connection(session, noa, connection.id)
    llm, _ = await _ask(db, owner_conn, onboarding_settings)
    assert [e["title"] for e in _entry(llm, "Noa")["events"]] == ["Remi thing"]
    assert "Google thing" not in _seen(llm)


async def test_private_event_is_not_in_granted_details(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _granted(db, group)
    calendar = Calendars(
        {
            "ca_noa": [
                CalendarEvent("n1", "Secret thing", _at(14), _at(15), private=True),
                CalendarEvent("n2", "Lunch out", _at(12), _at(13), private=False),
            ]
        }
    )
    llm = ScriptedLLM(said("", FRIDAY), said("Noa is busy."))
    channel, _ = await _say(db, owner_conn, onboarding_settings, llm, calendar=calendar)
    private = channel.sent[0]
    assert "Lunch out" in private.body
    assert "Secret thing" not in private.body


SHOW = "Remi, show my events to this group"
STAND_IN = (
    "[My last reply drew on people's calendars and isn't kept. {asked}"
    "To repeat anything, check the calendars again.]"
)


async def _details(db: Database, group: UUID) -> dict[UUID, bool]:
    async with db.system_session() as session:
        members = await groups.group_tenants(session, group)
    return {tenant_id: await _is_on(db, tenant_id, group) for tenant_id, _ in members}


async def _last_context(db: Database, group: UUID) -> str:
    async with db.system_session() as session:
        return [c for c in await groups.recent_context(session, group) if c.sender_name == REMI][
            -1
        ].body


async def test_show_my_events_changes_only_the_writer(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=False)
    noa = await _member(db, group, NOA, "Noa", "ca_noa", sharing=True)
    llm = ScriptedLLM(said("", call("show_my_events", on=True)), said("Sure."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm, body=SHOW)

    assert await _details(db, group) == {dana: True, noa: False}
    assert [message.body for message in channel.sent] == [f"Sure.\n\n{t('group_details_on', 'en')}"]


async def test_stranger_cannot_show_events(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=False)
    llm = ScriptedLLM(said("", call("show_my_events", on=True)), said("No."))

    await _say(db, owner_conn, onboarding_settings, llm, phone=STRANGER, body=SHOW)

    assert llm.calls[1][-1].content == turn_module.NOT_A_MEMBER
    assert await _details(db, group) == {dana: False}


async def test_show_my_events_off_keeps_busy_free(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, details=True)
    llm = ScriptedLLM(said("", call("show_my_events", on=False)), said("Okay."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm, body="Remi, stop showing")

    assert await _details(db, group) == {dana: False}
    assert (await _sharing(db, group))[dana] is True
    assert [message.body for message in channel.sent] == [
        f"Okay.\n\n{t('group_details_off', 'en')}"
    ]


async def test_unaddressed_okay_never_opens_details(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    noa = await _asked(db, group, NOA, "Noa", "ca_noa")
    llm = ScriptedLLM(said("YES"), said("", call("show_my_events", on=True)), said("Noa's in."))

    await _say(db, owner_conn, onboarding_settings, llm, phone=NOA, body="Dan said it's fine")

    assert llm.calls[2][-1].content == turn_module._NOT_AN_OKAY
    assert (await _sharing(db, group))[noa] is True
    assert await _details(db, group) == {noa: False}


@pytest.mark.parametrize(
    ("start", "calls", "expected"),
    [
        (
            "both",
            [call("share_my_calendar", on=False), call("show_my_events", on=False)],
            "shared_off",
        ),
        (
            "none",
            [call("show_my_events", on=True), call("share_my_calendar", on=False)],
            "shared_off",
        ),
        ("none", [call("show_my_events", on=False)], "shared_off"),
        ("both", [call("show_my_events", on=False)], "details_off"),
    ],
    ids=["stop-everything", "show-then-stop", "never-shared", "details-only-off"],
)
async def test_confirmation_tells_the_final_state(
    db: Database,
    owner_conn: Any,
    onboarding_settings: Settings,
    group: UUID,
    start: str,
    calls: list[Any],
    expected: str,
) -> None:
    both = start == "both"
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=both, details=both)
    llm = ScriptedLLM(*(said("", c) for c in calls), said("Okay."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm, body="Remi, change it")

    line = t(f"group_{expected}", "en")
    assert [message.body for message in channel.sent] == [f"Okay.\n\n{line}"]


async def test_stand_in_names_only_who_the_reply_asked(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, details=True)
    omer = await _member(db, group, "+972504444444", "Omer", None, sharing=False)
    llm = ScriptedLLM(said("", FRIDAY), said("Dana has the Dentist."))
    channel = FakeOutbound(name="gowa", roster=EVERYONE)
    await _say(db, owner_conn, onboarding_settings, llm, channel=channel)
    assert await _last_context(db, group) == STAND_IN.format(asked="")

    # "ok" reads no further than the sender's open ask and the asking line: no model call.
    await _say(
        db,
        owner_conn,
        onboarding_settings,
        NoLLM(),
        phone="+972504444444",
        body="ok",
        channel=channel,
    )
    assert (await _sharing(db, group))[omer] is False


@pytest.mark.parametrize(
    "tool",
    [
        call("show_my_events", on=True),
        call("share_my_calendar", on=True),
        ToolCall("c1", "call_me", {"name": "Dee"}),
    ],
)
async def test_titles_cant_write_consent(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID, tool: Any
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=False)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    llm = ScriptedLLM(said("", FRIDAY), said("", tool), said("Okay."))

    await _say(
        db, owner_conn, onboarding_settings, llm, channel=FakeOutbound(name="gowa", roster=EVERYONE)
    )

    assert llm.calls[2][-1].content == turn_module._AFTER_TITLES
    assert (await _details(db, group))[dana] is False
    assert (await _sharing(db, group))[dana] is False
    async with db.tenant_session(TenantId(dana)) as session:
        tenant = await get_tenant(session, dana)
    assert tenant is not None
    assert tenant.display_name == "Dana"


async def test_turning_off_works_after_titles(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, details=True)
    llm = ScriptedLLM(said("", FRIDAY), said("", call("show_my_events", on=False)), said("Okay."))

    await _say(
        db, owner_conn, onboarding_settings, llm, channel=FakeOutbound(name="gowa", roster=EVERYONE)
    )

    assert "Dentist" in _seen(llm)  # titles were revealed first
    assert (await _details(db, group))[dana] is False


async def test_titles_are_not_kept_in_context(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True, details=True)
    llm = ScriptedLLM(said("", FRIDAY), said("Noa has the Board meeting at 14:00."))

    channel, _ = await _say(
        db, owner_conn, onboarding_settings, llm, channel=FakeOutbound(name="gowa", roster=EVERYONE)
    )

    [sent] = channel.sent
    assert "Board meeting" in sent.body
    kept = await _last_context(db, group)
    assert kept == STAND_IN.format(asked="")
    assert "Board meeting" not in kept


async def test_a_busy_only_reply_is_kept_whole(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, NOA, "Noa", "ca_noa", sharing=True)
    llm = ScriptedLLM(said("", FRIDAY), said("Noa is busy 14:00 to 16:00."))

    await _say(db, owner_conn, onboarding_settings, llm, phone=NOA)

    assert await _last_context(db, group) == "Noa is busy 14:00 to 16:00."


async def test_okay_still_read_after_a_reply_with_titles(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True, details=True)
    omer = await _member(db, group, "+972504444444", "Omer", None, sharing=False)
    llm = ScriptedLLM(said("", FRIDAY), said("Dana has the Dentist. Omer, I need your okay."))
    channel = FakeOutbound(name="gowa", roster=EVERYONE)
    await _say(db, owner_conn, onboarding_settings, llm, channel=channel)
    assert await _last_context(db, group) == STAND_IN.format(asked="I asked Omer to okay sharing. ")

    again = ScriptedLLM(said("YES"), said("Omer's in."))
    await _say(
        db,
        owner_conn,
        onboarding_settings,
        again,
        phone="+972504444444",
        body="ok",
        channel=channel,
    )

    assert (await _sharing(db, group))[omer] is True


# --- asking from a private chat (#82) ---

JID2 = "120363402106000002@g.us"


class _Rosters(FakeOutbound):
    """Each group its own roster, by jid."""

    def __init__(self, by_jid: dict[str, GroupRoster | None]) -> None:
        super().__init__()
        self.by_jid = by_jid

    async def group_roster(self, group_jid: str) -> GroupRoster | None:
        return self.by_jid.get(group_jid)


async def _second_group(db: Database, tenant_id: UUID) -> UUID:
    async with db.system_session() as session:
        found = await groups.ensure_group(session, channel="gowa", jid=JID2)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await groups.touch_member(session, tenant_id, found.id, NOW)
    return found.id


async def _shown(db: Database, tenant_id: UUID, group_id: UUID) -> bool:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await groups.details_on(session, tenant_id, group_id)


def _named(name: str, *, phones: frozenset[str] = frozenset({DANA})) -> GroupRoster:
    return GroupRoster(name, phones, True)


async def test_show_in_group_one_group_needs_no_name(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    rosters = _Rosters({JID: _named("Family")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("", True)
    assert result.startswith("ok:")
    assert "Family" in result
    assert await _shown(db, dana, group)


async def test_show_in_group_checks_a_name_it_cant_match(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    rosters = _Rosters({JID: _named("Family")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("soccer", True)
    assert result.startswith("error:")
    assert "Family" in result
    assert not await _shown(db, dana, group)


async def test_show_in_group_several_lists_the_names(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    other = await _second_group(db, dana)
    rosters = _Rosters({JID: _named("Family"), JID2: _named("Soccer")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("", True)
    assert result.startswith("error: which group?")
    assert "Family" in result
    assert "Soccer" in result
    assert not await _shown(db, dana, group)
    assert not await _shown(db, dana, other)


async def test_show_in_group_picks_the_named_one(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    other = await _second_group(db, dana)
    rosters = _Rosters({JID: _named("Family"), JID2: _named("Soccer")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("family", True)
    assert result.startswith("ok:")
    assert await _shown(db, dana, group)
    assert not await _shown(db, dana, other)


async def test_show_in_group_skips_a_group_they_left(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    other = await _second_group(db, dana)
    rosters = _Rosters({JID: _named("Family", phones=frozenset({NOA})), JID2: _named("Soccer")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("", True)
    assert result.startswith("ok:")
    assert await _shown(db, dana, other)
    assert not await _shown(db, dana, group)


async def test_show_in_group_cant_tell_says_so(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    other = await _second_group(db, dana)
    result = await TenantGrants(db, dana, channel=_Rosters({})).show_in_group("", True)
    assert result == "error: can't tell which group; ask them to say it in that group"
    assert not await _shown(db, dana, group)
    assert not await _shown(db, dana, other)


async def test_show_in_group_no_groups(db: Database) -> None:
    tenant_id = await new_tenant(db, phone=DANA, status="active", step=None)
    result = await TenantGrants(db, tenant_id, channel=_Rosters({})).show_in_group("", True)
    assert result == "error: they aren't in a group with you yet; they can say it in the group"


async def test_show_in_group_off_with_one_open(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=True, details=True)
    await _second_group(db, dana)
    rosters = _Rosters({JID: _named("Family")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("", False)
    assert result == "ok: Family only sees when they're busy now"
    assert not await _shown(db, dana, group)


async def test_show_in_group_changes_only_its_owner(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    noa = await _member(db, group, NOA, "Noa", None, sharing=False)
    rosters = _Rosters({JID: _named("Family")})
    await TenantGrants(db, dana, channel=rosters).show_in_group("", True)
    assert await _shown(db, dana, group)
    assert not await _shown(db, noa, group)


async def test_show_in_group_a_name_it_cant_check_opens_nothing(db: Database, group: UUID) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    result = await TenantGrants(db, dana, channel=_Rosters({})).show_in_group("soccer", True)
    assert result == "error: can't tell which group; ask them to say it in that group"
    assert not await _shown(db, dana, group)


async def test_show_in_group_a_name_with_no_group_reader_opens_nothing(
    db: Database, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=False)
    result = await TenantGrants(db, dana, channel=None).show_in_group("soccer", True)
    assert result.startswith("error: can't tell")
    assert not await _shown(db, dana, group)


async def test_show_in_group_off_closes_all_when_names_cant_be_read(
    db: Database, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=True, details=True)
    other = await _second_group(db, dana)
    async with db.tenant_session(TenantId(dana)) as session:
        await groups.set_details(session, dana, other, True)
    result = await TenantGrants(db, dana, channel=_Rosters({})).show_in_group("soccer", False)
    assert result.startswith("ok:")
    assert not await _shown(db, dana, group)
    assert not await _shown(db, dana, other)


async def test_show_in_group_off_with_a_readable_wrong_name_closes_nothing(
    db: Database, group: UUID
) -> None:
    dana = await _member(db, group, DANA, "Dana", None, sharing=True, details=True)
    other = await _second_group(db, dana)
    async with db.tenant_session(TenantId(dana)) as session:
        await groups.set_details(session, dana, other, True)
    rosters = _Rosters({JID: _named("Family"), JID2: _named("Soccer")})
    result = await TenantGrants(db, dana, channel=rosters).show_in_group("book club", False)
    assert result.startswith("error: which group?")
    assert await _shown(db, dana, group)
    assert await _shown(db, dana, other)
