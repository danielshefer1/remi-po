"""Repository behaviour, against Postgres, as ``app_user`` under RLS.

What each function promises its callers in PRs 3 and 4: idempotent tenant creation, a
single-use link, a reconnect that leaves one active connection, and where a send that
answers nothing should go. Isolation between tenants is ``test_isolation.py``'s job.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import (
    NETWORK_WHATSAPP,
    CalendarConnection,
    TenantIdentity,
)
from personal_organizer.db.repositories import connections, links, messages, tenants

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]

PHONE = "+972501234567"
KEY = f"tel:{PHONE}"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


async def _create(database: Database, *, key: str = KEY, phone: str | None = PHONE) -> UUID:
    async with database.system_session() as session:
        return await tenants.create_tenant(
            session, network=NETWORK_WHATSAPP, external_id=key, phone=phone, language="he"
        )


async def _inbox(owner_conn: Any, *, channel: str = "gowa") -> UUID:
    inbox_id: UUID = await owner_conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_phone, "
        "message_type, sent_at) VALUES ($1, $2, $3, $4, 'text', now()) RETURNING id",
        channel,
        f"repo.{uuid4().hex}",
        KEY,
        PHONE,
    )
    return inbox_id


# --- tenants ------------------------------------------------------------------------------


async def test_create_tenant_starts_onboarding_at_the_zone_step(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        tenant = await tenants.get_tenant(session, tenant_id)
    assert tenant is not None
    assert (tenant.status, tenant.onboarding_step, tenant.language, tenant.timezone) == (
        "onboarding",
        "zone",
        "he",
        None,
    )


async def test_create_tenant_is_idempotent_on_the_identity(database: Database) -> None:
    first = await _create(database)
    second = await _create(database, phone="+15550000000")
    assert first == second
    async with database.tenant_session(TenantId(first)) as session:
        # The second call changed nothing, not even the phone it was given.
        assert await tenants.primary_phone(session, first) == PHONE


async def test_concurrent_create_tenant_leaves_one_tenant(
    database: Database, owner_conn: Any
) -> None:
    """Two messages from a new sender, handled by two workers at once."""
    ids = await asyncio.gather(*(_create(database) for _ in range(5)))
    assert len(set(ids)) == 1
    # A loser's tenant row would be an orphan no GUC can see, so count past RLS: app_owner
    # may become app_definer (bootstrap grants it for ALTER FUNCTION), which is BYPASSRLS.
    async with owner_conn.transaction():
        await owner_conn.execute("SET LOCAL ROLE app_definer")
        assert await owner_conn.fetchval("SELECT count(*) FROM tenants") == 1
        assert await owner_conn.fetchval("SELECT count(*) FROM tenant_identities") == 1


async def test_create_tenant_rejects_an_unknown_language(
    database: Database, owner_conn: Any
) -> None:
    """D11 detects ``he`` or ``en``; anything else is a caller bug, refused whole."""
    with pytest.raises(IntegrityError, match="ck_tenants_language"):
        async with database.system_session() as session:
            await tenants.create_tenant(
                session, network=NETWORK_WHATSAPP, external_id=KEY, phone=PHONE, language="fr"
            )
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY) is None
        )


async def test_resolve_tenant(database: Database) -> None:
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY) is None
        )
    tenant_id = await _create(database)
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY)
            == tenant_id
        )
        # Keyed by network (D12): the same key on another network is someone else.
        assert await tenants.resolve_tenant(session, network="telegram", external_id=KEY) is None


async def test_onboarding_state_updates(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await tenants.set_timezone(session, tenant_id, "Asia/Jerusalem")
        await tenants.set_onboarding_step(session, tenant_id, "connect")
        tenant = await tenants.get_tenant(session, tenant_id)
        assert tenant is not None
        await session.refresh(tenant)
        assert (tenant.timezone, tenant.onboarding_step, tenant.status) == (
            "Asia/Jerusalem",
            "connect",
            "onboarding",
        )
        assert tenant.updated_at >= tenant.created_at

        await tenants.activate(session, tenant_id)
        await session.refresh(tenant)
        assert (tenant.status, tenant.onboarding_step) == ("active", None)


async def test_primary_phone_is_the_earliest_identity_with_one(database: Database) -> None:
    tenant_id = await _create(database, key="uid:BSUID.1", phone=None)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await tenants.primary_phone(session, tenant_id) is None
        session.add(
            TenantIdentity(
                tenant_id=tenant_id, network=NETWORK_WHATSAPP, external_id=KEY, phone=PHONE
            )
        )
    # A separate transaction: created_at is now(), the transaction's start time.
    async with database.tenant_session(TenantId(tenant_id)) as session:
        session.add(
            TenantIdentity(
                tenant_id=tenant_id,
                network="telegram",
                external_id="tel:+15550000000",
                phone="+15550000000",
            )
        )
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await tenants.primary_phone(session, tenant_id) == PHONE


# --- links --------------------------------------------------------------------------------


async def test_a_spent_link_replays_only_within_the_window(database: Database) -> None:
    tenant_id = await _create(database)
    window = links.REPLAY_WINDOW
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="n-1", expires_at=NOW + timedelta(1))
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=NOW) is True
    async with database.tenant_session(TenantId(tenant_id)) as session:
        # A browser re-sending the POST, then a late replay: the window runs from first use.
        late = NOW + window - timedelta(seconds=1)
        assert await links.link_spendable(session, tenant_id, nonce="n-1", now=late) is True
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=late) is True
        assert (
            await links.link_spendable(session, tenant_id, nonce="n-1", now=NOW + window) is False
        )
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=NOW + window) is False
        assert await links.consume_link(session, tenant_id, nonce="n-other", now=NOW) is False


async def test_concurrent_consumes_all_win(database: Database) -> None:
    """Two POSTs of the same connect page at once: both get a redirect, neither deadlocks."""
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="n-1", expires_at=NOW + timedelta(1))

    async def consume(now: datetime) -> bool:
        async with database.tenant_session(TenantId(tenant_id)) as session:
            return await links.consume_link(session, tenant_id, nonce="n-1", now=now)

    moments = [NOW + timedelta(milliseconds=ms) for ms in (0, 100, 200, 300)]
    assert await asyncio.gather(*(consume(m) for m in moments)) == [True] * 4


async def test_an_expired_link_cannot_be_consumed(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="n-1", expires_at=NOW)
        # Expiry is exclusive: a link is dead at its expires_at.
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=NOW) is False


async def test_latest_usable_link(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await links.latest_usable_link(session, tenant_id, now=NOW) is None
        await links.create_link(session, tenant_id, nonce="old", expires_at=NOW + timedelta(1))
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="new", expires_at=NOW + timedelta(1))
        await links.create_link(session, tenant_id, nonce="dead", expires_at=NOW)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        link = await links.latest_usable_link(session, tenant_id, now=NOW)
        assert link is not None
        assert link.nonce == "new"
        assert await links.consume_link(session, tenant_id, nonce="new", now=NOW)
        link = await links.latest_usable_link(session, tenant_id, now=NOW)
        assert link is not None
        assert link.nonce == "old"


# --- connections --------------------------------------------------------------------------


async def test_bind_connection_revokes_the_previous_one(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        first = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
        )
        assert (first.status, first.composio_user_id) == ("active", str(tenant_id))
    async with database.tenant_session(TenantId(tenant_id)) as session:
        second = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_2", auth_config_id="ac_1"
        )
        previous = await session.get(CalendarConnection, first.id, populate_existing=True)
        assert previous is not None
        assert (previous.status, second.status) == ("revoked", "active")


async def test_bind_connection_is_idempotent_on_the_account(database: Database) -> None:
    """The callback delivered twice: one row, still active, nothing revoked."""
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        first = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
        )
    async with database.tenant_session(TenantId(tenant_id)) as session:
        again = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
        )
        assert (again.id, again.status) == (first.id, "active")


async def test_a_refused_bind_does_not_revoke_the_active_connection(database: Database) -> None:
    """The account belongs to tenant B: A's bind raises, and A's own connection survives it
    even when the caller catches the error and carries on in the same transaction."""
    tenant_a = await _create(database)
    tenant_b = await _create(database, key="tel:+972509999999", phone="+972509999999")
    async with database.tenant_session(TenantId(tenant_b)) as session:
        await connections.bind_connection(
            session, tenant_b, connected_account_id="ca_b", auth_config_id="ac_1"
        )
    async with database.tenant_session(TenantId(tenant_a)) as session:
        original = await connections.bind_connection(
            session, tenant_a, connected_account_id="ca_a", auth_config_id="ac_1"
        )
        original_id = original.id  # the savepoint's rollback expires loaded rows
        with pytest.raises(LookupError):
            await connections.bind_connection(
                session, tenant_a, connected_account_id="ca_b", auth_config_id="ac_1"
            )
        kept = await session.get(CalendarConnection, original_id, populate_existing=True)
        assert kept is not None
        assert kept.status == "active"


async def test_concurrent_binds_of_one_account_make_one_row(
    database: Database, owner_conn: Any
) -> None:
    tenant_id = await _create(database)

    async def bind() -> UUID:
        async with database.tenant_session(TenantId(tenant_id)) as session:
            row = await connections.bind_connection(
                session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
            )
            return row.id

    assert len(set(await asyncio.gather(bind(), bind()))) == 1
    async with owner_conn.transaction():
        await owner_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        assert await owner_conn.fetchval("SELECT count(*) FROM calendar_connections") == 1


# --- messages -----------------------------------------------------------------------------


async def test_latest_inbound_channel_follows_the_newest_message(
    database: Database, owner_conn: Any
) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await messages.latest_inbound_channel(session, tenant_id) is None
        for channel, minutes in (("whatsapp", 1), ("gowa", 2)):
            await messages.record_inbound(
                session,
                tenant_id,
                inbox_id=await _inbox(owner_conn, channel=channel),
                channel=channel,
                message_type="text",
                body="hello",
                sent_at=NOW + timedelta(minutes=minutes),
            )
        assert await messages.latest_inbound_channel(session, tenant_id) == "gowa"


async def test_record_inbound_survives_the_inbox_row(database: Database, owner_conn: Any) -> None:
    """``inbox_id`` is ``ON DELETE SET NULL``: the ledger may be pruned, content stays."""
    tenant_id = await _create(database)
    inbox_id = await _inbox(owner_conn)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        message_id = await messages.record_inbound(
            session,
            tenant_id,
            inbox_id=inbox_id,
            channel="gowa",
            message_type="text",
            body="hello",
            sent_at=NOW,
        )
    await owner_conn.execute("DELETE FROM channel_inbox WHERE id = $1", inbox_id)
    async with owner_conn.transaction():
        await owner_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        row = await owner_conn.fetchrow(
            "SELECT inbox_id, body, direction FROM messages WHERE id = $1", message_id
        )
    assert dict(row) == {"inbox_id": None, "body": "hello", "direction": "in"}
