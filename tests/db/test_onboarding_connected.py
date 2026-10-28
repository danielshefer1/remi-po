"""``onboarding:connected``: "You're all set", once, on the tenant's latest channel.

The message answers no inbound row, so D6's key, not ``(inbox_id, kind)``, makes it
at-most-once. It goes to the channel the tenant last wrote on (D10).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from personal_organizer.core.errors import (
    ChannelNotConfiguredError,
    TransientChannelError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.onboarding import connected
from personal_organizer.onboarding.connected import (
    ALL_SET_KIND,
    all_set_key,
    announce_connected,
)
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.db.test_handle_inbound import FakeOutbound
from tests.fixtures.connect import (
    PHONE,
    TRUNCATE_CONNECT_TABLES,
    make_active,
    seed_inbound,
    seed_tenant,
)

pytestmark = [pytest.mark.db]


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


def resolver(*channels: FakeOutbound) -> Callable[[str], OutboundChannel]:
    """What the worker's registry does: a configured channel, or ChannelNotConfiguredError."""
    by_name = {channel.name: channel for channel in channels}

    def resolve(name: str) -> OutboundChannel:
        try:
            return by_name[name]
        except KeyError:
            raise ChannelNotConfiguredError(name) from None

    return resolve


async def active_tenant(db: Database, **kwargs: Any) -> UUID:
    tenant_id = await seed_tenant(db, **kwargs)
    await make_active(db, tenant_id)
    return tenant_id


async def outbox(owner_conn: Any) -> list[dict[str, Any]]:
    rows = await owner_conn.fetch(
        "SELECT kind, inbox_id, idempotency_key, status, channel FROM channel_outbox"
    )
    return [dict(row) for row in rows]


class TestAnnounceConnected:
    async def test_it_goes_out_on_the_latest_channel_to_the_tenants_phone(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await active_tenant(db, language="he")
        now = datetime.now(UTC)
        await seed_inbound(db, tenant_id, channel="whatsapp", sent_at=now - timedelta(minutes=2))
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=now - timedelta(minutes=1))
        meta, gowa = FakeOutbound(name="whatsapp"), FakeOutbound(name="gowa")
        connection_id = uuid4()

        status = await announce_connected(
            tenant_id, connection_id, db=db, channels=resolver(meta, gowa)
        )

        assert status == "accepted"
        assert meta.sent == []
        [message] = gowa.sent
        # No Google provider here, so nothing was imported: the events are still to come.
        assert (message.recipient, message.body) == (PHONE, t("all_set_pending", "he"))
        assert await outbox(owner_conn) == [
            {
                "kind": ALL_SET_KIND,
                "inbox_id": None,
                "idempotency_key": all_set_key(connection_id),
                "status": "accepted",
                "channel": "gowa",
            }
        ]

    async def test_a_second_run_sends_nothing(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(name="gowa")
        connection_id = uuid4()
        for _ in range(2):
            await announce_connected(tenant_id, connection_id, db=db, channels=resolver(gowa))
        assert len(gowa.sent) == 1

    async def test_a_transient_failure_is_sent_by_the_retry(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(TransientChannelError("gateway down"), name="gowa")
        connection_id = uuid4()
        with pytest.raises(TransientChannelError):
            await announce_connected(tenant_id, connection_id, db=db, channels=resolver(gowa))
        status = await announce_connected(tenant_id, connection_id, db=db, channels=resolver(gowa))
        assert status == "accepted"
        assert len(gowa.sent) == 1

    async def test_no_inbound_message_means_no_send(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        gowa = FakeOutbound(name="gowa")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(gowa)) is None
        assert gowa.attempts == []

    async def test_no_phone_means_no_send(self, db: Database) -> None:
        tenant_id = await active_tenant(db, phone=None, external_id="uid:BSUID.1")
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(name="gowa")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(gowa)) is None
        assert gowa.attempts == []

    async def test_a_tenant_not_active_is_not_told(self, db: Database) -> None:
        tenant_id = await seed_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(name="gowa")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(gowa)) is None
        assert gowa.attempts == []

    async def test_a_channel_the_worker_lacks_is_logged_not_retried(self, db: Database) -> None:
        """GOWA switched off after the tenant last wrote there: finish, don't crash-loop."""
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        meta = FakeOutbound(name="whatsapp")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(meta)) is None
        assert meta.attempts == []

    async def test_an_import_says_the_events_are_in(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_1", auth_config_id="ac"
            )
        gowa = FakeOutbound(name="gowa")

        await announce_connected(
            tenant_id, uuid4(), db=db, channels=resolver(gowa), calendar=InMemoryCalendar()
        )

        assert [m.body for m in gowa.sent] == [t("all_set", "he")]

    async def test_import_failure_says_the_events_are_coming(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        google = InMemoryCalendar()

        async def boom(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("import blew up")

        monkeypatch.setattr(connected, "sync_calendar", boom)
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_1", auth_config_id="ac"
            )
        gowa = FakeOutbound(name="gowa")

        status = await announce_connected(
            tenant_id, uuid4(), db=db, channels=resolver(gowa), calendar=google
        )

        assert status == "accepted"
        assert [m.body for m in gowa.sent] == [t("all_set_pending", "he")]
