"""``schedule:briefing``'s logic: one morning message a local day, and its fallbacks.

The model writes it from today's events and still-pending reminders. Code sends a fixed list
when the model is down on the last attempt, refuses, or writes something unusable, and the
reminders alone with the ``calendar_disconnected`` line when the calendar is gone.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.errors import (
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
    LLMRejectedError,
    LLMUnavailableError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Reminder
from personal_organizer.db.repositories import reminders
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.tenants import get_tenant, set_timezone
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging import briefing
from personal_organizer.messaging.agent import shown_event
from personal_organizer.messaging.briefing import (
    BRIEFING_KIND,
    briefing_key,
    fallback_briefing,
    send_briefing,
)
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.proactive import proactive_target
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.db.test_handle_inbound import FakeOutbound
from tests.db.test_onboarding_connected import resolver
from tests.fixtures.connect import (
    PHONE,
    TRUNCATE_CONNECT_TABLES,
    make_active,
    seed_inbound,
    seed_tenant,
    suspend,
)
from tests.fixtures.llm import NoLLM, ScriptedLLM, said
from tests.fixtures.tenants import messages_of

pytestmark = [pytest.mark.db]

ZONE = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 15, 4, 30, tzinfo=UTC)  # 07:30 in Jerusalem
DAY = date(2026, 10, 15)
DENTIST = CalendarEvent(
    "e1",
    "Dentist",
    datetime(2026, 10, 15, 9, 0, tzinfo=ZONE),
    datetime(2026, 10, 15, 10, tzinfo=ZONE),
)


class Failing(InMemoryCalendar):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def list_events(self, *args: Any, **kwargs: Any) -> list[CalendarEvent]:
        raise self.error


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


async def _tenant(db: Database, *, connected: bool = True) -> UUID:
    """An active Hebrew tenant in Jerusalem who has written on GOWA."""
    tenant_id = await seed_tenant(db, language="he")
    await make_active(db, tenant_id)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
        if connected:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_bound", auth_config_id="ac_test"
            )
    await seed_inbound(db, tenant_id, channel="gowa", sent_at=NOW - timedelta(hours=10))
    return tenant_id


async def _remind(db: Database, tenant_id: UUID, due_at: datetime, text: str = "call mom") -> UUID:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await reminders.add_reminder(session, tenant_id, text=text, due_at=due_at)


async def _pending(db: Database, tenant_id: UUID) -> list[Reminder]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await reminders.upcoming_reminders(session, tenant_id, after=NOW)


async def _briefed_on(db: Database, tenant_id: UUID) -> date | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
    assert tenant is not None
    return tenant.briefed_on


async def _out(db: Database, tenant_id: UUID) -> list[str | None]:
    return [m.body for m in await messages_of(db, tenant_id) if m.direction == "out"]


async def _brief(
    db: Database,
    tenant_id: UUID,
    llm: Any,
    calendar: InMemoryCalendar | None = None,
    *,
    gowa: FakeOutbound | None = None,
    last_attempt: bool = False,
) -> tuple[str | None, FakeOutbound]:
    gowa = gowa or FakeOutbound(name="gowa")
    status = await send_briefing(
        tenant_id,
        DAY,
        db=db,
        channels=resolver(gowa),
        llm=llm,
        calendar=calendar if calendar is not None else InMemoryCalendar([DENTIST]),
        now=NOW,
        last_attempt=last_attempt,
    )
    return status, gowa


class TestSendBriefing:
    async def test_it_sends_the_models_briefing_once_and_marks_the_day(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await _tenant(db)
        status, gowa = await _brief(db, tenant_id, ScriptedLLM(said("Morning! Dentist at 9.")))

        assert status == "accepted"
        [message] = gowa.sent
        assert (message.recipient, message.body) == (PHONE, "Morning! Dentist at 9.")
        rows = await owner_conn.fetch("SELECT kind, idempotency_key FROM channel_outbox")
        assert [tuple(row) for row in rows] == [(BRIEFING_KIND, f"briefing:{tenant_id}:2026-10-15")]
        assert briefing_key(tenant_id, DAY) == f"briefing:{tenant_id}:2026-10-15"
        assert await _briefed_on(db, tenant_id) == DAY
        assert await _out(db, tenant_id) == ["Morning! Dentist at 9."]

        again, _ = await _brief(db, tenant_id, NoLLM(), gowa=gowa)
        assert again is None
        assert len(gowa.attempts) == 1

    async def test_a_rerun_after_the_send_marks_and_records_without_sending(
        self, db: Database
    ) -> None:
        """The job died after the send, before ``briefed_on`` was set: the key is settled."""
        tenant_id = await _tenant(db)
        gowa = FakeOutbound(name="gowa")
        await send_once(
            db,
            gowa,
            inbox_id=None,
            kind=BRIEFING_KIND,
            recipient_key=f"tel:{PHONE}",
            to=PHONE,
            text="Morning!",
            idempotency_key=briefing_key(tenant_id, DAY),
        )
        status, _ = await _brief(db, tenant_id, ScriptedLLM(said("Morning!")), gowa=gowa)
        assert status == "accepted"
        assert len(gowa.attempts) == 1
        assert await _briefed_on(db, tenant_id) == DAY
        assert await _out(db, tenant_id) == ["Morning!"]

    async def test_a_run_racing_another_records_the_message_once(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two runs both read the day as not briefed: only the one that marks it records."""
        tenant_id = await _tenant(db)
        gowa = FakeOutbound(name="gowa")
        await _brief(db, tenant_id, ScriptedLLM(said("Morning!")), gowa=gowa)

        async def not_yet_briefed(*args: Any, **kwargs: Any) -> Any:
            target = await proactive_target(*args, **kwargs)
            assert target is not None
            target.tenant.briefed_on = None
            return target

        monkeypatch.setattr(briefing, "proactive_target", not_yet_briefed)
        status, _ = await _brief(db, tenant_id, ScriptedLLM(said("Morning!")), gowa=gowa)

        assert status == "accepted"
        assert len(gowa.attempts) == 1
        assert await _out(db, tenant_id) == ["Morning!"]

    async def test_only_todays_events_and_still_pending_reminders_are_used(
        self, db: Database
    ) -> None:
        tenant_id = await _tenant(db)
        tomorrow = CalendarEvent(
            "e2",
            "Gym",
            datetime(2026, 10, 16, 8, tzinfo=ZONE),
            datetime(2026, 10, 16, 9, tzinfo=ZONE),
        )
        await _remind(db, tenant_id, datetime(2026, 10, 15, 18, tzinfo=ZONE))
        await _remind(db, tenant_id, datetime(2026, 10, 15, 6, tzinfo=ZONE), "already fired")
        cancelled = await _remind(db, tenant_id, datetime(2026, 10, 15, 20, tzinfo=ZONE), "off")
        await _remind(db, tenant_id, datetime(2026, 10, 16, 9, tzinfo=ZONE), "tomorrow")
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await reminders.cancel_reminder(session, tenant_id, cancelled)
        llm = ScriptedLLM(said("Morning!"))
        calendar = InMemoryCalendar([DENTIST, tomorrow])

        await _brief(db, tenant_id, llm, calendar)

        [(_, _, start, end)] = calendar.calls
        assert (start, end) == (
            datetime(2026, 10, 15, tzinfo=ZONE),
            datetime(2026, 10, 16, tzinfo=ZONE),
        )
        [[_, user]] = llm.calls
        assert json.loads(user.content) == {
            "events": [shown_event(DENTIST, ZONE)],
            "reminders": [{"text": "call mom", "at": "18:00"}],
        }

    async def test_an_empty_day_sends_the_fixed_line_without_the_model(self, db: Database) -> None:
        tenant_id = await _tenant(db)
        status, gowa = await _brief(db, tenant_id, NoLLM(), InMemoryCalendar())
        assert status == "accepted"
        assert [m.body for m in gowa.sent] == [t("briefing_empty", "he")]

    async def test_the_model_down_on_the_last_attempt_sends_the_fixed_list(
        self, db: Database
    ) -> None:
        tenant_id = await _tenant(db)
        with pytest.raises(LLMUnavailableError):
            await _brief(db, tenant_id, ScriptedLLM(LLMUnavailableError()))
        assert await _briefed_on(db, tenant_id) is None
        assert await _out(db, tenant_id) == []

        _, gowa = await _brief(db, tenant_id, ScriptedLLM(LLMUnavailableError()), last_attempt=True)
        fallback = fallback_briefing([DENTIST], [], zone=ZONE, language="he")
        assert [m.body for m in gowa.sent] == [fallback]
        assert await _briefed_on(db, tenant_id) == DAY

    @pytest.mark.parametrize("reply", [LLMRejectedError(400), said("see http://x")])
    async def test_a_refusal_or_unusable_reply_sends_the_fixed_list(
        self, db: Database, reply: Any
    ) -> None:
        tenant_id = await _tenant(db)
        _, gowa = await _brief(db, tenant_id, ScriptedLLM(reply))
        fallback = fallback_briefing([DENTIST], [], zone=ZONE, language="he")
        assert [m.body for m in gowa.sent] == [fallback]

    async def test_llm_none_sends_the_fixed_list(self, db: Database) -> None:
        tenant_id = await _tenant(db)
        _, gowa = await _brief(db, tenant_id, None)
        fallback = fallback_briefing([DENTIST], [], zone=ZONE, language="he")
        assert [m.body for m in gowa.sent] == [fallback]

    async def test_a_disconnected_calendar_sends_reminders_and_the_disconnected_line(
        self, db: Database
    ) -> None:
        tenant_id = await _tenant(db, connected=False)
        await _remind(db, tenant_id, datetime(2026, 10, 15, 18, tzinfo=ZONE))
        _, gowa = await _brief(db, tenant_id, NoLLM())
        listed = fallback_briefing([], await _pending(db, tenant_id), zone=ZONE, language="he")
        assert [m.body for m in gowa.sent] == [f"{listed}\n\n{t('calendar_disconnected', 'he')}"]

    async def test_a_disconnected_calendar_and_no_reminders_is_just_the_line(
        self, db: Database
    ) -> None:
        tenant_id = await _tenant(db, connected=False)
        _, gowa = await _brief(db, tenant_id, NoLLM())
        assert [m.body for m in gowa.sent] == [t("calendar_disconnected", "he")]

    async def test_a_calendar_outage_retries_then_sends_reminders_and_says_so(
        self, db: Database
    ) -> None:
        tenant_id = await _tenant(db)
        await _remind(db, tenant_id, datetime(2026, 10, 15, 18, tzinfo=ZONE))
        down = Failing(CalendarProviderUnavailableError())
        with pytest.raises(CalendarProviderUnavailableError):
            await _brief(db, tenant_id, None, down)
        assert await _out(db, tenant_id) == []

        _, gowa = await _brief(db, tenant_id, NoLLM(), down, last_attempt=True)
        listed = fallback_briefing([], await _pending(db, tenant_id), zone=ZONE, language="he")
        line = t("briefing_calendar_unavailable", "he")
        assert [m.body for m in gowa.sent] == [f"{listed}\n\n{line}"]

    @pytest.mark.parametrize(
        ("error", "last_attempt"),
        [(CalendarProviderUnavailableError(), True), (CalendarProviderRejectedError(400), False)],
    )
    async def test_a_failed_calendar_read_and_no_reminders_sends_nothing(
        self, db: Database, error: Exception, last_attempt: bool
    ) -> None:
        """Never "nothing on your calendar" when the calendar could not be read."""
        tenant_id = await _tenant(db)
        status, gowa = await _brief(
            db, tenant_id, NoLLM(), Failing(error), last_attempt=last_attempt
        )
        assert status is None
        assert gowa.attempts == []
        assert await _briefed_on(db, tenant_id) == DAY

    async def test_nowhere_to_send_marks_the_day(self, db: Database) -> None:
        """Else the tick defers it again every minute of the window."""
        tenant_id = await seed_tenant(db, language="he")
        await make_active(db, tenant_id)
        status, gowa = await _brief(db, tenant_id, NoLLM())
        assert status is None
        assert gowa.attempts == []
        assert await _briefed_on(db, tenant_id) == DAY

    async def test_an_inactive_tenant_gets_nothing(self, db: Database) -> None:
        tenant_id = await _tenant(db)
        await suspend(db, tenant_id)
        status, gowa = await _brief(db, tenant_id, NoLLM())
        assert status is None
        assert gowa.attempts == []

    async def test_a_tenant_already_briefed_today_gets_nothing(self, db: Database) -> None:
        tenant_id = await _tenant(db)
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await reminders.mark_briefed(session, tenant_id, DAY)
        status, gowa = await _brief(db, tenant_id, NoLLM())
        assert status is None
        assert gowa.attempts == []
