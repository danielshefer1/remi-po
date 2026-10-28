"""``send_once`` claimed on an idempotency key (D6): every ADR 0003 path, for a send that
answers no inbound message. The ``(inbox_id, kind)`` paths are pinned in
``test_handle_inbound.py``."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    ChannelError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.messaging.outbox import already_sent, send_once, send_parts, sent_message_id
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, insert_inbox
from tests.fixtures.payloads import SENDER_PHONE

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables")]

KEY = "connected:0b7c2c84"
KIND = "onboarding:all_set"
TEXT = "You're all set! Your Google Calendar is connected."
RECIPIENT = f"tel:{SENDER_PHONE}"


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _send(db: Database, channel: FakeOutbound, *, key: str = KEY, kind: str = KIND) -> str:
    return await send_once(
        db,
        channel,
        inbox_id=None,
        kind=kind,
        recipient_key=RECIPIENT,
        to=SENDER_PHONE,
        text=TEXT,
        idempotency_key=key,
    )


async def _outbox(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT inbox_id, kind, idempotency_key, status, error_code FROM channel_outbox "
        "ORDER BY created_at"
    )
    return [dict(row) for row in rows]


class TestSentMessageId:
    async def test_sent_message_id_after_accept(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await insert_inbox(owner_conn)
        channel = FakeOutbound()
        assert await sent_message_id(db, inbox_id, "poll") is None
        await send_once(
            db,
            channel,
            inbox_id=inbox_id,
            kind="poll",
            recipient_key="group:120363@g.us",
            to="120363@g.us",
            text="Where to?",
            poll_options=("Beach", "Park"),
        )
        assert channel.sent[0].poll_options == ("Beach", "Park")
        assert await sent_message_id(db, inbox_id, "poll") == "whatsapp.OUT1"
        assert await sent_message_id(db, inbox_id, "other") is None


class TestIdempotencyKey:
    async def test_sends_once_however_often_it_is_called(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound()
        assert [await _send(db, channel) for _ in range(3)] == ["accepted"] * 3
        assert channel.sent == [OutboundMessage(recipient=SENDER_PHONE, body=TEXT)]
        assert await _outbox(owner_conn) == [
            {
                "inbox_id": None,
                "kind": KIND,
                "idempotency_key": KEY,
                "status": "accepted",
                "error_code": None,
            }
        ]

    async def test_distinct_keys_are_distinct_sends(self, db: Database) -> None:
        channel = FakeOutbound()
        await _send(db, channel, key="connected:a")
        await _send(db, channel, key="connected:b")
        assert len(channel.sent) == 2

    async def test_the_key_is_global_across_kinds(self, db: Database) -> None:
        """A key names one event. Reusing it under another kind is the same send."""
        channel = FakeOutbound()
        await _send(db, channel, kind="onboarding:all_set")
        await _send(db, channel, kind="reminder")
        assert len(channel.sent) == 1

    async def test_transient_then_success_sends_once(self, db: Database, owner_conn: Any) -> None:
        channel = FakeOutbound(TransientChannelError("429"))
        with pytest.raises(TransientChannelError):
            await _send(db, channel)
        assert (await _outbox(owner_conn))[0]["status"] == "pending"

        assert await _send(db, channel) == "accepted"
        assert len(channel.attempts) == 2
        assert len(channel.sent) == 1

    async def test_an_ambiguous_send_is_never_retried(self, db: Database) -> None:
        channel = FakeOutbound(AmbiguousDeliveryError("read timeout"))
        assert await _send(db, channel) == "unknown"
        assert await _send(db, channel) == "unknown"
        assert len(channel.attempts) == 1

    async def test_a_send_interrupted_mid_flight_is_marked_unknown_not_resent(
        self, db: Database, owner_conn: Any
    ) -> None:
        await owner_conn.execute(
            "INSERT INTO channel_outbox (channel, kind, recipient_key, idempotency_key, status) "
            "VALUES ('whatsapp', $1, $2, $3, 'sending')",
            KIND,
            RECIPIENT,
            KEY,
        )
        channel = FakeOutbound()
        assert await _send(db, channel) == "unknown"
        assert channel.attempts == []

    async def test_a_rejected_send_is_recorded_and_not_retried(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound(RejectedChannelError(131047))
        assert await _send(db, channel) == "failed"
        assert await _send(db, channel) == "failed"
        assert len(channel.attempts) == 1
        assert (await _outbox(owner_conn))[0]["error_code"] == 131047

    async def test_keyed_and_inbox_sends_do_not_collide(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id: UUID = await insert_inbox(owner_conn)
        channel = FakeOutbound()
        await send_once(
            db,
            channel,
            inbox_id=inbox_id,
            kind=KIND,
            recipient_key=RECIPIENT,
            to=SENDER_PHONE,
            text=TEXT,
        )
        await _send(db, channel)
        assert len(channel.sent) == 2
        assert {row["idempotency_key"] for row in await _outbox(owner_conn)} == {None, KEY}

    async def test_the_outbox_holds_no_text(self, db: Database, owner_conn: Any) -> None:
        await _send(db, FakeOutbound())
        rows = await owner_conn.fetch("SELECT row_to_json(o)::text AS j FROM channel_outbox o")
        assert rows
        assert all("all set" not in row["j"] for row in rows)


class TestArguments:
    @pytest.mark.parametrize("both", [True, False])
    async def test_exactly_one_of_inbox_id_and_key(
        self, db: Database, owner_conn: Any, both: bool
    ) -> None:
        inbox_id = await insert_inbox(owner_conn) if both else None
        with pytest.raises(ValueError, match="exactly one"):
            await send_once(
                db,
                FakeOutbound(),
                inbox_id=inbox_id,
                kind=KIND,
                recipient_key=RECIPIENT,
                to=SENDER_PHONE,
                text=TEXT,
                idempotency_key=KEY if both else None,
            )
        assert await _outbox(owner_conn) == []


class TestAlreadySent:
    @pytest.mark.parametrize(
        ("failures", "sent"),
        [
            ((), True),
            ((AmbiguousDeliveryError("timeout"),), True),
            ((TransientChannelError("429"),), False),
        ],
    )
    async def test_claimed_counts_and_pending_does_not(
        self, db: Database, owner_conn: Any, failures: tuple[ChannelError, ...], sent: bool
    ) -> None:
        """A reply put back to ``pending`` was definitely not sent, so it is worked out again."""
        inbox_id = await insert_inbox(owner_conn)
        assert not await already_sent(db, inbox_id, KIND)
        with contextlib.suppress(TransientChannelError):
            await send_once(
                db,
                FakeOutbound(*failures),
                inbox_id=inbox_id,
                kind=KIND,
                recipient_key=RECIPIENT,
                to=SENDER_PHONE,
                text=TEXT,
            )
        assert await already_sent(db, inbox_id, KIND) is sent
        assert not await already_sent(db, inbox_id, "another:kind")


class SecondRefused(FakeOutbound):
    async def send_text(self, message: OutboundMessage) -> str:
        if len(self.attempts) == 1:
            self.attempts.append(message)
            raise RejectedChannelError(131)
        return await super().send_text(message)


class SecondTransient(FakeOutbound):
    async def send_text(self, message: OutboundMessage) -> str:
        if len(self.attempts) == 1:
            self.attempts.append(message)
            raise TransientChannelError("down")
        return await super().send_text(message)


class TestSendParts:
    async def _parts(self, db: Database, channel: FakeOutbound, inbox_id: UUID, *parts: str) -> str:
        sent, _ = await send_parts(
            db,
            channel,
            inbox_id=inbox_id,
            kind="agent_reply",
            recipient_key=RECIPIENT,
            to=SENDER_PHONE,
            parts=parts,
        )
        return sent

    async def test_parts_sent_in_order_with_own_keys(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await insert_inbox(owner_conn)
        channel = FakeOutbound()
        assert await self._parts(db, channel, inbox_id, "one", "two", "three") == "accepted"
        assert [m.body for m in channel.sent] == ["one", "two", "three"]
        rows = await owner_conn.fetch("SELECT kind, status FROM channel_outbox ORDER BY kind")
        assert [(r["kind"], r["status"]) for r in rows] == [
            ("agent_reply", "accepted"),
            ("agent_reply:2", "accepted"),
            ("agent_reply:3", "accepted"),
        ]

    async def test_rerun_sends_nothing(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await insert_inbox(owner_conn)
        channel = FakeOutbound()
        await self._parts(db, channel, inbox_id, "one", "two")
        assert await self._parts(db, channel, inbox_id, "one", "two", "three") == "accepted"
        assert [m.body for m in channel.sent] == ["one", "two"]

    async def test_refused_part_stops_rest(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await insert_inbox(owner_conn)
        channel = SecondRefused()
        assert await self._parts(db, channel, inbox_id, "one", "two", "three") == "accepted"
        assert [m.body for m in channel.attempts] == ["one", "two"]
        rows = await owner_conn.fetch("SELECT kind, status FROM channel_outbox ORDER BY kind")
        assert [(r["kind"], r["status"]) for r in rows] == [
            ("agent_reply", "accepted"),
            ("agent_reply:2", "failed"),
        ]

    async def test_transient_later_part_stops_and_fails_it(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id = await insert_inbox(owner_conn)
        channel = SecondTransient()
        assert await self._parts(db, channel, inbox_id, "one", "two", "three") == "accepted"
        assert [m.body for m in channel.attempts] == ["one", "two"]
        rows = await owner_conn.fetch("SELECT kind, status FROM channel_outbox ORDER BY kind")
        assert [(r["kind"], r["status"]) for r in rows] == [
            ("agent_reply", "accepted"),
            ("agent_reply:2", "failed"),
        ]
