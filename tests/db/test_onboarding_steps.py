"""The step machine's decision table, called directly. The gate around it, and committing its
``Advance``, are tested end to end in ``test_onboarding.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.db.engine import Database
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.onboarding import (
    CONNECT,
    REPLY,
    Advance,
    connect_text,
    guessed_text,
    onboarding_step,
    welcome_text,
)
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, insert_inbox
from tests.fixtures.tenants import IL_PHONE, NL_PHONE, US_PHONE, link_payload, new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

AMSTERDAM = "Europe/Amsterdam"


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


def _state(
    tenant_id: UUID, *, step: str | None = "zone", language: str = "en", first: bool = False
) -> TenantState:
    return TenantState(
        id=tenant_id, status="onboarding", step=step, language=language, first_message=first
    )


async def _step(
    db: Database,
    conn: Any,
    settings: Settings,
    state: TenantState,
    *,
    body: str | None,
    reply_id: str | None = None,
    phone: str | None = NL_PHONE,
    user_id: str | None = None,
    message_type: str = "text",
) -> tuple[Advance, FakeOutbound]:
    channel = FakeOutbound(name="gowa")
    inbox_id = await insert_inbox(
        conn,
        phone=phone,
        user_id=user_id,
        body=body,
        reply_id=reply_id,
        message_type=message_type,
    )
    row = await load_row(db, inbox_id)
    assert row is not None
    advance = await onboarding_step(
        db, channel, row, state, settings=settings, now=datetime.now(UTC)
    )
    return advance, channel


async def _kinds(conn: Any) -> list[str]:
    return [
        r["kind"] for r in await conn.fetch("SELECT kind FROM channel_outbox ORDER BY created_at")
    ]


class TestTexts:
    def test_the_welcome_with_a_guess(self) -> None:
        assert guessed_text("en", AMSTERDAM, "URL", welcome=True) == (
            f"{t('welcome', 'en')}\n\n"
            "I've set your clock to Amsterdam time, going by your number. If you're somewhere "
            "else, just tell me which city.\n\n"
            f"{t('connect_link', 'en', url='URL')}"
        )

    def test_the_welcome_without_a_guess(self) -> None:
        assert welcome_text("en") == f"{t('welcome', 'en')}\n\n{t('zone_ask_city', 'en')}"

    def test_the_hebrew_zone_is_named_in_hebrew(self) -> None:
        assert "לשעון ירושלים" in guessed_text("he", "Asia/Jerusalem", "URL")
        assert connect_text("he", "Asia/Jerusalem", "URL").startswith(
            "מעולה, כיוונתי לשעון ירושלים."
        )


class TestZoneStep:
    @pytest.mark.parametrize("first", [True, False])
    async def test_the_guess_is_set_without_asking(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings, first: bool
    ) -> None:
        tenant_id = await new_tenant(db)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, first=first), body="hi"
        )
        assert advance == Advance(timezone=AMSTERDAM, next_step="connect")
        [message] = channel.sent
        assert message.recipient == NL_PHONE
        url = message.body.rsplit("\n", 1)[-1]
        assert message.body == guessed_text("en", AMSTERDAM, url, welcome=first)
        assert link_payload(message.body).tenant_id == tenant_id
        assert await _kinds(owner_conn) == [REPLY]

    async def test_a_first_message_city_is_not_read_as_an_answer(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db)
        advance, _ = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, first=True), body="London"
        )
        assert advance == Advance(timezone=AMSTERDAM, next_step="connect")

    async def test_no_guess_welcome_asks_for_a_city(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=US_PHONE)
        advance, channel = await _step(
            db,
            owner_conn,
            onboarding_settings,
            _state(tenant_id, first=True),
            body="hi",
            phone=US_PHONE,
        )
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [welcome_text("en")]

    @pytest.mark.parametrize(
        ("phone", "body", "zone"),
        [
            (NL_PHONE, "London", "Europe/London"),  # a city over the guess
            (NL_PHONE, "tel aviv", "Asia/Jerusalem"),
            (US_PHONE, "new york", "America/New_York"),
            (US_PHONE, "Europe/Berlin", "Europe/Berlin"),
        ],
    )
    async def test_a_city_confirms_its_zone(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        phone: str,
        body: str,
        zone: str,
    ) -> None:
        tenant_id = await new_tenant(db, phone=phone)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body=body, phone=phone
        )
        assert advance == Advance(timezone=zone, next_step="connect")
        url = channel.sent[0].body.rsplit("\n", 1)[-1]
        assert channel.sent[0].body == connect_text("en", zone, url)
        assert await _kinds(owner_conn) == [CONNECT]

    @pytest.mark.parametrize(("body", "message_type"), [("Mars", "text"), (None, "audio")])
    async def test_an_unmatched_reply_gets_the_retry_prompt(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        body: str | None,
        message_type: str,
    ) -> None:
        """A voice note, a sticker or an image counts as unmatched (Review Focus 5)."""
        tenant_id = await new_tenant(db, phone=US_PHONE)
        advance, channel = await _step(
            db,
            owner_conn,
            onboarding_settings,
            _state(tenant_id),
            body=body,
            phone=US_PHONE,
            message_type=message_type,
        )
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [t("zone_retry", "en")]
        assert await _kinds(owner_conn) == [REPLY]

    async def test_hebrew_tenant_hebrew_replies(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=IL_PHONE, language="he")
        _, channel = await _step(
            db,
            owner_conn,
            onboarding_settings,
            _state(tenant_id, language="he", first=True),
            body="היי",
            phone=IL_PHONE,
        )
        url = channel.sent[0].body.rsplit("\n", 1)[-1]
        assert [m.body for m in channel.sent] == [
            guessed_text("he", "Asia/Jerusalem", url, welcome=True)
        ]


class TestConnectStep:
    @pytest.mark.parametrize("step", ["connect", None])
    async def test_any_message_resends_the_link(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings, step: str | None
    ) -> None:
        tenant_id = await new_tenant(db, step="connect")
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, step=step), body="hello?"
        )
        assert advance == Advance()
        [message] = channel.sent
        url = message.body.rsplit("\n", 1)[-1]
        assert message.body == t("connect_link", "en", url=url)
        assert link_payload(message.body).tenant_id == tenant_id
        assert await _kinds(owner_conn) == [REPLY]

    async def test_a_city_replaces_the_guessed_zone(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, step="connect")
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, step="connect"), body="London"
        )
        assert advance == Advance(timezone="Europe/London", next_step="connect")
        url = channel.sent[0].body.rsplit("\n", 1)[-1]
        assert channel.sent[0].body == connect_text("en", "Europe/London", url)


class TestAddressing:
    async def test_a_bsuid_only_message_goes_to_the_tenants_number(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db)
        _, channel = await _step(
            db,
            owner_conn,
            onboarding_settings,
            _state(tenant_id),
            body="Mars",
            phone=None,
            user_id="US.1",
        )
        assert [m.recipient for m in channel.sent] == [NL_PHONE]

    async def test_no_number_anywhere_sends_nothing(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=None, user_id="US.9")
        advance, channel = await _step(
            db,
            owner_conn,
            onboarding_settings,
            _state(tenant_id),
            body="1",
            phone=None,
            user_id="US.9",
        )
        assert advance == Advance()
        assert channel.attempts == []
