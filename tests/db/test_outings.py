"""Outings (Iteration 07): the poll, the votes, and answers read from the group's chat.

Done-When: a plan set up in the group goes out as one poll; an "in" vote adds the event to
the voter's own calendar at most once, however many times they vote; a stranger's vote does
nothing; and a reason said in the group, not to Remi, is kept and stops the follow-ups.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import GroupMember, PollAnswer
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
JID = "120363402106000001@g.us"
DANA, NOA = "+972501111111", "+972502222222"
STRANGER = "+972500000009"
DINNER = call(
    "set_up_outing", title="Family dinner", start="2026-10-23T19:00", end="2026-10-23T21:00"
)
POLL_ID = "gowa.OUT1"  # FakeOutbound's id for the first send: the poll


class Calendar(InMemoryCalendar):
    """Keeps which account each event was created in."""

    def __init__(self) -> None:
        super().__init__()
        self.created_in: list[str] = []

    async def create_event(
        self, account_id: str, user_id: str, event: CalendarEvent
    ) -> CalendarEvent:
        self.created_in.append(account_id)
        return await super().create_event(account_id, user_id, event)


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


async def _member(db: Database, group_id: UUID, phone: str, name: str, account: str) -> UUID:
    tenant_id = await new_tenant(db, phone=phone, status="active", step=None)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
        await bind_connection(
            session, tenant_id, connected_account_id=account, auth_config_id="ac_test"
        )
        await groups.set_display_name(session, tenant_id, name)
        await groups.touch_member(session, tenant_id, group_id, NOW)
        await groups.set_sharing(session, tenant_id, group_id, True)
    return tenant_id


async def _answer(db: Database, tenant_id: UUID) -> PollAnswer | None:
    """Their answer to the one poll, read in their own session: RLS hides it from others."""
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return (await session.scalars(select(PollAnswer))).one_or_none()


class Group:
    """One family group: messages go through the gate, on one channel and one calendar."""

    def __init__(self, db: Database, conn: Any, settings: Settings) -> None:
        self.db, self.conn, self.settings = db, conn, settings
        self.channel = FakeOutbound(name="gowa")
        self.calendar = Calendar()

    async def say(
        self,
        llm: Any,
        *,
        phone: str = DANA,
        body: str | None = "Remi, set up dinner on Friday at 7",
        message_type: str = "text",
        reply_id: str | None = None,
    ) -> None:
        inbox_id = await insert_inbox(
            self.conn,
            phone=phone,
            body=body,
            group_id=JID,
            sender_name="WhatsApp name",
            sent_at=NOW,
            message_type=message_type,
            reply_id=reply_id,
        )
        await handle_inbound(
            inbox_id,
            db=self.db,
            channels={self.channel.name: self.channel}.__getitem__,
            allowlist=frozenset(),
            on_allowed=Spy(),
            settings=self.settings,
            on_group=functools.partial(group_turn, db=self.db, llm=llm, calendar=self.calendar),
            now=lambda: NOW,
        )

    async def set_up(
        self, plan: Any = DINNER, body: str = "Remi, set up dinner on Friday at 7"
    ) -> None:
        await self.say(ScriptedLLM(said("", plan), said("Done, the poll's up.")), body=body)

    async def vote(self, phone: str, option: str | None) -> None:
        await self.say(
            NoLLM(), phone=phone, body=option, message_type="poll_vote", reply_id=POLL_ID
        )


@pytest.fixture
def family(db: Database, owner_conn: Any, onboarding_settings: Settings) -> Group:
    return Group(db, owner_conn, onboarding_settings)


async def test_set_up_outing_posts_one_poll(
    db: Database, owner_conn: Any, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")

    await family.set_up()

    poll, reply = family.channel.sent
    assert poll.recipient == JID
    assert poll.poll_options == ("I'm in", "Can't make it")
    assert "Family dinner" in poll.body
    assert "\n" not in poll.body
    assert reply.body == "Done, the poll's up."
    stored = await owner_conn.fetchrow("SELECT provider_message_id, status FROM group_polls")
    assert tuple(stored) == (POLL_ID, "open")
    context = await owner_conn.fetch("SELECT sender_name, body FROM group_context ORDER BY sent_at")
    assert any(row["sender_name"] == "Remi" and "Family dinner" in row["body"] for row in context)


async def test_stranger_cannot_set_up_outing(
    db: Database, owner_conn: Any, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    llm = ScriptedLLM(said("", DINNER), said("Only people set up with me can do that."))

    await family.say(llm, phone=STRANGER)

    assert [message.poll_options for message in family.channel.sent] == [()]
    assert await owner_conn.fetchval("SELECT count(*) FROM group_polls") == 0


async def test_vote_in_creates_event_in_voters_calendar(
    db: Database, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()

    await family.vote(NOA, "I'm in")

    assert family.calendar.created_in == ["ca_noa"]
    [event] = family.calendar.events
    assert (event.title, event.start) == ("Family dinner", datetime(2026, 10, 23, 16, tzinfo=UTC))
    private = family.channel.sent[-1]
    assert private.recipient == NOA
    assert "Family dinner" in private.body
    # Nothing about it in the group: the poll and the reply only.
    assert [message.recipient for message in family.channel.sent].count(JID) == 2


async def test_vote_in_twice_creates_one_event(db: Database, family: Group, group: UUID) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()

    await family.vote(NOA, "I'm in")
    await family.vote(NOA, "I'm in")

    assert family.calendar.created_in == ["ca_noa"]
    assert [message.recipient for message in family.channel.sent].count(NOA) == 1


async def test_vote_back_in_reuses_event(db: Database, family: Group, group: UUID) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()

    await family.vote(NOA, "I'm in")
    await family.vote(NOA, "Can't make it")
    assert family.channel.sent[-1].body == t("outing_kept", "en")
    assert family.channel.sent[-1].recipient == NOA
    await family.vote(NOA, "I'm in")

    assert family.calendar.created_in == ["ca_noa"]
    answer = await _answer(db, noa)
    assert answer is not None
    assert answer.answer == "in"


async def test_cleared_vote_clears_the_answer(db: Database, family: Group, group: UUID) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()

    await family.vote(NOA, "Can't make it")
    answer = await _answer(db, noa)
    assert answer is not None
    assert answer.answer == "out"
    await family.vote(NOA, None)

    answer = await _answer(db, noa)
    assert answer is not None
    assert answer.answer is None
    assert family.calendar.created_in == []


async def test_cleared_vote_keeps_a_reason_given_in_chat(
    db: Database, owner_conn: Any, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()
    await family.say(ScriptedLLM(said("OUT 1: working")), phone=NOA, body="I'm working Friday")
    await family.vote(NOA, "Can't make it")

    await family.vote(NOA, None)

    answer = await _answer(db, noa)
    assert answer is not None
    assert (answer.answer, answer.reason) == (None, "working")
    poll = await owner_conn.fetchval("SELECT id FROM group_polls")
    async with db.system_session() as session:
        assert (poll, noa) not in await groups.due_nudges(session, NOW + timedelta(hours=3))


async def test_vote_after_the_outing_is_ignored(
    db: Database, owner_conn: Any, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()
    await owner_conn.execute(
        "UPDATE group_polls SET starts_at = $1, ends_at = $2",
        NOW - timedelta(hours=3),
        NOW,
    )
    sent = len(family.channel.sent)

    await family.vote(NOA, "I'm in")

    assert family.calendar.created_in == []
    assert len(family.channel.sent) == sent
    assert await _answer(db, noa) is None


async def test_all_day_outing_today_is_posted(
    db: Database, owner_conn: Any, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    beach = call(
        "set_up_outing", title="Beach day", start="2026-10-21", end="2026-10-21", all_day=True
    )

    await family.set_up(beach, body="Remi, set up a beach day today")

    poll, _ = family.channel.sent
    assert poll.poll_options == ("I'm in", "Can't make it")
    assert "Beach day" in poll.body
    # And it still counts on its day: a vote adds it.
    await family.vote(NOA, "I'm in")
    assert family.calendar.created_in == ["ca_noa"]
    answer = await _answer(db, noa)
    assert answer is not None
    assert answer.answer == "in"


async def test_hebrew_outing(db: Database, family: Group, group: UUID) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    await _member(db, group, NOA, "Noa", "ca_noa")
    dinner = call(
        "set_up_outing", title="ארוחה משפחתית", start="2026-10-23T19:00", end="2026-10-23T21:00"
    )

    await family.set_up(dinner, body="רמי, תארגן לנו ארוחה ביום שישי בשבע")

    poll, _ = family.channel.sent
    assert poll.recipient == JID
    assert poll.poll_options == ("אני בפנים", "לא מסתדר לי")
    assert "ארוחה משפחתית" in poll.body
    await family.vote(NOA, "אני בפנים")
    assert family.calendar.created_in == ["ca_noa"]
    assert family.channel.sent[-1].recipient == NOA


async def test_vote_from_stranger_is_ignored(db: Database, family: Group, group: UUID) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    await family.set_up()
    sent = len(family.channel.sent)

    await family.vote(STRANGER, "I'm in")

    assert family.calendar.created_in == []
    assert len(family.channel.sent) == sent


async def test_vote_on_an_unknown_poll_or_option_is_ignored(
    db: Database, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()

    await family.say(
        NoLLM(), phone=NOA, body="I'm in", message_type="poll_vote", reply_id="gowa.OTHER"
    )
    await family.vote(NOA, "Maybe")

    assert family.calendar.created_in == []
    assert await _answer(db, noa) is None


async def test_unaddressed_reason_stops_follow_ups(
    db: Database, owner_conn: Any, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()
    await owner_conn.execute("UPDATE group_polls SET created_at = $1", NOW - timedelta(hours=3))
    poll = await owner_conn.fetchval("SELECT id FROM group_polls")
    async with db.system_session() as session:
        assert (poll, noa) in await groups.due_nudges(session, NOW)
    llm = ScriptedLLM(said("OUT 1: working"))

    await family.say(llm, phone=NOA, body="Friday doesn't work for me, I'm working")

    assert "Family dinner" in llm.calls[0][0].content
    answer = await _answer(db, noa)
    assert answer is not None
    assert (answer.answer, answer.reason) == ("out", "working")
    async with db.system_session() as session:
        assert (poll, noa) not in await groups.due_nudges(session, NOW)
    assert family.calendar.created_in == []
    assert len(family.channel.sent) == 2  # the poll and the reply: nothing new


async def test_unaddressed_from_answered_member_calls_no_model(
    db: Database, family: Group, group: UUID
) -> None:
    await _member(db, group, DANA, "Dana", "ca_dana")
    await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()
    await family.vote(NOA, "I'm in")

    await family.say(NoLLM(), phone=NOA, body="see you all there")
    await family.say(NoLLM(), phone=STRANGER, body="sounds fun")


async def test_not_an_okay_goes_on_to_the_outing(db: Database, family: Group, group: UUID) -> None:
    """Asked to okay sharing and with an outing open: a "no" from the okay check still reads
    the outing answer, two calls in all."""
    await _member(db, group, DANA, "Dana", "ca_dana")
    noa = await _member(db, group, NOA, "Noa", "ca_noa")
    await family.set_up()
    async with db.tenant_session(TenantId(noa)) as session:
        await session.execute(update(GroupMember).values(sharing=False, asked_at=NOW))
    async with db.system_session() as session:
        await groups.add_context(
            session, group, sender_name="Remi", body="Noa, I need your okay first.", sent_at=NOW
        )
    llm = ScriptedLLM(said("NO"), said("OUT 1: working"))

    await family.say(llm, phone=NOA, body="Friday doesn't work for me, I'm working")

    assert len(llm.calls) == 2
    answer = await _answer(db, noa)
    assert answer is not None
    assert (answer.answer, answer.reason) == ("out", "working")
