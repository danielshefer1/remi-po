"""``/webhooks/gowa`` over the real router, ingress store, Procrastinate and Postgres.

The same Done-When as Meta's (tests/db/test_ingress_dedupe.py): a replayed webhook is one
row and one job. Plus what only arises with two channels: message ids are unique per
channel, not across them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import procrastinate
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from personal_organizer.interfaces.channel import SenderRef
from personal_organizer.messaging.ingress import sender_lock
from personal_organizer.settings import GowaSettings, Settings
from personal_organizer.worker.app import build_procrastinate_app
from tests.db.test_ingress_dedupe import _counts, _pepper, _webhook_app
from tests.fixtures import gowa_payloads as gowa
from tests.fixtures import payloads as meta

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables")]

URL = "/webhooks/gowa"


@pytest.fixture
def gowa_db_settings(whatsapp_db_settings: Settings) -> Settings:
    """Meta *and* the gateway enabled, as they may be in production."""
    return whatsapp_db_settings.model_copy(
        update={
            "gowa": GowaSettings(
                enabled=True,
                basic_auth_user="po",
                basic_auth_password=SecretStr("gateway-password"),
                webhook_secret=SecretStr(gowa.WEBHOOK_SECRET.decode()),
            )
        }
    )


@pytest.fixture
async def http(gowa_db_settings: Settings) -> AsyncIterator[AsyncClient]:
    queue: procrastinate.App = build_procrastinate_app(gowa_db_settings)
    async with queue.open_async():
        application = _webhook_app(gowa_db_settings, queue)
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


class TestReplayCreatesOneJob:
    async def test_ten_sequential_replays(self, http: AsyncClient, owner_conn: Any) -> None:
        body, headers = gowa.signed(gowa.text_message(message_id="3EB0REPLAY1"))
        for _ in range(10):
            assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        assert await _counts(owner_conn) == (1, 1)

    async def test_ten_concurrent_replays(self, http: AsyncClient, owner_conn: Any) -> None:
        body, headers = gowa.signed(gowa.text_message(message_id="3EB0REPLAY2"))
        responses = await asyncio.gather(
            *(http.post(URL, content=body, headers=headers) for _ in range(10))
        )
        assert [r.status_code for r in responses] == [200] * 10
        assert await _counts(owner_conn) == (1, 1)

    async def test_stored_normalised_under_the_gowa_channel(
        self, http: AsyncClient, owner_conn: Any, gowa_db_settings: Settings
    ) -> None:
        body, headers = gowa.signed(gowa.text_message(message_id="3EB0STORED", body="hello"))
        await http.post(URL, content=body, headers=headers)

        row = await owner_conn.fetchrow("SELECT * FROM channel_inbox")
        assert row["channel"] == "gowa"
        assert row["sender_phone"] == meta.SENDER_PHONE
        assert row["sender_key"] == f"tel:{meta.SENDER_PHONE}"
        assert row["body"] == "hello"
        lock = await owner_conn.fetchval("SELECT lock FROM procrastinate_jobs")
        assert lock == sender_lock(
            "gowa",
            SenderRef(user_id=None, phone=meta.SENDER_PHONE),
            _pepper(gowa_db_settings),
        )


class TestGroups:
    async def test_group_message_stored_with_group(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        body, headers = gowa.signed(gowa.load("group_text"))
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200

        row = await owner_conn.fetchrow("SELECT * FROM channel_inbox")
        assert row["group_id"] == "120363402106000001@g.us"
        assert row["sender_name"] == "Noa"
        assert row["sender_phone"] == "+972500000002"


class TestTwoChannels:
    async def test_one_id_on_both_channels_is_two_messages(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        """Provider ids are only unique within a provider."""
        body, headers = gowa.signed(gowa.text_message(message_id="SAME-ID"))
        await http.post(URL, content=body, headers=headers)
        body, headers = meta.signed(meta.text_message(wamid="SAME-ID"))
        await http.post("/webhooks/whatsapp", content=body, headers=headers)

        channels = await owner_conn.fetch("SELECT channel FROM channel_inbox ORDER BY channel")
        assert [row["channel"] for row in channels] == ["gowa", "whatsapp"]
        assert await _counts(owner_conn) == (2, 2)


class TestReceipts:
    async def test_a_delivery_receipt_moves_only_the_gowa_outbox_row(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        for channel in ("gowa", "whatsapp"):
            await owner_conn.execute(
                "INSERT INTO channel_outbox (channel, kind, recipient_key, status, "
                "provider_message_id) VALUES ($1, 'ack', 'tel:+31612345678', 'accepted', $2)",
                channel,
                "3EB0AAAAAAAAAAAAAAAA01",
            )
        body, headers = gowa.signed(gowa.load("ack_delivered"))
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200

        rows = await owner_conn.fetch("SELECT channel, status FROM channel_outbox ORDER BY channel")
        assert [tuple(row) for row in rows] == [("gowa", "delivered"), ("whatsapp", "accepted")]
        assert await _counts(owner_conn) == (0, 0)
