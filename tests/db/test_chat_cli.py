"""``po-chat`` on an active tenant: which calendar and connection the agent is given."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from personal_organizer.admin import chat
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.messaging.tenancy import load_state
from personal_organizer.providers.calendar.composio import ComposioConnector
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import ComposioSettings, Settings
from tests.fixtures.llm import NoLLM
from tests.fixtures.tenants import new_tenant

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def given(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What ``agent_chat`` was handed; it returns at once."""
    seen: dict[str, Any] = {}

    async def agent_chat(tenant: Any, **kwargs: Any) -> int:
        seen.update(kwargs, tenant=tenant)
        return chat.OK

    monkeypatch.setattr(chat, "agent_chat", agent_chat)
    return seen


async def _active(db: Database, *, connected: bool) -> Any:
    tenant_id = await new_tenant(db, status="active", step=None)
    if connected:
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_bound", auth_config_id="ac_test"
            )
    return await load_state(db, tenant_id)


async def test_the_bound_connection_and_composio(
    db: Database, onboarding_settings: Settings, given: dict[str, Any]
) -> None:
    tenant = await _active(db, connected=True)
    assert await chat._agent(db, onboarding_settings, tenant, llm=NoLLM(), fake=False) == chat.OK
    assert isinstance(given["calendar"], ComposioConnector)
    assert given["account"].connected_account_id == "ca_bound"


async def test_no_composio_key_is_refused(
    db: Database, onboarding_settings: Settings, given: dict[str, Any]
) -> None:
    tenant = await _active(db, connected=True)
    keyless = onboarding_settings.model_copy(update={"composio": ComposioSettings()})
    assert await chat._agent(db, keyless, tenant, llm=NoLLM(), fake=False) == chat.REFUSED
    assert given == {}


@pytest.mark.parametrize("connected", [True, False])
async def test_the_fake_calendar_answers_with_or_without_a_connection(
    db: Database, onboarding_settings: Settings, given: dict[str, Any], connected: bool
) -> None:
    tenant = await _active(db, connected=connected)
    assert await chat._agent(db, onboarding_settings, tenant, llm=NoLLM(), fake=True) == chat.OK
    assert isinstance(given["calendar"], InMemoryCalendar)
    assert given["calendar"].events
    assert given["account"].connected_account_id == ("ca_bound" if connected else "fake")
