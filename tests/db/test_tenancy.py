"""Resolving a sender to a tenant through network-keyed identities (D12)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import resolve_tenant
from personal_organizer.messaging.inbox import InboxRow, load_row
from personal_organizer.messaging.tenancy import (
    TenantState,
    enrol,
    load_state,
    phone_of,
    resolve_sender,
)
from personal_organizer.settings import Settings
from tests.fixtures.channels import insert_inbox
from tests.fixtures.tenants import NL_PHONE

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _row(
    db: Database,
    conn: Any,
    *,
    user_id: str | None = None,
    phone: str | None = NL_PHONE,
    channel: str = "gowa",
) -> InboxRow:
    row = await load_row(
        db, await insert_inbox(conn, user_id=user_id, phone=phone, channel=channel)
    )
    assert row is not None
    return row


async def _resolve(db: Database, key: str) -> Any:
    async with db.system_session() as session:
        return await resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=key)


async def _tenant_and_identity_counts(conn: Any) -> tuple[int, int]:
    """Counted past RLS, as ``app_definer`` (as PR 2's concurrent create test does)."""
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE app_definer")
        return (
            await conn.fetchval("SELECT count(*) FROM tenants"),
            await conn.fetchval("SELECT count(*) FROM tenant_identities"),
        )


async def _wipe(conn: Any) -> None:
    await conn.execute("TRUNCATE tenants CASCADE")


class TestIdentities:
    async def test_an_unknown_sender_resolves_to_nothing(
        self, db: Database, owner_conn: Any
    ) -> None:
        assert await resolve_sender(db, await _row(db, owner_conn)) is None

    async def test_enrolled_by_phone_resolves_on_either_channel(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn, channel="gowa"), language="en")
        via_meta = await _row(db, owner_conn, channel="whatsapp")
        assert await resolve_sender(db, via_meta) == tenant_id

    async def test_enrolled_with_a_bsuid_and_a_phone_records_both(
        self, db: Database, owner_conn: Any
    ) -> None:
        row = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        tenant_id = await enrol(db, row, language="en")
        assert await _resolve(db, "uid:US.1") == tenant_id
        assert await _resolve(db, f"tel:{NL_PHONE}") == tenant_id

    async def test_a_bsuid_for_a_phone_keyed_tenant_is_added_not_a_second_tenant(
        self, db: Database, owner_conn: Any
    ) -> None:
        """Review Focus 3: first seen on the gateway by number, later on Meta with a BSUID."""
        tenant_id = await enrol(db, await _row(db, owner_conn), language="en")
        with_bsuid = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await resolve_sender(db, with_bsuid) == tenant_id
        assert await _resolve(db, "uid:US.1") == tenant_id
        hidden_number = await _row(db, owner_conn, user_id="US.1", phone=None, channel="whatsapp")
        assert await resolve_sender(db, hidden_number) == tenant_id

    async def test_enrol_is_idempotent(self, db: Database, owner_conn: Any) -> None:
        row = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await enrol(db, row, language="en") == await enrol(db, row, language="he")

    async def test_a_meta_message_for_a_gateway_tenant_links_the_uid(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn, channel="gowa"), language="en")
        meta = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await resolve_sender(db, meta) == tenant_id
        assert await _resolve(db, "uid:US.1") == tenant_id
        assert await _tenant_and_identity_counts(owner_conn) == (1, 2)

    async def test_a_uid_tenant_writing_from_a_new_phone_gets_the_tel_linked(
        self, db: Database, owner_conn: Any
    ) -> None:
        first = await _row(db, owner_conn, user_id="US.1", phone=None, channel="whatsapp")
        tenant_id = await enrol(db, first, language="en")
        assert await _resolve(db, f"tel:{NL_PHONE}") is None
        with_phone = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await resolve_sender(db, with_phone) == tenant_id
        assert await _resolve(db, f"tel:{NL_PHONE}") == tenant_id

    async def test_first_messages_on_both_channels_at_once_make_one_tenant(
        self, db: Database, owner_conn: Any
    ) -> None:
        """Jobs for ``tel:`` and ``uid:`` keys are serialized apart, so they can race."""
        gowa = await _row(db, owner_conn, channel="gowa")
        meta = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")

        async def first_message(row: InboxRow) -> UUID:
            found = await resolve_sender(db, row)
            return found if found is not None else await enrol(db, row, language="en")

        for _ in range(5):
            ids = await asyncio.gather(first_message(gowa), first_message(meta))
            assert ids[0] == ids[1]
            assert await _tenant_and_identity_counts(owner_conn) == (1, 2)
            await _wipe(owner_conn)

    async def test_a_key_owned_by_another_tenant_is_logged_without_identifiers(
        self, db: Database, owner_conn: Any
    ) -> None:
        a = await enrol(db, await _row(db, owner_conn, channel="gowa"), language="en")
        b = await enrol(
            db,
            await _row(db, owner_conn, user_id="US.9", phone=None, channel="whatsapp"),
            language="en",
        )
        assert a != b
        clash = await _row(db, owner_conn, user_id="US.9", channel="whatsapp")
        with capture_logs() as logs:
            assert await resolve_sender(db, clash) == b
        conflicts = [entry for entry in logs if entry["event"] == "tenant.identity_conflict"]
        assert len(conflicts) == 1
        assert set(conflicts[0]) <= {"event", "log_level", "tenant_id", "channel"}
        assert NL_PHONE not in str(logs)
        assert "US.9" not in str(logs)
        assert await _resolve(db, f"tel:{NL_PHONE}") == a


class TestState:
    async def test_a_new_tenant_is_onboarding_its_first_message(
        self, db: Database, owner_conn: Any
    ) -> None:
        row = await _row(db, owner_conn)
        tenant_id = await enrol(db, row, language="he")
        assert await load_state(db, tenant_id) == TenantState(
            id=tenant_id,
            status="onboarding",
            step="zone",
            language="he",
            first_message=True,
            briefing_at=time(7, 30),
        )
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await record_inbound(
                session,
                tenant_id,
                inbox_id=row.id,
                channel=row.channel,
                message_type=row.message_type,
                body=row.body,
                sent_at=row.sent_at,
            )
        state = await load_state(db, tenant_id)
        assert state is not None
        assert state.first_message is False

    async def test_an_unknown_tenant_has_no_state(self, db: Database) -> None:
        assert await load_state(db, uuid4()) is None

    async def test_phone_of(self, db: Database, owner_conn: Any) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn), language="en")
        assert await phone_of(db, tenant_id) == NL_PHONE
