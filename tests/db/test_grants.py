"""Letting someone see your event details (Iteration 07), against Postgres as ``app_user``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import groups
from personal_organizer.messaging.groups.grants import TenantGrants, resolve_peer
from personal_organizer.settings import Settings
from tests.fixtures.connect import make_active, seed_tenant

pytestmark = [pytest.mark.db]

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
_TRUNCATE = "TRUNCATE tenants, wa_groups, channel_outbox, channel_inbox, procrastinate_jobs CASCADE"


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(_TRUNCATE)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(_TRUNCATE)


async def _person(db: Database, phone: str, name: str | None, *groups_in: UUID) -> UUID:
    tenant_id = await seed_tenant(db, phone=phone)
    await make_active(db, tenant_id)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        if name is not None:
            await groups.set_display_name(session, tenant_id, name)
        for group in groups_in:
            await groups.touch_member(session, tenant_id, group, NOW)
    return tenant_id


async def _group(db: Database, n: int) -> UUID:
    async with db.system_session() as session:
        return (await groups.ensure_group(session, channel="gowa", jid=f"{n}@g.us")).id


async def _family(db: Database) -> tuple[UUID, UUID, UUID, UUID]:
    """A and B share a group; C is alone in another; D shares one with A and is also "Rina"."""
    g1, g2, g3 = await _group(db, 1), await _group(db, 2), await _group(db, 3)
    a = await _person(db, "+972500000001", "Dana", g1, g3)
    b = await _person(db, "+972500000002", "Rina Cohen", g1)
    c = await _person(db, "+972500000003", "Yossi", g2)
    d = await _person(db, "+972500000004", "Rina Levi", g3)
    return a, b, c, d


async def test_resolve_peer_by_first_name(db: Database) -> None:
    a, b, _, _ = await _family(db)
    assert await resolve_peer(db, a, " rina cohen ") == (b, "Rina Cohen")
    surname = await resolve_peer(db, a, "Cohen")  # first word only, not any word
    assert isinstance(surname, list)
    assert sorted(surname) == ["Rina Cohen", "Rina Levi"]


async def test_resolve_peer_by_unique_first_word(db: Database) -> None:
    a, _, _, _ = await _family(db)
    g = await _group(db, 4)
    e = await _person(db, "+972500000005", "Yossi Levi", g)
    async with db.tenant_session(TenantId(a)) as session:
        await groups.touch_member(session, a, g, NOW)
    assert await resolve_peer(db, a, "yossi") == (e, "Yossi Levi")


async def test_resolve_peer_ambiguous_lists_names(db: Database) -> None:
    a, _, _, _ = await _family(db)
    found = await resolve_peer(db, a, "rina")
    assert isinstance(found, list)
    assert sorted(found) == ["Rina Cohen", "Rina Levi"]


async def test_resolve_peer_never_outside_shared_groups(db: Database) -> None:
    a, _, _, _ = await _family(db)
    found = await resolve_peer(db, a, "Yossi")
    assert isinstance(found, list)
    assert "Yossi" not in found


async def test_let_see_details_then_stop(db: Database) -> None:
    a, b, _, _ = await _family(db)
    grants = TenantGrants(db, a)
    assert await grants.let_see("Rina Cohen") == "ok: Rina Cohen can now see your event details"
    async with db.tenant_session(TenantId(a)) as session:
        assert await groups.grant(session, a, b) is False  # already there
    assert await grants.stop("rina cohen") == "ok: Rina Cohen can no longer see your event details"
    assert await grants.stop("Rina Cohen") == (
        "ok: Rina Cohen couldn't see your event details anyway"
    )
    assert (await grants.let_see("Yossi")).startswith("error: who do they mean?")
