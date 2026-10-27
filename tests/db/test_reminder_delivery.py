"""Sending a due reminder: the fixed line, once, on the tenant's latest channel.

The send is keyed ``reminder:<id>`` (ADR 0003), so a re-run sends nothing new. The reminder
then leaves ``pending`` -- ``sent``, or ``failed`` when it cannot go out -- and a sent one is
kept in the history as an ``out`` message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from personal_organizer.core.errors import RejectedChannelError, TransientChannelError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import reminders
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.messaging import reminders as delivery
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.reminders import REMINDER_KIND, reminder_key, send_reminder
from personal_organizer.settings import Settings
from tests.db.test_handle_inbound import FakeOutbound
from tests.db.test_onboarding_connected import active_tenant, outbox, resolver
from tests.fixtures.connect import PHONE, TRUNCATE_CONNECT_TABLES, seed_inbound

pytestmark = [pytest.mark.db]

TEXT = "להתקשר לאמא"


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


async def _tenant(db: Database, *, inbound: bool = True) -> UUID:
    tenant_id = await active_tenant(db, language="he")
    if inbound:
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
    return tenant_id


async def _add(db: Database, tenant_id: UUID) -> UUID:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await reminders.add_reminder(session, tenant_id, text=TEXT, due_at=datetime.now(UTC))


async def _status(db: Database, tenant_id: UUID, reminder_id: UUID) -> str:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        reminder = await reminders.get_reminder(session, tenant_id, reminder_id)
    assert reminder is not None
    return reminder.status


async def _sent_messages(db: Database, tenant_id: UUID) -> list[tuple[str, UUID | None]]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        rows = await session.execute(
            text("SELECT body, inbox_id FROM messages WHERE direction = 'out'")
        )
    return [(row[0], row[1]) for row in rows]


async def test_it_sends_the_fixed_line_once_and_records_it(db: Database, owner_conn: Any) -> None:
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    gowa = FakeOutbound(name="gowa")

    status = await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )

    assert status == "accepted"
    line = t("reminder", "he", text=TEXT)
    [message] = gowa.sent
    assert (message.recipient, message.body) == (PHONE, line)
    assert await outbox(owner_conn) == [
        {
            "kind": REMINDER_KIND,
            "inbox_id": None,
            "idempotency_key": reminder_key(reminder_id),
            "status": "accepted",
            "channel": "gowa",
        }
    ]
    assert await _status(db, tenant_id, reminder_id) == "sent"
    assert await _sent_messages(db, tenant_id) == [(line, None)]


async def test_a_rerun_sends_nothing_new(db: Database) -> None:
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    gowa = FakeOutbound(name="gowa")

    await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )
    again = await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )

    assert again is None
    assert len(gowa.sent) == 1
    assert len(await _sent_messages(db, tenant_id)) == 1


async def test_a_rerun_after_the_send_but_before_the_update_finishes_the_row(
    db: Database,
) -> None:
    """The first run died after the send: the key is settled, the reminder still pending."""
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    gowa = FakeOutbound(name="gowa")
    line = t("reminder", "he", text=TEXT)
    await send_once(
        db,
        gowa,
        inbox_id=None,
        kind=REMINDER_KIND,
        recipient_key=f"tel:{PHONE}",
        to=PHONE,
        text=line,
        idempotency_key=reminder_key(reminder_id),
    )

    status = await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )

    assert status == "accepted"
    assert len(gowa.sent) == 1
    assert await _status(db, tenant_id, reminder_id) == "sent"
    assert await _sent_messages(db, tenant_id) == [(line, None)]


async def test_a_run_racing_another_records_the_message_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs both read the reminder as pending: only the one that finishes it records."""
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        stale = await reminders.get_reminder(session, tenant_id, reminder_id)
    gowa = FakeOutbound(name="gowa")
    await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )

    async def still_pending(*args: Any) -> Any:
        return stale

    monkeypatch.setattr(delivery, "get_reminder", still_pending)
    status = await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )

    assert status == "accepted"
    assert len(gowa.sent) == 1
    assert len(await _sent_messages(db, tenant_id)) == 1


async def test_a_cancelled_reminder_is_not_sent(db: Database) -> None:
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await reminders.cancel_reminder(session, tenant_id, reminder_id)
    gowa = FakeOutbound(name="gowa")

    assert (
        await send_reminder(
            reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
        )
        is None
    )
    assert gowa.attempts == []
    assert await _status(db, tenant_id, reminder_id) == "cancelled"


async def test_no_channel_or_phone_marks_it_failed(db: Database) -> None:
    tenant_id = await _tenant(db, inbound=False)
    reminder_id = await _add(db, tenant_id)
    gowa = FakeOutbound(name="gowa")

    assert (
        await send_reminder(
            reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
        )
        is None
    )
    assert gowa.attempts == []
    assert await _status(db, tenant_id, reminder_id) == "failed"


async def test_a_refused_send_marks_it_failed(db: Database) -> None:
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    gowa = FakeOutbound(RejectedChannelError(131026), name="gowa")

    status = await send_reminder(
        reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
    )

    assert status == "failed"
    assert await _status(db, tenant_id, reminder_id) == "failed"
    assert await _sent_messages(db, tenant_id) == []


async def test_a_transient_failure_is_raised_for_the_retry(db: Database) -> None:
    tenant_id = await _tenant(db)
    reminder_id = await _add(db, tenant_id)
    gowa = FakeOutbound(TransientChannelError("gateway down"), name="gowa")

    with pytest.raises(TransientChannelError):
        await send_reminder(
            reminder_id, tenant_id, db=db, channels=resolver(gowa), now=datetime.now(UTC)
        )

    assert await _status(db, tenant_id, reminder_id) == "pending"
    assert await _sent_messages(db, tenant_id) == []


async def _repeating(db: Database, tenant_id: UUID, due_at: datetime) -> UUID:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await reminders.add_reminder(
            session, tenant_id, text=TEXT, due_at=due_at, rule="FREQ=DAILY"
        )


async def _pending(db: Database, tenant_id: UUID) -> list[datetime]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return [
            r.due_at
            for r in await reminders.upcoming_reminders(
                session, tenant_id, after=datetime.min.replace(tzinfo=UTC)
            )
        ]


async def test_sent_repeating_reminder_schedules_next(db: Database) -> None:
    tenant_id = await _tenant(db)
    now = datetime.now(UTC).replace(microsecond=0)
    reminder_id = await _repeating(db, tenant_id, now)
    gowa = FakeOutbound(name="gowa")

    await send_reminder(reminder_id, tenant_id, db=db, channels=resolver(gowa), now=now)

    assert await _status(db, tenant_id, reminder_id) == "sent"
    assert await _pending(db, tenant_id) == [now + timedelta(days=1)]


async def test_failed_send_still_schedules_next(db: Database) -> None:
    tenant_id = await _tenant(db)
    now = datetime.now(UTC).replace(microsecond=0)
    reminder_id = await _repeating(db, tenant_id, now)
    gowa = FakeOutbound(RejectedChannelError(131026), name="gowa")

    await send_reminder(reminder_id, tenant_id, db=db, channels=resolver(gowa), now=now)

    assert await _status(db, tenant_id, reminder_id) == "failed"
    assert await _pending(db, tenant_id) == [now + timedelta(days=1)]


async def test_no_target_still_schedules_next(db: Database) -> None:
    tenant_id = await _tenant(db, inbound=False)
    now = datetime.now(UTC).replace(microsecond=0)
    reminder_id = await _repeating(db, tenant_id, now)
    gowa = FakeOutbound(name="gowa")

    await send_reminder(reminder_id, tenant_id, db=db, channels=resolver(gowa), now=now)

    assert await _status(db, tenant_id, reminder_id) == "failed"
    assert await _pending(db, tenant_id) == [now + timedelta(days=1)]


async def test_no_target_steps_in_the_tenants_zone(db: Database) -> None:
    """In UTC a Sunday 01:00 in Jerusalem is Saturday 23:00, and the next would be Monday."""
    tenant_id = await _tenant(db, inbound=False)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_timezone(session, tenant_id, "Asia/Jerusalem")
        due = datetime(2026, 11, 1, 1, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        reminder_id = await reminders.add_reminder(
            session, tenant_id, text=TEXT, due_at=due, rule="FREQ=WEEKLY;BYDAY=SU"
        )
    gowa = FakeOutbound(name="gowa")

    await send_reminder(reminder_id, tenant_id, db=db, channels=resolver(gowa), now=due)

    assert await _status(db, tenant_id, reminder_id) == "failed"
    assert await _pending(db, tenant_id) == [due + timedelta(days=7)]


async def test_rerun_schedules_one_next(db: Database) -> None:
    tenant_id = await _tenant(db)
    now = datetime.now(UTC).replace(microsecond=0)
    reminder_id = await _repeating(db, tenant_id, now)
    gowa = FakeOutbound(name="gowa")

    for _ in range(2):
        await send_reminder(reminder_id, tenant_id, db=db, channels=resolver(gowa), now=now)

    assert len(gowa.sent) == 1
    assert await _pending(db, tenant_id) == [now + timedelta(days=1)]


async def test_stale_occurrence_skips_to_next(db: Database) -> None:
    tenant_id = await _tenant(db)
    now = datetime.now(UTC).replace(microsecond=0)
    reminder_id = await _repeating(db, tenant_id, now - timedelta(hours=13))
    gowa = FakeOutbound(name="gowa")

    status = await send_reminder(reminder_id, tenant_id, db=db, channels=resolver(gowa), now=now)

    assert status is None
    assert gowa.attempts == []
    assert await _status(db, tenant_id, reminder_id) == "failed"
    [nxt] = await _pending(db, tenant_id)
    assert nxt > now
