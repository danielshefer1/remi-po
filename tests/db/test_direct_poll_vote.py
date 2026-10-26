"""A poll vote in a private chat (Iteration 07): Remi posts polls only in groups, so it answers
nothing Remi asked. Seen on staging 2026-10-26, when the agent replied to such a vote."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from personal_organizer.db.engine import Database
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.tenants import IL_PHONE, messages_of, new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def test_a_private_vote_gets_no_reply_and_no_model_call(
    db: Database, owner_conn: Any, onboarding_settings: Settings
) -> None:
    tenant_id = await new_tenant(db, phone=IL_PHONE, status="active", step=None)
    inbox_id = await insert_inbox(
        owner_conn, phone=IL_PHONE, body="Can't make it", message_type="poll_vote", sent_at=NOW
    )
    channel, spy = FakeOutbound(name="gowa"), Spy()

    disposition = await handle_inbound(
        inbox_id,
        db=db,
        channels={channel.name: channel}.__getitem__,
        allowlist=frozenset(),
        on_allowed=spy,
        settings=onboarding_settings,
        now=lambda: NOW,
    )

    assert disposition == "allowed"
    assert spy.calls == []
    assert channel.attempts == []
    assert [(m.direction, m.message_type) for m in await messages_of(db, tenant_id)] == [
        ("in", "poll_vote")
    ]
