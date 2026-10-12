"""The worker's gate, against Postgres, with a fake outbound channel.

Done-When: an unregistered sender uses no LLM budget. There is no LLM yet, so what is pinned
is the seam: a stranger never reaches ``on_allowed``, which Iteration 04 turns into "defer an
agent turn". Also pinned: every way a reply could be sent twice.
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    ChannelError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundChannel, OutboundMessage
from personal_organizer.messaging.inbound import InboxRow, handle_inbound
from personal_organizer.messaging.onboarding import Advance
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.replies import ACK_TEXT, INVITE_ONLY_TEXT
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings
from tests.fixtures.payloads import SENDER_PHONE

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables")]

STRANGER_PHONE = "+447700900123"
ALLOWLIST = frozenset({SENDER_PHONE})


class FakeOutbound:
    name = "whatsapp"

    def __init__(
        self, *failures: ChannelError, mark_read_fails: bool = False, name: str = "whatsapp"
    ) -> None:
        self.name = name
        self.failures = list(failures)
        self.mark_read_fails = mark_read_fails
        self.attempts: list[OutboundMessage] = []
        self.sent: list[OutboundMessage] = []
        self.read: list[str] = []

    async def send_text(self, message: OutboundMessage) -> str:
        self.attempts.append(message)
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)
        return f"wamid.OUT{len(self.sent)}"

    async def mark_read(self, provider_message_id: str) -> None:
        if self.mark_read_fails:
            raise TransientChannelError("graph down")
        self.read.append(provider_message_id)


class Spy:
    """Stands in for the agent handoff. Anything reaching it would, from Iteration 04,
    cost an LLM call."""

    def __init__(self) -> None:
        self.calls: list[InboxRow] = []

    async def __call__(
        self, row: InboxRow, channel: OutboundChannel, tenant: TenantState, now: datetime
    ) -> Advance | None:
        self.calls.append(row)
        return None


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _inbox(
    conn: Any,
    *,
    phone: str | None = SENDER_PHONE,
    user_id: str | None = None,
    body: str = "Oncology appointment with Dr Meyer",
    sent_at: datetime | None = None,
    channel: str = "whatsapp",
) -> UUID:
    key = f"uid:{user_id}" if user_id else f"tel:{phone}"
    inbox_id: UUID = await conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_user_id, "
        "sender_phone, message_type, body, raw, sent_at) "
        "VALUES ($8, $1, $2, $3, $4, 'text', $5, $6::jsonb, $7) RETURNING id",
        f"wamid.{uuid4().hex}",
        key,
        user_id,
        phone,
        body,
        json.dumps({"text": {"body": body}}),
        sent_at or datetime.now(UTC),
        channel,
    )
    return inbox_id


async def _run(
    db: Database, inbox_id: UUID, channel: FakeOutbound, on_allowed: Any = None, **kw: Any
) -> str | None:
    """Run the handler the way the task does, on the allowlist gate, which acknowledges."""
    return await handle_inbound(
        inbox_id,
        db=db,
        channels={channel.name: channel}.__getitem__,
        allowlist=ALLOWLIST,
        on_allowed=on_allowed or Spy(),
        **kw,
    )


async def _outbox(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT kind, status, provider_message_id, error_code FROM channel_outbox "
        "ORDER BY created_at"
    )
    return [dict(row) for row in rows]


async def _row(conn: Any, inbox_id: UUID) -> Any:
    return await conn.fetchrow("SELECT * FROM channel_inbox WHERE id = $1", inbox_id)


class TestAllowlisted:
    async def test_marked_read_and_acknowledged_once(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound()

        assert await _run(db, inbox_id, channel) == "allowed"

        assert channel.sent == [OutboundMessage(recipient=SENDER_PHONE, body=ACK_TEXT)]
        assert len(channel.read) == 1
        assert await _outbox(owner_conn) == [
            {
                "kind": "ack",
                "status": "accepted",
                "provider_message_id": "wamid.OUT1",
                "error_code": None,
            }
        ]
        row = await _row(owner_conn, inbox_id)
        assert row["disposition"] == "allowed"
        assert row["processed_at"] is not None
        assert row["body"] is not None  # kept for the agent, Iteration 04

    async def test_a_second_run_sends_nothing(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound()
        await _run(db, inbox_id, channel)
        assert await _run(db, inbox_id, channel) == "allowed"
        assert len(channel.sent) == 1

    async def test_a_crash_after_sending_does_not_resend(
        self, db: Database, owner_conn: Any
    ) -> None:
        """The job died after the send was recorded but before the row was finished."""
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound()
        await _run(db, inbox_id, channel)
        await owner_conn.execute(
            "UPDATE channel_inbox SET processed_at = NULL WHERE id = $1", inbox_id
        )

        await _run(db, inbox_id, channel)
        assert len(channel.sent) == 1

    async def test_transient_then_success_sends_once(self, db: Database, owner_conn: Any) -> None:
        """What Procrastinate's retry does: the job raises, then runs again."""
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound(TransientChannelError("429"))

        with pytest.raises(TransientChannelError):
            await _run(db, inbox_id, channel)
        assert (await _outbox(owner_conn))[0]["status"] == "pending"
        assert (await _row(owner_conn, inbox_id))["processed_at"] is None

        assert await _run(db, inbox_id, channel) == "allowed"
        assert len(channel.attempts) == 2
        assert len(channel.sent) == 1
        assert (await _outbox(owner_conn))[0]["status"] == "accepted"

    async def test_an_ambiguous_send_is_never_retried(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound(AmbiguousDeliveryError("read timeout"))

        assert await _run(db, inbox_id, channel) == "allowed"
        await owner_conn.execute(
            "UPDATE channel_inbox SET processed_at = NULL WHERE id = $1", inbox_id
        )
        await _run(db, inbox_id, channel)

        assert len(channel.attempts) == 1
        assert (await _outbox(owner_conn))[0]["status"] == "unknown"

    async def test_a_send_interrupted_mid_flight_is_marked_unknown_not_resent(
        self, db: Database, owner_conn: Any
    ) -> None:
        """A row left in ``sending`` means the worker died holding the request."""
        inbox_id = await _inbox(owner_conn)
        await owner_conn.execute(
            "INSERT INTO channel_outbox (channel, inbox_id, kind, recipient_key, status) "
            "VALUES ('whatsapp', $1, 'ack', $2, 'sending')",
            inbox_id,
            f"tel:{SENDER_PHONE}",
        )
        channel = FakeOutbound()
        await _run(db, inbox_id, channel)
        assert channel.attempts == []
        assert (await _outbox(owner_conn))[0]["status"] == "unknown"

    async def test_a_rejected_send_is_recorded_and_not_retried(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound(RejectedChannelError(131047))
        assert await _run(db, inbox_id, channel) == "allowed"
        assert await _outbox(owner_conn) == [
            {"kind": "ack", "status": "failed", "provider_message_id": None, "error_code": 131047}
        ]

    async def test_a_mark_read_failure_still_acknowledges(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound(mark_read_fails=True)
        assert await _run(db, inbox_id, channel) == "allowed"
        assert len(channel.sent) == 1

    async def test_a_stale_message_gets_no_reply(self, db: Database, owner_conn: Any) -> None:
        """Redelivered after the 24-hour window closed: any send would be refused."""
        sent_at = datetime.now(UTC) - timedelta(hours=30)
        inbox_id = await _inbox(owner_conn, sent_at=sent_at)
        channel = FakeOutbound()
        spy = Spy()
        assert await _run(db, inbox_id, channel, on_allowed=spy) == "stale"
        assert channel.attempts == []
        assert channel.read == []
        assert spy.calls == []

    async def test_just_inside_the_window_is_answered(self, db: Database, owner_conn: Any) -> None:
        sent_at = datetime.now(UTC) - timedelta(hours=23)
        inbox_id = await _inbox(owner_conn, sent_at=sent_at)
        channel = FakeOutbound()
        assert await _run(db, inbox_id, channel) == "allowed"


class TestStrangers:
    """Done-When: an unregistered sender never reaches the agent seam."""

    async def test_one_fixed_reply_no_handoff_and_content_purged(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id = await _inbox(owner_conn, phone=STRANGER_PHONE)
        channel = FakeOutbound()
        spy = Spy()

        assert await _run(db, inbox_id, channel, on_allowed=spy) == "stranger"

        assert spy.calls == []
        assert channel.read == []
        assert channel.sent == [OutboundMessage(recipient=STRANGER_PHONE, body=INVITE_ONLY_TEXT)]
        row = await _row(owner_conn, inbox_id)
        assert row["body"] is None
        assert row["raw"] is None
        assert row["disposition"] == "stranger"

    async def test_muted_for_a_day_after_the_first_reply(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound()
        spy = Spy()
        first = await _inbox(owner_conn, phone=STRANGER_PHONE)
        second = await _inbox(owner_conn, phone=STRANGER_PHONE, body="hello??")

        await _run(db, first, channel, on_allowed=spy)
        assert await _run(db, second, channel, on_allowed=spy) == "stranger_muted"

        assert len(channel.sent) == 1
        assert spy.calls == []
        assert (await _row(owner_conn, second))["body"] is None

    async def test_a_failed_invite_does_not_mute_the_next_one(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound(RejectedChannelError(131026))
        first = await _inbox(owner_conn, phone=STRANGER_PHONE)
        second = await _inbox(owner_conn, phone=STRANGER_PHONE)
        await _run(db, first, channel)
        assert await _run(db, second, channel) == "stranger"
        assert len(channel.sent) == 1

    async def test_the_mute_expires(self, db: Database, owner_conn: Any) -> None:
        channel = FakeOutbound()
        first = await _inbox(owner_conn, phone=STRANGER_PHONE)
        await _run(db, first, channel)
        await owner_conn.execute(
            "UPDATE channel_outbox SET created_at = now() - interval '25 hours'"
        )
        second = await _inbox(owner_conn, phone=STRANGER_PHONE)
        assert await _run(db, second, channel) == "stranger"
        assert len(channel.sent) == 2

    async def test_a_bsuid_only_stranger_gets_no_reply(self, db: Database, owner_conn: Any) -> None:
        inbox_id = await _inbox(owner_conn, phone=None, user_id="US.1")
        channel = FakeOutbound()
        spy = Spy()
        assert await _run(db, inbox_id, channel, on_allowed=spy) == "stranger"
        assert channel.attempts == []
        assert spy.calls == []
        assert (await _row(owner_conn, inbox_id))["body"] is None

    async def test_a_stale_strangers_content_is_purged_too(
        self, db: Database, owner_conn: Any
    ) -> None:
        sent_at = datetime.now(UTC) - timedelta(days=3)
        inbox_id = await _inbox(owner_conn, phone=STRANGER_PHONE, sent_at=sent_at)
        assert await _run(db, inbox_id, FakeOutbound()) == "stale"
        assert (await _row(owner_conn, inbox_id))["body"] is None


class TestSeveralChannels:
    """Meta and the gateway live side by side: each message is answered where it arrived."""

    @staticmethod
    async def _handle(db: Database, inbox_id: UUID, *channels: FakeOutbound) -> str | None:
        return await handle_inbound(
            inbox_id,
            db=db,
            channels={channel.name: channel for channel in channels}.__getitem__,
            allowlist=ALLOWLIST,
            on_allowed=Spy(),
        )

    async def test_each_reply_goes_out_on_its_own_channel(
        self, db: Database, owner_conn: Any
    ) -> None:
        meta, gowa = FakeOutbound(name="whatsapp"), FakeOutbound(name="gowa")
        via_meta = await _inbox(owner_conn, channel="whatsapp")
        via_gowa = await _inbox(owner_conn, channel="gowa")

        assert await self._handle(db, via_meta, meta, gowa) == "allowed"
        assert await self._handle(db, via_gowa, meta, gowa) == "allowed"

        assert meta.sent == [OutboundMessage(recipient=SENDER_PHONE, body=ACK_TEXT)]
        assert gowa.sent == [OutboundMessage(recipient=SENDER_PHONE, body=ACK_TEXT)]
        channels = await owner_conn.fetch("SELECT channel FROM channel_outbox ORDER BY channel")
        assert [row["channel"] for row in channels] == ["gowa", "whatsapp"]

    async def test_a_stranger_is_told_once_across_channels(
        self, db: Database, owner_conn: Any
    ) -> None:
        meta, gowa = FakeOutbound(name="whatsapp"), FakeOutbound(name="gowa")
        first = await _inbox(owner_conn, phone=STRANGER_PHONE, channel="whatsapp")
        second = await _inbox(owner_conn, phone=STRANGER_PHONE, channel="gowa")

        assert await self._handle(db, first, meta, gowa) == "stranger"
        assert await self._handle(db, second, meta, gowa) == "stranger_muted"

        assert len(meta.sent) == 1
        assert gowa.sent == []

    async def test_a_channel_this_worker_lacks_fails_before_sending(
        self, db: Database, owner_conn: Any
    ) -> None:
        meta = FakeOutbound(name="whatsapp")
        inbox_id = await _inbox(owner_conn, channel="gowa")

        with pytest.raises(KeyError):
            await self._handle(db, inbox_id, meta)

        assert meta.attempts == []
        assert await _outbox(owner_conn) == []
        assert (await _row(owner_conn, inbox_id))["processed_at"] is None


class TestEdges:
    async def test_a_missing_row_is_a_no_op(self, db: Database) -> None:
        assert await _run(db, uuid4(), FakeOutbound()) is None

    async def test_send_once_directly_is_idempotent_per_kind(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id = await _inbox(owner_conn)
        channel = FakeOutbound()
        for _ in range(3):
            await send_once(
                db,
                channel,
                inbox_id=inbox_id,
                kind="ack",
                recipient_key=f"tel:{SENDER_PHONE}",
                to=SENDER_PHONE,
                text=ACK_TEXT,
            )
        assert len(channel.sent) == 1


class TestLogs:
    async def test_the_worker_logs_neither_who_nor_what(
        self, db: Database, owner_conn: Any, settings: Settings
    ) -> None:
        """Through the real logging chain, for both an allowlisted user and a stranger."""
        stream = io.StringIO()
        configure_logging(settings, stream=stream)
        channel = FakeOutbound()
        await _run(db, await _inbox(owner_conn), channel)
        await _run(db, await _inbox(owner_conn, phone=STRANGER_PHONE), channel)

        captured = stream.getvalue()
        assert "inbound.handled" in captured
        assert "sender_hash" in captured
        for leaked in (SENDER_PHONE, STRANGER_PHONE, "Oncology appointment", "31612345678"):
            assert leaked not in captured
