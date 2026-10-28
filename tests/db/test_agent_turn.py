"""An active tenant's turn (PR 2): through the gate, with a fake model and calendar.

Done-When: an active tenant gets an LLM answer, not the fixed ack, sent once and recorded
with their message. Also pinned: re-runs, the fallbacks of D3 and the no-calendar line of D9.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderUnavailableError,
    LLMRejectedError,
    LLMUnavailableError,
    TransientChannelError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.messages import record_inbound, record_outbound
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging.agent import AGENT_REPLY, agent_turn
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.onboarding import LINK_PLACEHOLDER, Advance
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState, load_state
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, insert_inbox
from tests.fixtures.llm import NoLLM, ScriptedLLM, call, said
from tests.fixtures.tenants import IL_PHONE, link_payload, messages_of, new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
TOMORROW = call("list_events", start="2026-10-13T00:00", end="2026-10-14T00:00")


class Failing(InMemoryCalendar):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def list_events(self, *args: Any, **kwargs: Any) -> list[CalendarEvent]:
        raise self.error


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _active(db: Database, *, connected: bool = True) -> TenantState:
    tenant_id = await new_tenant(db, phone=IL_PHONE, language="he", status="active", step=None)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
        if connected:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_bound", auth_config_id="ac_test"
            )
    state = await load_state(db, tenant_id)
    assert state is not None
    return state


async def _turn(
    db: Database,
    conn: Any,
    tenant: TenantState,
    llm: Any,
    calendar: InMemoryCalendar | None = None,
    *,
    inbox_id: UUID | None = None,
    phone: str | None = IL_PHONE,
    last_attempt: bool = False,
    settings: Settings | None = None,
) -> tuple[Advance, FakeOutbound, UUID]:
    channel = FakeOutbound(name="gowa")
    if inbox_id is None:
        inbox_id = await insert_inbox(
            conn, phone=phone, user_id=None if phone else "IL.1", body="מה יש לי מחר?"
        )
    row = await load_row(db, inbox_id)
    assert row is not None
    advance = await agent_turn(
        row,
        channel,
        tenant,
        NOW,
        db=db,
        llm=llm,
        calendar=calendar or InMemoryCalendar(),
        settings=settings,
        last_attempt=last_attempt,
    )
    return advance, channel, inbox_id


async def _kinds(conn: Any) -> list[tuple[str, str]]:
    rows = await conn.fetch("SELECT kind, status FROM channel_outbox ORDER BY created_at")
    return [(row["kind"], row["status"]) for row in rows]


class TestThroughTheGate:
    async def test_the_reply_is_sent_once_and_recorded(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant = await _active(db)
        dentist = CalendarEvent(
            "evt-1",
            "רופא שיניים",
            datetime(2026, 10, 13, 6, 0, tzinfo=UTC),
            datetime(2026, 10, 13, 6, 30, tzinfo=UTC),
        )
        calendar = InMemoryCalendar([dentist])
        llm = ScriptedLLM(said("", TOMORROW), said("מחר ב-09:00 רופא שיניים."))
        channel = FakeOutbound(name="gowa")
        inbox_id = await insert_inbox(owner_conn, phone=IL_PHONE, body="מה יש לי מחר?")

        disposition = await handle_inbound(
            inbox_id,
            db=db,
            channels={"gowa": channel}.__getitem__,
            allowlist=frozenset(),
            on_allowed=functools.partial(agent_turn, db=db, llm=llm, calendar=calendar),
            settings=onboarding_settings,
            now=lambda: NOW,
        )

        assert disposition == "allowed"
        assert [m.body for m in channel.sent] == ["מחר ב-09:00 רופא שיניים."]
        assert await _kinds(owner_conn) == [(AGENT_REPLY, "accepted")]
        [(account, user, start, end)] = calendar.calls
        assert (account, user) == ("ca_bound", str(tenant.id))
        assert (start.isoformat(), end.isoformat()) == (
            "2026-10-13T00:00:00+03:00",
            "2026-10-14T00:00:00+03:00",
        )
        assert '"start": "2026-10-13T09:00"' in llm.calls[1][-1].content
        assert [(m.direction, m.body) for m in await messages_of(db, tenant.id)] == [
            ("in", "מה יש לי מחר?"),
            ("out", "מחר ב-09:00 רופא שיניים."),
        ]


class TestReRuns:
    async def test_a_re_run_after_the_send_calls_no_model_and_sends_nothing(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        first, channel, inbox_id = await _turn(db, owner_conn, tenant, ScriptedLLM(said("שלום")))
        assert first == Advance(reply="שלום")
        assert [m.body for m in channel.sent] == ["שלום"]

        again, rerun_channel, _ = await _turn(db, owner_conn, tenant, NoLLM(), inbox_id=inbox_id)
        assert again == Advance()
        assert rerun_channel.attempts == []
        assert await _kinds(owner_conn) == [(AGENT_REPLY, "accepted")]

    async def test_an_unavailable_model_raises_and_sends_nothing(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        with pytest.raises(LLMUnavailableError):
            await _turn(db, owner_conn, tenant, ScriptedLLM(LLMUnavailableError("down")))
        assert await _kinds(owner_conn) == []


class TestFallbacks:
    @pytest.mark.parametrize(
        ("llm", "calendar"),
        [
            (ScriptedLLM(LLMUnavailableError("down")), InMemoryCalendar()),
            (
                ScriptedLLM(said("", TOMORROW)),
                Failing(CalendarProviderUnavailableError("down")),
            ),
        ],
        ids=["model down", "calendar down"],
    )
    async def test_the_last_attempt_sends_the_fixed_line(
        self, db: Database, owner_conn: Any, llm: ScriptedLLM, calendar: InMemoryCalendar
    ) -> None:
        tenant = await _active(db)
        advance, channel, _ = await _turn(db, owner_conn, tenant, llm, calendar, last_attempt=True)
        fixed = t("agent_unavailable", "he")
        assert [m.body for m in channel.sent] == [fixed]
        assert advance == Advance(reply=fixed)
        assert await _kinds(owner_conn) == [(AGENT_REPLY, "accepted")]

    async def test_a_down_calendar_raises_before_the_last_attempt(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        calendar = Failing(CalendarProviderUnavailableError("down"))
        with pytest.raises(CalendarProviderUnavailableError):
            await _turn(db, owner_conn, tenant, ScriptedLLM(said("", TOMORROW)), calendar)
        assert await _kinds(owner_conn) == []

    async def test_a_refusal_sends_the_fixed_line_on_any_attempt(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        _, channel, _ = await _turn(db, owner_conn, tenant, ScriptedLLM(LLMRejectedError(None)))
        assert [m.body for m in channel.sent] == [t("agent_unavailable", "he")]

    async def test_an_empty_answer_sends_the_fixed_line(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        _, channel, _ = await _turn(db, owner_conn, tenant, ScriptedLLM(said("  ")))
        assert [m.body for m in channel.sent] == [t("agent_unavailable", "he")]

    async def test_an_overlong_answer_is_cut_to_one_message(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        long = "\n".join(f"line {n}" for n in range(1000))
        advance, channel, _ = await _turn(db, owner_conn, tenant, ScriptedLLM(said(long)))
        [sent] = channel.sent
        assert len(sent.body) <= WHATSAPP_TEXT_LIMIT
        assert sent.body.endswith("…")
        assert advance.reply == sent.body


class TestSplitReply:
    async def test_confirm_is_its_own_last_part(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        propose = call(
            "propose_event", title="Dentist", start="2026-10-13T09:00", end="2026-10-13T10:00"
        )
        llm = ScriptedLLM(said("", propose), said("Got it.\n---\nTuesday at nine, then."))
        advance, channel, _ = await _turn(db, owner_conn, tenant, llm)
        [first, second, last] = channel.sent
        assert (first.body, second.body) == ("Got it.", "Tuesday at nine, then.")
        assert last.body.startswith("Dentist\n")
        assert last.body.endswith("להוסיף ליומן?")
        assert advance.reply == f"{first.body}\n\n{second.body}\n\n{last.body}"
        assert "---" not in advance.reply

    async def test_a_later_part_going_down_still_records_the_reply(
        self, db: Database, owner_conn: Any
    ) -> None:
        class SecondTransient(FakeOutbound):
            async def send_text(self, message: OutboundMessage) -> str:
                if len(self.attempts) == 1:
                    self.attempts.append(message)
                    raise TransientChannelError("down")
                return await super().send_text(message)

        tenant = await _active(db)
        row_id = await insert_inbox(owner_conn, phone=IL_PHONE, body="hi")
        row = await load_row(db, row_id)
        assert row is not None
        channel = SecondTransient(name="gowa")
        advance = await agent_turn(
            row,
            channel,
            tenant,
            NOW,
            db=db,
            llm=ScriptedLLM(said("one\n---\ntwo\n---\nthree")),
            calendar=InMemoryCalendar(),
        )
        assert [m.body for m in channel.sent] == ["one"]
        assert advance.reply == "one\n\ntwo\n\nthree"

    async def test_only_break_lines_is_the_fallback(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        _, channel, _ = await _turn(db, owner_conn, tenant, ScriptedLLM(said("---")))
        assert [m.body for m in channel.sent] == [t("agent_unavailable", "he")]


class TestNoCalendar:
    async def test_no_connection_gets_a_link_to_connect(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant = await _active(db, connected=False)
        calendar = InMemoryCalendar()
        llm = ScriptedLLM(said("", TOMORROW), said("אני עוד לא רואה את היומן שלך."))
        advance, channel, _ = await _turn(
            db, owner_conn, tenant, llm, calendar, settings=onboarding_settings
        )
        [text] = [m.body for m in channel.sent]
        url = text.rsplit("\n", 1)[-1]
        line = t("calendar_not_connected", "he", url=url)
        assert text == f"אני עוד לא רואה את היומן שלך.\n\n{line}"
        assert link_payload(text).tenant_id == tenant.id
        assert calendar.calls == []
        # The link is a bearer credential: the history keeps a placeholder.
        assert advance.reply == text.replace(url, LINK_PLACEHOLDER)

    async def test_an_account_that_stopped_working_gets_a_link_to_reconnect(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant = await _active(db)
        calendar = Failing(CalendarNotConnectedError(401))
        llm = ScriptedLLM(said("", TOMORROW), said(""))
        _, channel, _ = await _turn(
            db, owner_conn, tenant, llm, calendar, settings=onboarding_settings
        )
        [text] = [m.body for m in channel.sent]
        assert text == t("calendar_not_connected", "he", url=text.rsplit("\n", 1)[-1])

    async def test_without_settings_the_line_says_to_ask(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db, connected=False)
        llm = ScriptedLLM(said("", TOMORROW), said(""))
        _, channel, _ = await _turn(db, owner_conn, tenant, llm)
        assert [m.body for m in channel.sent] == [t("calendar_disconnected", "he")]


class TestContext:
    async def test_the_recorded_history_reaches_the_model(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        earlier = await insert_inbox(owner_conn, phone=IL_PHONE, body="היי")
        async with db.tenant_session(TenantId(tenant.id)) as session:
            await record_inbound(
                session,
                tenant.id,
                inbox_id=earlier,
                channel="gowa",
                message_type="text",
                body="היי",
                sent_at=NOW,
            )
            await record_outbound(
                session, tenant.id, inbox_id=earlier, channel="gowa", body="שלום! במה אפשר לעזור?"
            )
        llm = ScriptedLLM(said("אין לך כלום מחר."))
        await _turn(db, owner_conn, tenant, llm)
        assert llm.calls[0][1:] == [
            Message("user", "היי"),
            Message("assistant", "שלום! במה אפשר לעזור?"),
            Message("user", "מה יש לי מחר?"),
        ]

    async def test_a_sender_with_no_number_gets_no_reply(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await new_tenant(db, phone=None, user_id="IL.1", status="active", step=None)
        tenant = await load_state(db, tenant_id)
        assert tenant is not None
        advance, channel, _ = await _turn(db, owner_conn, tenant, NoLLM(), phone=None)
        assert advance == Advance()
        assert channel.attempts == []
