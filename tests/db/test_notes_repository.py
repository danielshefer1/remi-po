"""Notes (Iteration 06): add, the duplicate no-op, the cap, the order, remove."""

from __future__ import annotations

from typing import Any

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import notes
from tests.fixtures.tenants import IL_PHONE, new_tenant

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]


async def _tenant(database: Database) -> Any:
    return await new_tenant(database, phone=IL_PHONE, language="en", status="active", step=None)


async def test_add_list_remove(database: Database) -> None:
    tenant_id = await _tenant(database)
    # Separate transactions, as TenantNotes makes them: now() is per transaction.
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await notes.add_note(session, tenant_id, "Wife: Dana", limit=60) == "added"
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await notes.add_note(session, tenant_id, "Diet: vegetarian", limit=60) == "added"
    async with database.tenant_session(TenantId(tenant_id)) as session:
        listed = await notes.list_notes(session, tenant_id)
        assert [n.content for n in listed] == ["Diet: vegetarian", "Wife: Dana"]
        assert await notes.remove_note(session, tenant_id, listed[0].id)
        assert not await notes.remove_note(session, tenant_id, listed[0].id)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert [n.content for n in await notes.list_notes(session, tenant_id)] == ["Wife: Dana"]


async def test_the_same_note_twice_is_saved_once(database: Database) -> None:
    tenant_id = await _tenant(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await notes.add_note(session, tenant_id, "Wife: Dana", limit=60) == "added"
        assert await notes.add_note(session, tenant_id, "  wife: dana ", limit=60) == "exists"
        assert len(await notes.list_notes(session, tenant_id)) == 1


async def test_a_full_list_takes_no_more(database: Database) -> None:
    tenant_id = await _tenant(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await notes.add_note(session, tenant_id, "one", limit=2) == "added"
        assert await notes.add_note(session, tenant_id, "two", limit=2) == "added"
        assert await notes.add_note(session, tenant_id, "three", limit=2) == "full"
        assert await notes.add_note(session, tenant_id, "two", limit=2) == "exists"
