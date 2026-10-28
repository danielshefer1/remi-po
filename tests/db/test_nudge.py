"""Follow-ups (Iteration 07): one warm private line a day until they answer the outing.

Done-When: a member who okayed sharing and hasn't answered gets at most one follow-up a day,
written by the model or, when it is down on the last attempt, the fixed line; none once they
answered, the outing started or the poll closed; and an answer said privately to the agent
stops them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from personal_organizer.core.errors import LLMUnavailableError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.outings import create_poll, set_answer
from personal_organizer.interfaces.calendar import CalendarEvent
from personal_organizer.messaging.agent import respond
from personal_organizer.messaging.calendar import InMemoryTenantCalendar
from personal_organizer.messaging.groups.answers import TenantOutings
from personal_organizer.messaging.groups.nudge import NUDGE_KIND, nudge_key, send_nudge
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import load_state
from personal_organizer.settings import Settings
from tests.db.test_handle_inbound import FakeOutbound
from tests.db.test_onboarding_connected import outbox, resolver
from tests.db.test_outings import JID, NOA, NOW, _answer, _member
from tests.fixtures.connect import seed_inbound
from tests.fixtures.llm import NoLLM, ScriptedLLM, call, said
from tests.fixtures.tenants import messages_of

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

ZONE = ZoneInfo("Asia/Jerusalem")
DINNER = CalendarEvent(
    "",
    "Family dinner",
    datetime(2026, 10, 23, 19, tzinfo=ZONE),
    datetime(2026, 10, 23, 21, tzinfo=ZONE),
)
WARM = "Hey Noa, are you joining Friday's family dinner? No pressure, just let me know."


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _outing(db: Database, owner_conn: Any) -> tuple[UUID, UUID]:
    """A dinner poll posted three hours before ``NOW``, and Noa, sharing, who hasn't answered."""
    async with db.system_session() as session:
        group = await groups.ensure_group(session, channel="gowa", jid=JID)
        poll = await create_poll(
            session,
            group_id=group.id,
            inbox_id=uuid4(),
            event=DINNER,
            language="en",
            in_option="I'm in",
            out_option="Can't make it",
        )
    await owner_conn.execute("UPDATE group_polls SET created_at = $1", NOW - timedelta(hours=3))
    noa = await _member(db, group.id, NOA, "Noa", "ca_noa")
    await seed_inbound(db, noa, channel="gowa", sent_at=NOW - timedelta(days=1), phone=NOA)
    return poll.id, noa


async def _due(db: Database, now: datetime) -> list[tuple[UUID, UUID]]:
    async with db.system_session() as session:
        return await groups.due_nudges(session, now)


async def test_nudge_sent_once_per_day(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    gowa = FakeOutbound(name="gowa")
    llm = ScriptedLLM(said(WARM))

    status = await send_nudge(
        poll, noa, db=db, channels=resolver(gowa), llm=llm, now=NOW, last_attempt=False
    )

    assert status == "accepted"
    [message] = gowa.sent
    assert (message.recipient, message.body) == (NOA, WARM)
    system, data = llm.calls[0]
    assert "Family dinner" in data.content
    assert "Noa" in data.content
    assert "first" in system.content
    [row] = await outbox(owner_conn)
    assert (row["kind"], row["idempotency_key"]) == (
        NUDGE_KIND,
        nudge_key(poll, noa, date(2026, 10, 21)),
    )
    # The private reply has the context: the line is in their history.
    assert [(m.direction, m.body) for m in await messages_of(db, noa)][-1] == ("out", WARM)
    answer = await _answer(db, noa)
    assert answer is not None
    assert answer.nudged_at == NOW
    assert (poll, noa) not in await _due(db, NOW + timedelta(hours=1))

    # A second run the same day sends nothing and calls no model.
    again = await send_nudge(
        poll,
        noa,
        db=db,
        channels=resolver(gowa),
        llm=NoLLM(),
        now=NOW + timedelta(hours=1),
        last_attempt=False,
    )
    assert again is None
    assert len(gowa.sent) == 1

    # The next day it is due again, and the model knows it asked before.
    tomorrow = NOW + timedelta(days=1)
    assert (poll, noa) in await _due(db, tomorrow)
    later = ScriptedLLM(said("Hi again Noa, any thoughts on Friday's dinner?"))
    await send_nudge(
        poll, noa, db=db, channels=resolver(gowa), llm=later, now=tomorrow, last_attempt=False
    )
    assert len(gowa.sent) == 2
    assert "first" not in later.calls[0][0].content


async def test_nudge_skipped_after_answer(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    async with db.tenant_session(TenantId(noa)) as session:
        await set_answer(session, noa, poll, answer=None, reason="working late")
    gowa = FakeOutbound(name="gowa")

    status = await send_nudge(
        poll, noa, db=db, channels=resolver(gowa), llm=NoLLM(), now=NOW, last_attempt=False
    )

    assert status is None
    assert gowa.sent == []


async def test_nudge_skipped_once_they_stop_sharing(db: Database, owner_conn: Any) -> None:
    """Due when the tick scanned, but they stopped sharing before the job ran."""
    poll, noa = await _outing(db, owner_conn)
    async with db.tenant_session(TenantId(noa)) as session:
        [group] = await groups.groups_of(session, noa)
        await groups.set_sharing(session, noa, group, False)
    gowa = FakeOutbound(name="gowa")

    status = await send_nudge(
        poll, noa, db=db, channels=resolver(gowa), llm=NoLLM(), now=NOW, last_attempt=False
    )

    assert status is None
    assert gowa.sent == []


async def test_nudge_skipped_after_event_start(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    gowa = FakeOutbound(name="gowa")
    started = DINNER.start + timedelta(minutes=1)

    assert (
        await send_nudge(
            poll, noa, db=db, channels=resolver(gowa), llm=NoLLM(), now=started, last_attempt=False
        )
        is None
    )
    await owner_conn.execute("UPDATE group_polls SET status = 'closed'")
    assert (
        await send_nudge(
            poll, noa, db=db, channels=resolver(gowa), llm=NoLLM(), now=NOW, last_attempt=False
        )
        is None
    )
    assert gowa.sent == []


async def test_nudge_fallback_on_last_attempt(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    gowa = FakeOutbound(name="gowa")

    with pytest.raises(LLMUnavailableError):
        await send_nudge(
            poll,
            noa,
            db=db,
            channels=resolver(gowa),
            llm=ScriptedLLM(LLMUnavailableError("down")),
            now=NOW,
            last_attempt=False,
        )
    assert gowa.sent == []

    status = await send_nudge(
        poll,
        noa,
        db=db,
        channels=resolver(gowa),
        llm=ScriptedLLM(LLMUnavailableError("down")),
        now=NOW,
        last_attempt=True,
    )

    assert status == "accepted"
    [message] = gowa.sent
    summary = "Family dinner, Friday 2026-10-23, 19:00-21:00"
    assert message.body == t("outing_nudge_fallback", "en", summary=summary)


async def test_nudge_with_a_link_falls_back(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    gowa = FakeOutbound(name="gowa")
    llm = ScriptedLLM(said("Coming? Details at https://example.com"))

    await send_nudge(
        poll, noa, db=db, channels=resolver(gowa), llm=llm, now=NOW, last_attempt=False
    )

    [message] = gowa.sent
    assert "http" not in message.body
    assert "Family dinner" in message.body


async def test_answer_outing_privately_stops_nudges(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    tenant = await load_state(db, noa)
    assert tenant is not None
    llm = ScriptedLLM(
        said("", call("answer_outing", n=1, coming=False, reason="working late")),
        said("Got it, I'll count you out of Friday's dinner."),
    )

    reply = await respond(
        [],
        "I can't make it on Friday, I'm working late",
        tenant,
        llm=llm,
        calendar=InMemoryTenantCalendar(),
        now=NOW,
        outings=TenantOutings(db, noa, NOW),
    )

    assert reply == "Got it, I'll count you out of Friday's dinner."
    assert "Family dinner" in llm.calls[0][0].content
    assert llm.calls[1][-1].content.startswith("ok")
    answer = await _answer(db, noa)
    assert answer is not None
    assert (answer.answer, answer.reason) == ("out", "working late")
    assert (poll, noa) not in await _due(db, NOW)


async def test_answer_outing_coming_adds_nothing(db: Database, owner_conn: Any) -> None:
    poll, noa = await _outing(db, owner_conn)
    tenant = await load_state(db, noa)
    assert tenant is not None
    calendar = InMemoryTenantCalendar()
    llm = ScriptedLLM(
        said("", call("answer_outing", n=1, coming=True)),
        said("Great! Vote I'm in on the poll and it goes in your calendar."),
    )

    await respond(
        [],
        "yes I'm coming",
        tenant,
        llm=llm,
        calendar=calendar,
        now=NOW,
        outings=TenantOutings(db, noa, NOW),
    )

    answer = await _answer(db, noa)
    assert answer is not None
    assert (answer.answer, answer.reason) == ("in", None)
    assert calendar.added == []
    assert (poll, noa) not in await _due(db, NOW)


async def test_answer_outing_without_outings(db: Database, owner_conn: Any) -> None:
    _, noa = await _outing(db, owner_conn)
    tenant = await load_state(db, noa)
    assert tenant is not None
    llm = ScriptedLLM(said("", call("answer_outing", n=1, coming=True)), said("Hmm."))

    await respond([], "yes", tenant, llm=llm, calendar=InMemoryTenantCalendar(), now=NOW)

    assert llm.calls[1][-1].content == "error: outings are not available here"
    assert "Family dinner" not in llm.calls[0][0].content
