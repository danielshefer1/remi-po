"""The gate with DB invites: an open invite onboards, a revoked one does not, and the env
list still works with an empty table (ADR 0006)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.invites import create_invite, revoke_invite
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.replies import INVITE_ONLY_TEXT
from personal_organizer.messaging.tenancy import enrol
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import ComposioSettings, Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.tenants import IL_PHONE, tenant_by_phone

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _invite(db: Database, phone: str = IL_PHONE) -> None:
    async with db.system_session() as session:
        assert await create_invite(session, phone)


async def _invite_state(conn: Any, phone: str = IL_PHONE) -> list[tuple[bool, bool]]:
    """``(used, revoked)`` for every invite of ``phone``, oldest first."""
    rows = await conn.fetch(
        "SELECT used_at IS NOT NULL AS used, revoked_at IS NOT NULL AS revoked "
        "FROM invites WHERE phone = $1 ORDER BY created_at",
        phone,
    )
    return [(row["used"], row["revoked"]) for row in rows]


async def _say(
    db: Database,
    conn: Any,
    settings: Settings,
    *,
    phone: str = IL_PHONE,
    allowlist: frozenset[str] = frozenset(),
    channel: str = "gowa",
    user_id: str | None = None,
    sent_at: datetime | None = None,
) -> tuple[str | None, FakeOutbound, UUID]:
    outbound = FakeOutbound(name=channel)
    inbox_id = await insert_inbox(
        conn, phone=phone, user_id=user_id, body="hello", channel=channel, sent_at=sent_at
    )
    disposition = await handle_inbound(
        inbox_id,
        db=db,
        channels={channel: outbound}.__getitem__,
        allowlist=allowlist,
        on_allowed=Spy(),
        settings=settings,
    )
    return disposition, outbound, inbox_id


class TestTenantGate:
    async def test_an_open_invite_onboards_and_is_used(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _invite(db)
        disposition, _, _ = await _say(db, owner_conn, onboarding_settings)
        assert disposition == "onboarding"
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        # An Israeli number's zone is guessed, which is all setup needs: the calendar is optional.
        assert tenant.status == "active"
        assert await _invite_state(owner_conn) == [(True, False)]

    async def test_a_revoked_invite_is_turned_away(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _invite(db)
        async with db.system_session() as session:
            await revoke_invite(session, IL_PHONE)
        disposition, outbound, inbox_id = await _say(db, owner_conn, onboarding_settings)
        assert disposition == "stranger"
        assert [m.body for m in outbound.sent] == [INVITE_ONLY_TEXT]
        assert await tenant_by_phone(db, IL_PHONE) is None
        assert (
            await owner_conn.fetchval("SELECT body FROM channel_inbox WHERE id = $1", inbox_id)
            is None
        )

    async def test_the_env_list_still_invites_with_an_empty_table(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """The fallback: the owner's own number is never locked out by the table."""
        disposition, _, _ = await _say(
            db, owner_conn, onboarding_settings, allowlist=frozenset({IL_PHONE})
        )
        assert disposition == "onboarding"
        assert await _invite_state(owner_conn) == []

    async def test_an_env_listed_number_uses_its_open_invite_too(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _invite(db)
        await _say(db, owner_conn, onboarding_settings, allowlist=frozenset({IL_PHONE}))
        assert await _invite_state(owner_conn) == [(True, False)]

    async def test_an_invite_works_on_either_channel(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 5: first message through Meta, carrying a BSUID and the phone."""
        await _invite(db)
        disposition, _, _ = await _say(
            db, owner_conn, onboarding_settings, channel="whatsapp", user_id="US.42"
        )
        assert disposition == "onboarding"
        assert await tenant_by_phone(db, IL_PHONE) is not None
        assert await _invite_state(owner_conn) == [(True, False)]

    async def test_a_stale_first_message_leaves_the_invite_open(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 4: redelivered after an outage, so no tenant -- and their next
        message must still find the invite."""
        await _invite(db)
        stale = datetime.now(UTC) - timedelta(hours=30)
        disposition, _, _ = await _say(db, owner_conn, onboarding_settings, sent_at=stale)
        assert disposition == "stale"
        assert await tenant_by_phone(db, IL_PHONE) is None
        assert await _invite_state(owner_conn) == [(False, False)]

        disposition, _, _ = await _say(db, owner_conn, onboarding_settings)
        assert disposition == "onboarding"


class TestComposioOff:
    async def test_a_db_invite_is_allowed_and_stays_open(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        """Iteration 02's gate creates no tenant, so nothing is used."""
        off = db_settings.model_copy(update={"composio": ComposioSettings()})
        await _invite(db)
        disposition, _, _ = await _say(db, owner_conn, off)
        assert disposition == "allowed"
        assert await _invite_state(owner_conn) == [(False, False)]

    async def test_without_an_invite_it_is_a_stranger(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        off = db_settings.model_copy(update={"composio": ComposioSettings()})
        disposition, _, _ = await _say(db, owner_conn, off)
        assert disposition == "stranger"


class TestEnrol:
    async def test_racing_first_messages_use_the_invite_once(
        self, db: Database, owner_conn: Any
    ) -> None:
        """One person, both channels at once: one tenant, one ``invite.used``."""
        for _ in range(5):
            await _invite(db)
            gowa = await load_row(db, await insert_inbox(owner_conn, phone=IL_PHONE))
            meta = await load_row(
                db,
                await insert_inbox(owner_conn, phone=IL_PHONE, user_id="US.7", channel="whatsapp"),
            )
            assert gowa is not None
            assert meta is not None
            with capture_logs() as logs:
                ids = await asyncio.gather(
                    enrol(db, gowa, language="en"), enrol(db, meta, language="en")
                )
            assert ids[0] == ids[1]
            assert await _invite_state(owner_conn) == [(True, False)]
            used = [entry for entry in logs if entry["event"] == "invite.used"]
            assert len(used) == 1
            assert set(used[0]) <= {"event", "log_level", "tenant_id"}
            await owner_conn.execute("TRUNCATE tenants, invites CASCADE")

    async def test_invite_used_is_logged_without_the_number(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        stream = io.StringIO()
        configure_logging(onboarding_settings, stream=stream)
        await _invite(db)
        await _say(db, owner_conn, onboarding_settings)
        captured = stream.getvalue()
        assert "invite.used" in captured
        for leaked in (IL_PHONE, IL_PHONE.removeprefix("+")):
            assert leaked not in captured
