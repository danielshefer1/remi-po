"""The pending action behind a confirm (Iteration 04, D5), against Postgres as ``app_user``.

What ``agent_turn`` relies on: a claim hands the action back exactly once, however many
claims race for it, and never once it is replaced, dropped or expired; and another tenant
can neither see nor take it, with or without the app-level filter.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories import pending, tenants

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
PAYLOAD = {"title": "Dinner with Noa", "start": "2026-10-08T20:00", "end": "2026-10-08T21:00"}


async def _create(database: Database, phone: str = "+972501234567") -> UUID:
    async with database.system_session() as session:
        return await tenants.create_tenant(
            session,
            network=NETWORK_WHATSAPP,
            external_id=f"tel:{phone}",
            phone=phone,
            language="he",
        )


async def _propose(database: Database, tenant_id: UUID, **kwargs: Any) -> pending.PendingAction:
    async with database.tenant_session(TenantId(tenant_id)) as session:
        return await pending.propose(
            session, tenant_id, kind="create_event", payload=PAYLOAD, now=NOW, **kwargs
        )


async def _claim(
    database: Database, tenant_id: UUID, action_id: str, *, now: datetime = NOW
) -> pending.PendingAction | None:
    async with database.tenant_session(TenantId(tenant_id)) as session:
        return await pending.claim(session, tenant_id, action_id, now=now)


async def _current(
    database: Database, tenant_id: UUID, *, now: datetime = NOW
) -> pending.PendingAction | None:
    async with database.tenant_session(TenantId(tenant_id)) as session:
        return await pending.current(session, tenant_id, now=now)


async def _is_sql_null(database: Database, tenant_id: UUID) -> bool:
    """``NULL``, not the JSON ``null`` a plain ``None`` would store."""
    async with database.tenant_session(TenantId(tenant_id)) as session:
        result: bool = await session.scalar(
            text("SELECT pending_action IS NULL FROM tenants WHERE id = :t"), {"t": tenant_id}
        )
        return result


async def test_propose_stores_it_until_it_expires(database: Database) -> None:
    tenant_id = await _create(database)
    action = await _propose(database, tenant_id)
    assert action.kind == "create_event"
    assert action.expires_at == NOW + timedelta(hours=1)
    assert await _current(database, tenant_id) == action
    assert await _current(database, tenant_id, now=action.expires_at) is None


async def test_a_new_proposal_replaces_the_old_one(database: Database) -> None:
    tenant_id = await _create(database)
    old = await _propose(database, tenant_id)
    new = await _propose(database, tenant_id)
    assert new.id != old.id
    assert await _current(database, tenant_id) == new
    assert await _claim(database, tenant_id, old.id) is None
    assert await _claim(database, tenant_id, new.id) == new


async def test_a_claim_returns_the_action_once(database: Database) -> None:
    """The returned value is the row *before* the update -- the update itself nulls it."""
    tenant_id = await _create(database)
    action = await _propose(database, tenant_id)
    claimed = await _claim(database, tenant_id, action.id)
    assert claimed == action
    assert claimed is not None
    assert claimed.payload == PAYLOAD
    assert await _is_sql_null(database, tenant_id)
    assert await _claim(database, tenant_id, action.id) is None


async def test_a_claim_waiting_on_another_gets_nothing(database: Database) -> None:
    """Two "1"s at once: the second waits on the first's row lock, then finds it cleared."""
    tenant_id = await _create(database)
    action = await _propose(database, tenant_id)
    async with database.tenant_session(TenantId(tenant_id)) as first:
        won = await pending.claim(first, tenant_id, action.id, now=NOW)
        second = asyncio.create_task(_claim(database, tenant_id, action.id))
        await asyncio.sleep(0.3)
        assert not second.done(), "the second claim should be waiting on the row lock"
    assert won == action
    assert await second is None


async def test_concurrent_claims_have_one_winner(database: Database) -> None:
    tenant_id = await _create(database)
    action = await _propose(database, tenant_id)
    results = await asyncio.gather(*(_claim(database, tenant_id, action.id) for _ in range(4)))
    assert sorted(result is not None for result in results) == [False, False, False, True]


async def test_an_expired_action_cannot_be_claimed(database: Database) -> None:
    tenant_id = await _create(database)
    action = await _propose(database, tenant_id, ttl=timedelta(minutes=5))
    assert await _claim(database, tenant_id, action.id, now=NOW + timedelta(minutes=5)) is None
    assert await _claim(database, tenant_id, action.id, now=NOW + timedelta(minutes=4)) == action


async def test_drop(database: Database) -> None:
    tenant_id = await _create(database)
    action = await _propose(database, tenant_id)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await pending.drop(session, tenant_id)
        await pending.drop(session, tenant_id)  # nothing pending: still fine
    assert await _current(database, tenant_id) is None
    assert await _is_sql_null(database, tenant_id)
    assert await _claim(database, tenant_id, action.id) is None


@pytest.mark.parametrize("mode", ["app-filter", "rls-only"])
async def test_another_tenant_can_neither_read_nor_claim_it(
    database: Database, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """As in ``test_isolation.py``, the ``rls-only`` run switches ``scoped`` off, so it is
    RLS alone that keeps B's action from A."""
    a, b = await _create(database, "+972500000001"), await _create(database, "+972500000002")
    b_action = await _propose(database, b)
    if mode == "rls-only":
        monkeypatch.setattr(pending, "scoped", lambda stmt, model, tenant_id: stmt)
    async with database.tenant_session(TenantId(a)) as session:
        assert await pending.current(session, b, now=NOW) is None
        assert await pending.claim(session, b, b_action.id, now=NOW) is None
        await pending.drop(session, b)
        await pending.propose(session, b, kind="create_event", payload={}, now=NOW)
    monkeypatch.undo()
    assert await _current(database, b) == b_action
