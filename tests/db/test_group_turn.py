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

from personal_organizer.core.errors import (
    CalendarProviderUnavailableError,
    LLMRejectedError,
    LLMUnavailableError,
    TransientChannelError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.groups import turn as turn_module
from personal_organizer.messaging.groups.gate import GROUP_REPLY, REMI
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
