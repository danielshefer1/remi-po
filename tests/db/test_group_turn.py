"""The group turn (Iteration 07), through the gate, with a scripted model and fake calendars.

Done-When: the model and the group see busy times and names, never what an event is; a
member who hasn't okayed the group is never read; okaying takes a member's own words; and a
person granted details gets them privately, by the grants as they stand at that moment.
"""

from __future__ import annotations

import functools
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.errors import CalendarProviderUnavailableError, TransientChannelError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.groups.turn import group_turn
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
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
    db: Database, group_id: UUID, phone: str, name: str, account: str, *, sharing: bool
) -> UUID:
    tenant_id = await new_tenant(db, phone=phone, status="active", step=None)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
        await bind_connection(
            session, tenant_id, connected_account_id=account, auth_config_id="ac_test"
        )
        await groups.set_display_name(session, tenant_id, name)
        await groups.touch_member(session, tenant_id, group_id, NOW)
        await groups.set_sharing(session, tenant_id, group_id, sharing)
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
) -> tuple[FakeOutbound, InMemoryCalendar]:
    """Run one message through the gate; pass ``channel`` and ``inbox_id`` to retry it."""
    calendar = calendar or Calendars(CALENDARS)
    channel = channel or FakeOutbound(name="gowa")
    if inbox_id is None:
        inbox_id = await insert_inbox(
            conn, phone=phone, body=body, group_id=JID, sender_name="WhatsApp name", sent_at=NOW
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
    """Noa's calendar answers who_is_free, then is down when her details are read."""

    async def list_events(
        self, account_id: str, user_id: str, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]:
        if account_id == "ca_noa" and any(call[0] == "ca_noa" for call in self.calls):
            raise CalendarProviderUnavailableError("composio down")
        return await super().list_events(account_id, user_id, start=start, end=end)


async def test_grantor_outage_retries_until_the_last_attempt(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _granted(db, group)
    reply = said("Noa's busy 2 to 4 on Friday.")
    channel = FakeOutbound(name="gowa")

    with pytest.raises(CalendarProviderUnavailableError):
        await _say(
            db,
            owner_conn,
            onboarding_settings,
            ScriptedLLM(said("", FRIDAY), reply),
            calendar=NoaDown(CALENDARS),
            channel=channel,
        )
    assert channel.sent == []

    [inbox_id] = await owner_conn.fetchval("SELECT array_agg(id) FROM channel_inbox")
    await _say(
        db,
        owner_conn,
        onboarding_settings,
        ScriptedLLM(said("", FRIDAY), reply),
        calendar=NoaDown(CALENDARS),
        channel=channel,
        inbox_id=inbox_id,
        last_attempt=True,
    )
    assert [message.recipient for message in channel.sent] == [JID]


async def test_details_follow_current_grants(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    dana, noa = await _granted(db, group)
    async with db.tenant_session(TenantId(noa)) as session:
        await groups.revoke(session, noa, dana)
    llm = ScriptedLLM(said("", FRIDAY), said("Noa's busy 2 to 4 on Friday."))

    channel, _ = await _say(db, owner_conn, onboarding_settings, llm)

    assert [message.recipient for message in channel.sent] == [JID]


async def test_unaddressed_message_calls_no_model(
    db: Database, owner_conn: Any, onboarding_settings: Settings, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana", sharing=True)
    channel, calendar = await _say(
        db, owner_conn, onboarding_settings, NoLLM(), body="who's free on Friday?"
    )
    assert channel.sent == []
    assert calendar.calls == []
