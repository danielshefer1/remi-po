"""Adding an event behind a confirm (PR 3, D5): ``agent_turn`` with a fake model and calendar.

Done-When: a proposal goes out with its question written by code and is stored; a reply the
confirm call reads as ADD creates the event exactly once, however the job re-runs or the person
repeats it; CANCEL, anything else or an hour's wait each leave nothing to confirm. Only OTHER
reaches the agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from personal_organizer.core.errors import (
    CalendarNotConnectedError,
    CalendarProviderUnavailableError,
    LLMUnavailableError,
    TransientChannelError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import pending
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.messaging.agent import CREATE_EVENT, agent_turn
from personal_organizer.messaging.confirm import confirm_prompt
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.onboarding import Advance
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState, load_state
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, insert_inbox
from tests.fixtures.llm import NoLLM, ScriptedLLM, call, said
from tests.fixtures.tenants import IL_PHONE, new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
DENTIST = call(
    "propose_event", title="רופא שיניים", start="2026-10-13T09:00", end="2026-10-13T10:00"
)
SUMMARY = "\n".join(["רופא שיניים", "יום שלישי 2026-10-13, 09:00-10:00"])
CHOICE = confirm_prompt(SUMMARY, "he")
ADDED = t("event_added", "he", summary=SUMMARY)


class Outbound(FakeOutbound):
    """One per turn, so each needs its own message ids."""

    async def send_text(self, message: OutboundMessage) -> str:
        await super().send_text(message)
        return f"gowa.{uuid4().hex}"


class FailingCreate(InMemoryCalendar):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.creates = 0

    async def create_event(self, *args: Any, **kwargs: Any) -> CalendarEvent:
        self.creates += 1
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
    calendar: InMemoryCalendar,
    body: str | None = "כן",
    *,
    inbox_id: UUID | None = None,
    channel: FakeOutbound | None = None,
    now: datetime = NOW,
    last_attempt: bool = False,
) -> tuple[Advance, FakeOutbound, UUID]:
    channel = channel or Outbound(name="gowa")
    if inbox_id is None:
        inbox_id = await insert_inbox(conn, phone=IL_PHONE, body=body)
    row = await load_row(db, inbox_id)
    assert row is not None
    advance = await agent_turn(
        row,
        channel,
        tenant,
        now,
        db=db,
        llm=llm,
        calendar=calendar,
        last_attempt=last_attempt,
    )
    return advance, channel, inbox_id


async def _propose(
    db: Database, conn: Any, tenant: TenantState, calendar: InMemoryCalendar
) -> UUID:
    llm = ScriptedLLM(said("", DENTIST), said("להוסיף את זה?"))
    _, channel, inbox_id = await _turn(db, conn, tenant, llm, calendar, "תוסיף רופא שיניים מחר")
    assert [m.body for m in channel.sent] == ["להוסיף את זה?", CHOICE]
    return inbox_id


async def _current(
    db: Database, tenant: TenantState, now: datetime = NOW
) -> pending.PendingAction | None:
    async with db.tenant_session(TenantId(tenant.id)) as session:
        return await pending.current(session, tenant.id, now=now)


class TestPropose:
    async def test_the_choice_is_written_by_code_and_the_proposal_stored(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        inbox_id = await _propose(db, owner_conn, tenant, calendar)
        action = await _current(db, tenant)
        assert action is not None
        assert action.kind == CREATE_EVENT
        assert action.payload == {
            "title": "רופא שיניים",
            "start": "2026-10-13T09:00",
            "end": "2026-10-13T10:00",
            "all_day": False,
            "location": None,
            "proposed_by": str(inbox_id),
        }
        assert calendar.events == []

    async def test_a_fallback_stores_no_proposal(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        llm = ScriptedLLM(said("", DENTIST), LLMUnavailableError("down"))
        _, channel, _ = await _turn(
            db, owner_conn, tenant, llm, InMemoryCalendar(), "תוסיף", last_attempt=True
        )
        assert [m.body for m in channel.sent] == [t("agent_unavailable", "he")]
        assert await _current(db, tenant) is None


def _yes() -> ScriptedLLM:
    """The confirm call reading the reply as ADD."""
    return ScriptedLLM(said("ADD"))


class TestConfirm:
    @pytest.mark.parametrize("answer", ["ADD", " add.\n"])
    async def test_add_creates_the_event_once_without_the_agent(
        self, db: Database, owner_conn: Any, answer: str
    ) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        llm = ScriptedLLM(said(answer))
        advance, channel, _ = await _turn(db, owner_conn, tenant, llm, calendar, "יאללה, תוסיף")
        # One call, which saw the summary and the reply and nothing else.
        [[system, user]] = llm.calls
        assert SUMMARY in system.content
        assert (user.role, user.content) == ("user", "יאללה, תוסיף")
        [event] = calendar.events
        assert (event.title, event.start.isoformat(), event.end.isoformat()) == (
            "רופא שיניים",
            "2026-10-13T09:00:00+03:00",
            "2026-10-13T10:00:00+03:00",
        )
        assert [m.body for m in channel.sent] == [ADDED]
        assert advance == Advance(reply=ADDED)
        assert await _current(db, tenant) is None

    async def test_a_re_run_after_the_send_creates_and_sends_nothing(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        _, _, inbox_id = await _turn(db, owner_conn, tenant, _yes(), calendar)
        again, channel, _ = await _turn(
            db, owner_conn, tenant, NoLLM(), calendar, inbox_id=inbox_id
        )
        assert again == Advance()
        assert channel.attempts == []
        assert len(calendar.events) == 1

    async def test_a_re_run_after_the_claim_creates_and_sends_nothing(
        self, db: Database, owner_conn: Any
    ) -> None:
        """The send failed after the create: the claim is spent, and nothing asks the model
        what a bare "yes" means."""
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        failing = Outbound(TransientChannelError("down"), name="gowa")
        inbox_id = await insert_inbox(owner_conn, phone=IL_PHONE, body="כן")
        with pytest.raises(TransientChannelError):
            await _turn(
                db, owner_conn, tenant, _yes(), calendar, inbox_id=inbox_id, channel=failing
            )
        again, channel, _ = await _turn(
            db, owner_conn, tenant, NoLLM(), calendar, inbox_id=inbox_id
        )
        assert again == Advance()
        assert channel.attempts == []
        assert len(calendar.events) == 1

    async def test_a_second_one_creates_nothing_more(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        await _turn(db, owner_conn, tenant, _yes(), calendar)
        # Nothing is open any more, so this "yes" is ordinary text for the agent.
        llm = ScriptedLLM(said("במה עוד אפשר לעזור?"))
        _, channel, _ = await _turn(db, owner_conn, tenant, llm, calendar)
        assert [m.body for m in channel.sent] == ["במה עוד אפשר לעזור?"]
        assert len(calendar.events) == 1

    async def test_a_claim_that_finds_it_gone_creates_nothing(
        self, db: Database, owner_conn: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seen open, then taken or expired before the claim."""
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)

        async def gone(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(pending, "claim", gone)
        _, channel, _ = await _turn(db, owner_conn, tenant, _yes(), calendar)
        assert [m.body for m in channel.sent] == [t("proposal_closed", "he")]
        assert calendar.events == []

    async def test_an_unavailable_calendar_after_the_claim_is_not_retried(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        calendar = FailingCreate(CalendarProviderUnavailableError("timeout"))
        await _propose(db, owner_conn, tenant, calendar)
        advance, channel, inbox_id = await _turn(db, owner_conn, tenant, _yes(), calendar)
        assert [m.body for m in channel.sent] == [t("event_not_added", "he")]
        assert advance == Advance(reply=t("event_not_added", "he"))
        again, _, _ = await _turn(db, owner_conn, tenant, NoLLM(), calendar, inbox_id=inbox_id)
        assert again == Advance()
        assert calendar.creates == 1

    async def test_an_account_that_stopped_working_gets_the_d9_line(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        calendar = FailingCreate(CalendarNotConnectedError(401))
        await _propose(db, owner_conn, tenant, calendar)
        _, channel, _ = await _turn(db, owner_conn, tenant, _yes(), calendar)
        assert [m.body for m in channel.sent] == [t("calendar_disconnected", "he")]

    async def test_no_connection_gets_the_d9_line(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db, connected=False)
        async with db.tenant_session(TenantId(tenant.id)) as session:
            payload = {
                "title": "x",
                "start": "2026-10-13T09:00",
                "end": "2026-10-13T10:00",
                "all_day": False,
                "location": None,
            }
            await pending.propose(session, tenant.id, kind=CREATE_EVENT, payload=payload, now=NOW)
        calendar = InMemoryCalendar()
        _, channel, _ = await _turn(db, owner_conn, tenant, _yes(), calendar)
        assert [m.body for m in channel.sent] == [t("calendar_disconnected", "he")]
        assert calendar.events == []

    async def test_an_unavailable_confirm_call_is_retried_with_the_proposal_kept(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        inbox_id = await insert_inbox(owner_conn, phone=IL_PHONE, body="כן")
        with pytest.raises(LLMUnavailableError):
            await _turn(
                db,
                owner_conn,
                tenant,
                ScriptedLLM(LLMUnavailableError("down")),
                calendar,
                inbox_id=inbox_id,
            )
        assert await _current(db, tenant) is not None
        await _turn(db, owner_conn, tenant, _yes(), calendar, inbox_id=inbox_id)
        assert len(calendar.events) == 1


class TestNoConfirm:
    async def test_cancel_drops_it_without_the_agent(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        llm = ScriptedLLM(said("CANCEL"))
        _, channel, _ = await _turn(db, owner_conn, tenant, llm, calendar, "לא, עזוב")
        assert [m.body for m in channel.sent] == [t("event_cancelled", "he")]
        assert await _current(db, tenant) is None
        assert calendar.events == []

    async def test_an_expired_proposal_is_absent(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        later = NOW + pending.TTL + timedelta(minutes=1)
        llm = ScriptedLLM(said("מה להוסיף?"))
        _, channel, _ = await _turn(db, owner_conn, tenant, llm, calendar, now=later)
        assert [m.body for m in channel.sent] == ["מה להוסיף?"]
        assert llm.calls[0][-1].content == "כן"
        assert calendar.events == []

    @pytest.mark.parametrize("answer", ["OTHER", "Maybe", ""])
    async def test_another_message_drops_it(
        self, db: Database, owner_conn: Any, answer: str
    ) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        llm = ScriptedLLM(said(answer), said("בסדר, לא נוסיף."))
        await _turn(db, owner_conn, tenant, llm, calendar, "עזוב, אני אבדוק קודם")
        assert await _current(db, tenant) is None
        await _turn(db, owner_conn, tenant, ScriptedLLM(said("?")), calendar)
        assert calendar.events == []

    async def test_another_message_may_propose_again(self, db: Database, owner_conn: Any) -> None:
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        await _propose(db, owner_conn, tenant, calendar)
        ten = call(
            "propose_event", title="רופא שיניים", start="2026-10-13T10:00", end="2026-10-13T11:00"
        )
        llm = ScriptedLLM(said("OTHER"), said("", ten), said("בעשר, אם כך."))
        await _turn(db, owner_conn, tenant, llm, calendar, "בעצם בעשר")
        action = await _current(db, tenant)
        assert action is not None
        assert action.payload["start"] == "2026-10-13T10:00"
        await _turn(db, owner_conn, tenant, _yes(), calendar)
        assert [e.start.isoformat() for e in calendar.events] == ["2026-10-13T10:00:00+03:00"]

    async def test_a_re_run_of_the_proposing_message_cannot_confirm_it(
        self, db: Database, owner_conn: Any
    ) -> None:
        """A "yes" with nothing open proposes; if its send fails, the re-run finds the proposal
        it made, which the person never saw, and must not take "yes" for its confirm."""
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        failing = Outbound(TransientChannelError("down"), name="gowa")
        llm = ScriptedLLM(said("", DENTIST), said("להוסיף?"))
        inbox_id = await insert_inbox(owner_conn, phone=IL_PHONE, body="כן")
        with pytest.raises(TransientChannelError):
            await _turn(db, owner_conn, tenant, llm, calendar, inbox_id=inbox_id, channel=failing)
        assert await _current(db, tenant) is not None
        llm = ScriptedLLM(said("", DENTIST), said("להוסיף?"))
        _, channel, _ = await _turn(db, owner_conn, tenant, llm, calendar, inbox_id=inbox_id)
        assert [m.body for m in channel.sent] == ["להוסיף?", CHOICE]
        assert calendar.events == []

    async def test_one_with_nothing_open_is_text_for_the_model(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant = await _active(db)
        llm = ScriptedLLM(said("במה אפשר לעזור?"))
        _, channel, _ = await _turn(db, owner_conn, tenant, llm, InMemoryCalendar())
        assert [m.body for m in channel.sent] == ["במה אפשר לעזור?"]
