"""The group gate (Iteration 07), against Postgres, with a spy for the group turn.

Done-When: a group no member has written in costs nothing and keeps nothing; a served group
keeps a short context and hands every message to the turn, saying whether it was for Remi;
nothing said in a group lands in anyone's personal history.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import GroupMember
from personal_organizer.db.repositories.groups import group_tenants
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.groups.gate import GROUP_REPLY, GroupTurn
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.gowa_payloads import load
from tests.fixtures.tenants import IL_PHONE, messages_of, new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
JID = "120363402106000001@g.us"
BOT_PHONE = "+972552640741"
STRANGER = "+972500000009"


class GroupSpy:
    """Stands in for the group turn. Anything reaching it may cost an LLM call."""

    def __init__(self) -> None:
        self.calls: list[tuple[InboxRow, GroupTurn]] = []

    async def __call__(self, row: InboxRow, channel: OutboundChannel, turn: GroupTurn) -> None:
        self.calls.append((row, turn))


@pytest.fixture
def settings(onboarding_settings: Settings) -> Settings:
    onboarding = onboarding_settings.onboarding.model_copy(update={"bot_phone": BOT_PHONE})
    return onboarding_settings.model_copy(update={"onboarding": onboarding})


@pytest.fixture
async def db(settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _member(db: Database) -> UUID:
    return await new_tenant(db, phone=IL_PHONE, status="active", step=None)


async def _say(
    db: Database,
    conn: Any,
    settings: Settings | None,
    spy: GroupSpy,
    *,
    phone: str = IL_PHONE,
    body: str | None = "dinner friday?",
    name: str | None = "Dana",
    sent_at: datetime = NOW,
    now: datetime = NOW,
    **inbox: Any,
) -> tuple[UUID, str | None]:
    inbox_id = await insert_inbox(
        conn, phone=phone, body=body, group_id=JID, sender_name=name, sent_at=sent_at, **inbox
    )
    channel = FakeOutbound(name="gowa")
    disposition = await handle_inbound(
        inbox_id,
        db=db,
        channels={channel.name: channel}.__getitem__,
        allowlist=frozenset({STRANGER}),
        on_allowed=Spy(),
        settings=settings,
        on_group=spy,
        now=lambda: now,
    )
    assert channel.sent == []
    return inbox_id, disposition


async def _bot_said(conn: Any, inbox_id: UUID, *, at: datetime, message_id: str) -> None:
    """Remi's reply in the group to ``inbox_id``, as Task 5's ``send_once`` records it."""
    await conn.execute(
        "INSERT INTO channel_outbox (channel, inbox_id, kind, recipient_key, status, "
        "provider_message_id, created_at) VALUES ('gowa', $1, $2, $3, 'accepted', $4, $5)",
        inbox_id,
        GROUP_REPLY,
        f"group:{JID}",
        message_id,
        at,
    )


async def _context(conn: Any) -> list[tuple[str | None, str]]:
    rows = await conn.fetch("SELECT sender_name, body FROM group_context ORDER BY sent_at, id")
    return [(row["sender_name"], row["body"]) for row in rows]


async def _purged(conn: Any, inbox_id: UUID) -> bool:
    row = await conn.fetchrow("SELECT * FROM channel_inbox WHERE id = $1", inbox_id)
    return row["processed_at"] is not None and all(
        row[column] is None for column in ("body", "raw", "sender_name")
    )


async def test_unserved_group_is_purged_silently(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    spy = GroupSpy()
    inbox_id, disposition = await _say(
        db, owner_conn, settings, spy, phone=STRANGER, body="Remi, are you there?"
    )
    assert disposition == "group_unserved"
    assert spy.calls == []
    assert await _context(owner_conn) == []
    assert await _purged(owner_conn, inbox_id)
    assert await owner_conn.fetchval("SELECT served_at FROM wa_groups") is None


async def test_member_message_serves_group_and_calls_turn(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    tenant_id = await _member(db)
    spy = GroupSpy()
    inbox_id, disposition = await _say(db, owner_conn, settings, spy, body="Remi, dinner friday?")

    assert disposition == "group"
    [(row, turn)] = spy.calls
    assert row.body == "Remi, dinner friday?"
    assert (turn.jid, turn.tenant_id, turn.addressed, turn.now) == (JID, tenant_id, True, NOW)
    group = await owner_conn.fetchrow("SELECT id, served_at FROM wa_groups")
    assert (turn.group_id, group["served_at"]) == (group["id"], NOW)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        members = list(await session.scalars(select(GroupMember.group_id)))
        tenant = await get_tenant(session, tenant_id)
    assert members == [group["id"]]
    assert tenant is not None
    assert tenant.display_name == "Dana"
    assert await _context(owner_conn) == [("Dana", "Remi, dinner friday?")]
    assert await _purged(owner_conn, inbox_id)


async def test_stranger_in_served_group_is_context_only(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    tenant_id = await _member(db)
    spy = GroupSpy()
    await _say(db, owner_conn, settings, spy, body="dinner friday?")
    inbox_id, disposition = await _say(
        db,
        owner_conn,
        settings,
        spy,
        phone=STRANGER,
        name="Noa",
        body=None,
        message_type="image",
        sent_at=NOW + timedelta(minutes=1),
    )

    assert disposition == "group"
    assert [turn.tenant_id for _, turn in spy.calls] == [tenant_id, None]
    assert spy.calls[1][1].addressed is False
    assert await _context(owner_conn) == [("Dana", "dinner friday?"), ("Noa", "[image]")]
    async with db.system_session() as session:
        assert await group_tenants(session, spy.calls[1][1].group_id) == [(tenant_id, False)]
    assert await _purged(owner_conn, inbox_id)


async def test_poll_vote_is_handed_on_but_not_kept_as_context(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    await _member(db)
    spy = GroupSpy()
    _, disposition = await _say(
        db, owner_conn, settings, spy, body="I'm in", message_type="poll_vote", reply_id="POLL1"
    )
    assert disposition == "group"
    assert [row.reply_id for row, _ in spy.calls] == ["POLL1"]
    assert await _context(owner_conn) == []


async def test_retried_turn_keeps_one_context_line(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    """A turn that raised is retried with the same row: its line is not kept twice."""
    await _member(db)

    class FailsOnce(GroupSpy):
        async def __call__(self, row: InboxRow, channel: OutboundChannel, turn: GroupTurn) -> None:
            await super().__call__(row, channel, turn)
            if len(self.calls) == 1:
                raise RuntimeError("model down")

    spy = FailsOnce()
    inbox_id = await insert_inbox(owner_conn, phone=IL_PHONE, group_id=JID, sender_name="Dana")
    channel = FakeOutbound(name="gowa")
    for attempt in range(2):
        call = handle_inbound(
            inbox_id,
            db=db,
            channels={channel.name: channel}.__getitem__,
            allowlist=frozenset(),
            on_allowed=Spy(),
            settings=settings,
            on_group=spy,
        )
        if attempt == 0:
            with pytest.raises(RuntimeError):
                await call
        else:
            assert await call == "group"
    assert await _context(owner_conn) == [("Dana", "hi")]


async def test_group_message_not_in_personal_history(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    tenant_id = await _member(db)
    await _say(db, owner_conn, settings, GroupSpy(), body="Remi, remind me about the dentist")
    assert await messages_of(db, tenant_id) == []


async def test_stale_group_message_gets_no_turn(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    await _member(db)
    spy = GroupSpy()
    inbox_id, disposition = await _say(
        db, owner_conn, settings, spy, sent_at=NOW - timedelta(days=2)
    )
    assert disposition == "group_stale"
    assert spy.calls == []
    assert await _context(owner_conn) == []
    assert await _purged(owner_conn, inbox_id)


@pytest.mark.parametrize("composio_on", [False, True])
async def test_groups_off_purges_and_sends_nothing(
    db: Database, owner_conn: Any, settings: Settings, composio_on: bool
) -> None:
    """Composio off is the allowlist gate, which must never see a group: even an invited
    number writing in a group is purged, not acknowledged. So is everything with no turn."""
    inbox_id = await insert_inbox(owner_conn, phone=STRANGER, group_id=JID, sender_name="Noa")
    channel = FakeOutbound(name="gowa")
    disposition = await handle_inbound(
        inbox_id,
        db=db,
        channels={channel.name: channel}.__getitem__,
        allowlist=frozenset({STRANGER}),
        on_allowed=Spy(),
        settings=settings if composio_on else None,
        on_group=None,
    )
    assert disposition == "group_off"
    assert channel.sent == []
    assert await _purged(owner_conn, inbox_id)
    assert await owner_conn.fetchval("SELECT count(*) FROM wa_groups") == 0


async def test_quote_reply_to_remi_is_addressed(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    await _member(db)
    spy = GroupSpy()
    first, _ = await _say(db, owner_conn, settings, spy, body="Remi, dentist sunday?")
    quoted = load("group_reply")["payload"]
    await _bot_said(owner_conn, first, at=NOW, message_id=quoted["replied_to_id"])

    await _say(
        db,
        owner_conn,
        settings,
        spy,
        phone=STRANGER,
        body=quoted["body"],
        context_message_id=quoted["replied_to_id"],
        now=NOW + timedelta(hours=1),
    )
    assert spy.calls[1][1].addressed is True


async def _follow_up(db: Database, conn: Any, settings: Settings, minutes: int) -> list[bool]:
    """The member answers Remi's reply ``minutes`` later without naming it, then a stranger
    does: someone else's follow-up is never Remi's, however soon."""
    await _member(db)
    spy = GroupSpy()
    first, _ = await _say(db, conn, settings, spy, body="Remi, dentist sunday?")
    await _bot_said(conn, first, at=NOW, message_id="3EB0BOT0001")
    later = NOW + timedelta(minutes=minutes)
    await _say(db, conn, settings, spy, body="at 10", sent_at=later, now=later)
    await _say(db, conn, settings, spy, phone=STRANGER, body="ok", sent_at=later, now=later)
    return [turn.addressed for _, turn in spy.calls]


async def test_follow_up_within_five_minutes_is_addressed(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    assert await _follow_up(db, owner_conn, settings, 4) == [True, True, False]


async def test_follow_up_after_six_minutes_is_not(
    db: Database, owner_conn: Any, settings: Settings
) -> None:
    assert await _follow_up(db, owner_conn, settings, 6) == [True, False, False]
