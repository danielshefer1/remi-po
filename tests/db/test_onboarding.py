"""Onboarding over chat, end to end against Postgres, with fake channels.

Done-When (the chat half): an invited number writes, has its time zone set from its number,
and is sent a connect link. Also pinned:
- D2: who is served, onboarded, enrolled or turned away;
- D7: a known tenant's words end up under RLS and nowhere else;
- that no re-run of any job sends twice or advances twice.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.errors import (
    LLMRejectedError,
    LLMUnavailableError,
    TransientChannelError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import resolve_tenant
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging import inbound
from personal_organizer.messaging.inbound import acknowledge, handle_inbound
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.onboarding import (
    LINK_PLACEHOLDER,
    Advance,
    connect_text,
    guessed_text,
)
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.replies import ACK_TEXT, INVITE_ONLY_TEXT
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import ComposioSettings, Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.llm import NoLLM, ScriptedLLM, call, said
from tests.fixtures.tenants import (
    BASE_URL,
    IL_PHONE,
    NL_PHONE,
    US_PHONE,
    link_payload,
    links_of,
    messages_of,
    new_tenant,
    tenant_by_phone,
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

STRANGER_PHONE = "+447700900123"
UNINVITED_TENANT_PHONE = "+4915112345678"
INVITED = frozenset({IL_PHONE, NL_PHONE, US_PHONE})


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


@dataclass
class Person:
    """One person writing to the bot: each ``say`` stores a message as ingress would and runs
    its job as the worker would. Both channels are live, and replies follow the inbound one."""

    db: Database
    conn: Any
    settings: Settings
    phone: str | None = NL_PHONE
    user_id: str | None = None
    gowa: FakeOutbound = field(default_factory=lambda: FakeOutbound(name="gowa"))
    meta: FakeOutbound = field(default_factory=lambda: FakeOutbound(name="whatsapp"))
    agent: Spy = field(default_factory=Spy)
    last: UUID | None = None
    llm: Any = None
    last_attempt: bool = False

    async def say(
        self,
        body: str | None,
        *,
        via: str = "gowa",
        reply_id: str | None = None,
        message_type: str = "text",
        sent_at: datetime | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> str | None:
        self.last = await insert_inbox(
            self.conn,
            phone=self.phone,
            user_id=self.user_id,
            body=body,
            reply_id=reply_id,
            message_type=message_type,
            channel=via,
            sent_at=sent_at,
        )
        return await self.run(self.last, now=now)

    async def run(self, inbox_id: UUID, *, now: Callable[[], datetime] | None = None) -> str | None:
        return await handle_inbound(
            inbox_id,
            db=self.db,
            channels={self.gowa.name: self.gowa, self.meta.name: self.meta}.__getitem__,
            allowlist=INVITED,
            on_allowed=self.agent,
            settings=self.settings,
            llm=self.llm,
            last_attempt=self.last_attempt,
            now=now or (lambda: datetime.now(UTC)),
        )

    def texts(self) -> list[str]:
        return [message.body for message in self.gowa.sent + self.meta.sent]


@pytest.fixture
def person(db: Database, owner_conn: Any, onboarding_settings: Settings) -> Person:
    return Person(db, owner_conn, onboarding_settings)


async def _inbox(conn: Any, inbox_id: UUID | None) -> Any:
    return await conn.fetchrow("SELECT * FROM channel_inbox WHERE id = $1", inbox_id)


async def _kinds(conn: Any) -> list[str]:
    rows = await conn.fetch("SELECT kind FROM channel_outbox ORDER BY created_at")
    return [row["kind"] for row in rows]


def _fail_finishing_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The database goes away inside the transaction that finishes the row."""
    real = record_inbound
    calls: list[int] = []

    async def flaky(*args: Any, **kwargs: Any) -> UUID:
        calls.append(1)
        if len(calls) == 1:
            msg = "the database went away at the commit"
            raise ConnectionResetError(msg)
        return await real(*args, **kwargs)

    monkeypatch.setattr(inbound, "record_inbound", flaky)


class TestFirstMessage:
    async def test_an_invited_number_becomes_an_onboarding_tenant(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        person = Person(db, owner_conn, onboarding_settings, phone=IL_PHONE)
        assert await person.say("שלום!") == "onboarding"

        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert (tenant.status, tenant.onboarding_step, tenant.language, tenant.timezone) == (
            "onboarding",
            "connect",
            "he",
            "Asia/Jerusalem",
        )
        [text] = person.texts()
        assert text == guessed_text("he", "Asia/Jerusalem", _url(text), welcome=True)
        assert person.gowa.read != []
        assert person.agent.calls == []
        assert await _kinds(owner_conn) == ["onboarding:reply"]

    @pytest.mark.parametrize(
        ("body", "language"), [("hello", "en"), (None, "en"), ("hi שלום", "he")]
    )
    async def test_the_language_comes_from_the_words_not_the_number(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        body: str | None,
        language: str,
    ) -> None:
        person = Person(db, owner_conn, onboarding_settings, phone=IL_PHONE)
        await person.say(body, message_type="text" if body else "sticker")
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert tenant.language == language

    async def test_its_content_moves_under_rls(self, person: Person, db: Database) -> None:
        await person.say("Oncology appointment with Dr Meyer")
        row = await _inbox(person.conn, person.last)
        assert (row["body"], row["raw"], row["disposition"]) == (None, None, "onboarding")
        assert row["processed_at"] is not None
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        [message] = await messages_of(db, tenant.id)
        assert (message.direction, message.channel, message.inbox_id, message.body) == (
            "in",
            "gowa",
            person.last,
            "Oncology appointment with Dr Meyer",
        )

    async def test_a_tenant_left_by_a_crashed_job_still_gets_the_welcome(
        self, person: Person, db: Database
    ) -> None:
        """The previous run created the tenant and died: the re-run must welcome, not retry."""
        await new_tenant(db)
        await person.say("hi")
        [text] = person.texts()
        assert text == guessed_text("en", AMSTERDAM, _url(text), welcome=True)

    async def test_a_stale_first_message_creates_no_tenant(
        self, person: Person, db: Database
    ) -> None:
        sent_at = datetime.now(UTC) - timedelta(hours=30)
        assert await person.say("hi", sent_at=sent_at) == "stale"
        assert await tenant_by_phone(db, NL_PHONE) is None
        assert person.gowa.attempts == []
        assert (await _inbox(person.conn, person.last))["body"] is None


class TestComposioOff:
    async def test_iteration_02_exactly(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        """Before Composio is configured, invited senders keep the acknowledgement."""
        # Whatever the developer's .env says, Composio is off here.
        off = db_settings.model_copy(update={"composio": ComposioSettings()})
        channel = FakeOutbound(name="gowa")
        inbox_id = await insert_inbox(owner_conn)
        disposition = await handle_inbound(
            inbox_id,
            db=db,
            channels={"gowa": channel}.__getitem__,
            allowlist=INVITED,
            on_allowed=Spy(),
            settings=off,
        )
        assert disposition == "allowed"
        assert channel.sent == [OutboundMessage(recipient=NL_PHONE, body=ACK_TEXT)]
        assert await tenant_by_phone(db, NL_PHONE) is None
        assert (await _inbox(owner_conn, inbox_id))["body"] == "hi"


class TestAcknowledge:
    async def test_a_bsuid_only_active_tenant_gets_no_acknowledgement(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        """Pinned until Iteration 04: with no number to address, there is no reply."""
        channel = FakeOutbound(name="gowa")
        inbox_id = await insert_inbox(owner_conn, phone=None, user_id="US.1")
        row = await load_row(db, inbox_id)
        assert row is not None
        await acknowledge(row, channel, db=db)
        assert channel.sent == []
        assert channel.attempts == []


class TestStrangers:
    async def test_unchanged_with_composio_on(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        stranger = Person(db, owner_conn, onboarding_settings, phone=STRANGER_PHONE)
        assert await stranger.say("hello") == "stranger"
        assert stranger.texts() == [INVITE_ONLY_TEXT]
        assert stranger.agent.calls == []
        assert await tenant_by_phone(db, STRANGER_PHONE) is None
        assert (await _inbox(owner_conn, stranger.last))["body"] is None

    async def test_a_suspended_tenant_is_turned_away(self, person: Person, db: Database) -> None:
        await new_tenant(db, status="suspended")
        assert await person.say("hello") == "stranger"
        assert person.texts() == [INVITE_ONLY_TEXT]
        assert person.agent.calls == []
        assert (await _inbox(person.conn, person.last))["body"] is None


class TestZoneToConnect:
    async def test_the_whole_chat(self, person: Person, db: Database) -> None:
        assert await person.say("hi") == "onboarding"

        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == ("Europe/Amsterdam", "connect")
        first_link = link_payload(person.texts()[-1])
        assert first_link.tenant_id == tenant.id

        await person.say("did it work?")
        resent = person.texts()[-1]
        assert resent == t("connect_link", "en", url=resent.rsplit("\n", 1)[-1])
        assert link_payload(resent).nonce == first_link.nonce
        assert len(await links_of(db, tenant.id)) == 1
        assert await _kinds(person.conn) == ["onboarding:reply", "onboarding:reply"]
        assert len(await messages_of(db, tenant.id)) == 2

    async def test_a_city_replaces_the_guess(self, person: Person, db: Database) -> None:
        await person.say("hi")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == (AMSTERDAM, "connect")

        await person.say("London")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == ("Europe/London", "connect")

    async def test_a_used_or_aging_link_is_replaced(self, person: Person, db: Database) -> None:
        await person.say("hi")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        first = link_payload(person.texts()[-1]).nonce
        async with db.tenant_session(TenantId(tenant.id)) as session:
            await consume_link(session, tenant.id, nonce=first, now=datetime.now(UTC))

        await person.say("the link didn't work")
        second = link_payload(person.texts()[-1]).nonce
        assert second != first

        ttl = person.settings.onboarding.link_ttl_s
        later = datetime.now(UTC) + timedelta(seconds=ttl * 0.6)
        await person.say("still nothing", now=lambda: later)
        assert link_payload(person.texts()[-1]).nonce not in (first, second)


class TestReRuns:
    async def test_a_transient_failure_neither_advances_nor_double_sends(
        self, person: Person, db: Database
    ) -> None:
        await person.say("hi")
        person.gowa.failures.append(TransientChannelError("429"))
        with pytest.raises(TransientChannelError):
            await person.say("London")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert tenant.timezone == AMSTERDAM
        assert person.last is not None

        assert await person.run(person.last) == "onboarding"
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert tenant.timezone == "Europe/London"
        assert len(person.gowa.sent) == 2  # the welcome, then the new zone
        assert len(await links_of(db, tenant.id)) == 1

    async def test_a_crash_after_the_send_neither_resends_nor_advances_twice(
        self, person: Person, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review Focus 1: the link went out, then the job died before committing the step."""
        await person.say("hi")
        _fail_finishing_once(monkeypatch)
        with pytest.raises(ConnectionResetError):
            await person.say("London")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.onboarding_step, tenant.timezone) == ("connect", AMSTERDAM)
        assert (await _inbox(person.conn, person.last))["processed_at"] is None
        assert person.last is not None

        assert await person.run(person.last) == "onboarding"
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.onboarding_step, tenant.timezone) == ("connect", "Europe/London")
        assert len(person.gowa.sent) == 2
        assert await _kinds(person.conn) == ["onboarding:reply", "onboarding:connect"]
        assert len(await messages_of(db, tenant.id)) == 2

    async def test_a_crash_on_the_first_message_still_welcomes_once(
        self, person: Person, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crash after the welcome went out: the re-run is still a *first* message, because
        nothing was recorded, and so makes the same decision. One tenant, one welcome, and
        the guessed zone still set."""
        _fail_finishing_once(monkeypatch)
        with pytest.raises(ConnectionResetError):
            await person.say("hi")
        assert person.last is not None
        assert len(person.gowa.sent) == 1
        assert (await _inbox(person.conn, person.last))["processed_at"] is None
        assert (await _inbox(person.conn, person.last))["body"] == "hi"

        assert await person.run(person.last) == "onboarding"
        assert len(person.gowa.sent) == 1
        assert await _kinds(person.conn) == ["onboarding:reply"]
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.onboarding_step, tenant.timezone) == ("connect", AMSTERDAM)
        assert len(await messages_of(db, tenant.id)) == 1

    async def test_the_same_job_twice_advances_and_records_once(
        self, person: Person, db: Database
    ) -> None:
        await person.say("hi")
        await person.say("London")
        assert person.last is not None
        assert await person.run(person.last) == "onboarding"
        assert await person.run(person.last) == "onboarding"
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.onboarding_step, tenant.timezone) == ("connect", "Europe/London")
        assert len(person.gowa.sent) == 2
        assert await _kinds(person.conn) == ["onboarding:reply", "onboarding:connect"]
        assert len(await messages_of(db, tenant.id)) == 2

    async def test_an_overlapping_retry_records_and_advances_once(
        self, person: Person, db: Database
    ) -> None:
        """Two jobs that both loaded the row before either finished: the second to commit
        finds ``processed_at`` set, and writes nothing."""
        await person.say("hi")
        await person.say("London")
        assert person.last is not None
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        row = await load_row(db, person.last)
        assert row is not None
        # The state both overlapping jobs saw: a stale in-memory row, not yet finished.
        stale = replace(row, processed_at=None, disposition=None)
        assert len(await messages_of(db, tenant.id)) == 2

        await inbound._finish_known(
            db, stale, tenant.id, "onboarding", Advance(timezone="Asia/Jerusalem")
        )
        assert len(await messages_of(db, tenant.id)) == 2
        after = await tenant_by_phone(db, NL_PHONE)
        assert after is not None
        assert (after.onboarding_step, after.timezone) == ("connect", "Europe/London")

    async def test_a_finished_row_is_a_no_op(self, person: Person) -> None:
        await person.say("hi")
        assert person.last is not None
        assert await person.run(person.last) == "onboarding"
        assert len(person.gowa.sent) == 1


class TestActiveTenants:
    async def test_handed_to_the_agent_and_kept_under_rls(
        self, person: Person, db: Database
    ) -> None:
        tenant_id = await new_tenant(db, status="active")
        assert await person.say("Oncology appointment with Dr Meyer") == "allowed"
        assert [row.id for row in person.agent.calls] == [person.last]
        assert person.agent.calls[0].body == "Oncology appointment with Dr Meyer"
        assert person.gowa.read != []
        assert person.texts() == []  # the Spy stands in for the acknowledgement
        assert (await _inbox(person.conn, person.last))["body"] is None
        [message] = await messages_of(db, tenant_id)
        assert message.body == "Oncology appointment with Dr Meyer"

    async def test_identity_wins_over_the_invite_list(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Onboarded people stay served after their number leaves WHATSAPP__ALLOWED_PHONES."""
        await new_tenant(db, phone=UNINVITED_TENANT_PHONE, status="active")
        person = Person(db, owner_conn, onboarding_settings, phone=UNINVITED_TENANT_PHONE)
        assert await person.say("hello") == "allowed"
        assert len(person.agent.calls) == 1

    async def test_a_stale_message_is_kept_but_not_answered(
        self, person: Person, db: Database
    ) -> None:
        tenant_id = await new_tenant(db, status="active")
        sent_at = datetime.now(UTC) - timedelta(hours=30)
        assert await person.say("from yesterday", sent_at=sent_at) == "stale"
        assert person.agent.calls == []
        assert person.gowa.attempts == []
        assert (await _inbox(person.conn, person.last))["body"] is None
        assert [m.body for m in await messages_of(db, tenant_id)] == ["from yesterday"]


class TestOnePersonTwoChannels:
    async def test_enrolled_on_the_gateway_continued_on_meta(
        self, person: Person, db: Database
    ) -> None:
        await person.say("hi", via="gowa")
        await person.say("London", via="whatsapp")
        assert len(person.gowa.sent) == 1
        assert len(person.meta.sent) == 1  # the reply follows the inbound channel
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert tenant.onboarding_step == "connect"

    async def test_a_bsuid_then_a_hidden_number_stay_one_tenant(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 3, through the gate."""
        meta = Person(db, owner_conn, onboarding_settings, user_id="US.1")
        await meta.say("hi", via="whatsapp")
        hidden = Person(
            db, owner_conn, onboarding_settings, phone=None, user_id="US.1", meta=meta.meta
        )
        assert await hidden.say("ok", via="whatsapp") == "onboarding"
        gateway = Person(db, owner_conn, onboarding_settings, gowa=meta.gowa, meta=meta.meta)
        assert await gateway.say("hello", via="gowa") == "onboarding"

        async with db.system_session() as session:
            by_uid = await resolve_tenant(session, network=NETWORK_WHATSAPP, external_id="uid:US.1")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert by_uid == tenant.id
        assert tenant.onboarding_step == "connect"
        assert len(await messages_of(db, tenant.id)) == 3


class TestLogs:
    async def test_onboarding_logs_neither_who_nor_what_nor_the_link(
        self, person: Person, settings: Settings
    ) -> None:
        """Review Focus 4: the link is a bearer credential for the connect page."""
        stream = io.StringIO()
        configure_logging(settings, stream=stream)
        await person.say("Oncology appointment with Dr Meyer")
        await person.say("Mars")
        await person.say("London")
        await person.say("again please")

        captured = stream.getvalue()
        assert "inbound.handled" in captured
        token = person.texts()[-1].rsplit("/", 1)[-1]
        for leaked in (NL_PHONE, "31612345678", "Oncology appointment", "Mars", BASE_URL, token):
            assert leaked not in captured


AMSTERDAM = "Europe/Amsterdam"


def _url(text: str) -> str:
    return text.rsplit("\n", 1)[-1]


class TestTalkedThrough:
    """D10: with a model, the fixed-text branches answer what was said, and code still
    appends the zone line or the link and owns every state change."""

    async def test_an_off_script_first_message_gets_an_answer_and_the_guess(
        self, person: Person, db: Database
    ) -> None:
        person.llm = ScriptedLLM(said("I can, once your calendar is connected."))
        assert await person.say("can you add my dentist tomorrow?") == "onboarding"
        [text] = person.texts()
        url = _url(text)
        assert text == (
            f"I can, once your calendar is connected.\n\n{guessed_text('en', AMSTERDAM, url)}"
        )
        assert await _kinds(person.conn) == ["onboarding:reply"]
        [messages] = person.llm.calls
        assert AMSTERDAM in messages[0].content  # the step and the guess
        assert messages[-1] == Message("user", "can you add my dentist tomorrow?")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == (AMSTERDAM, "connect")
        assert [(m.direction, m.body) for m in await messages_of(db, tenant.id)] == [
            ("in", "can you add my dentist tomorrow?"),
            ("out", text.replace(url, LINK_PLACEHOLDER)),
        ]

    async def test_an_exact_city_never_calls_the_model(self, person: Person, db: Database) -> None:
        person.llm = ScriptedLLM(said("Welcome!"))
        await person.say("hi")
        person.llm = NoLLM()
        await person.say("London")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == ("Europe/London", "connect")
        assert await _kinds(person.conn) == ["onboarding:reply", "onboarding:connect"]
        # Recorded for the model's next turn, without the link itself.
        out = [m.body for m in await messages_of(db, tenant.id) if m.direction == "out"]
        assert out[-1] == connect_text("en", "Europe/London", LINK_PLACEHOLDER)

    async def test_an_unmatched_zone_message_is_answered_above_the_question(
        self, person: Person
    ) -> None:
        person.phone = US_PHONE  # no guess, so the zone step asks for a city
        person.llm = ScriptedLLM(said("Welcome!"), said("A personal organizer for your circle."))
        await person.say("hi")
        await person.say("what is this?")
        ask = t("zone_ask_city", "en")
        assert person.texts()[-1] == f"A personal organizer for your circle.\n\n{ask}"
        # The second turn read the first from history: the message, then our reply.
        assert person.llm.calls[1][1:3] == [
            Message("user", "hi"),
            Message("assistant", f"Welcome!\n\n{ask}"),
        ]

    async def test_no_blank_turn_reaches_the_model(self, person: Person) -> None:
        """The API refuses a turn with no text in it, now or in the history."""
        person.llm = ScriptedLLM(said("Welcome!"), said("Which city?"), said("Which city?"))
        await person.say("hi")
        await person.say(" \n ")
        await person.say("Mars")
        assert person.llm.calls[1][-1] == Message("user", "[text]")
        assert all(m.content.strip() for m in person.llm.calls[2])

    async def test_set_timezone_takes_the_confirm_path(self, person: Person, db: Database) -> None:
        person.llm = ScriptedLLM(
            said("Welcome!"),
            said("", call("set_timezone", zone="America/New_York")),
            said("I've used New York, where you are this month."),
        )
        await person.say("hi")
        await person.say("I live in Tel Aviv but I'm in New York this month")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == ("America/New_York", "connect")
        reply = person.texts()[-1]
        url = reply.rsplit("\n", 1)[-1]
        assert reply == (
            "I've used New York, where you are this month.\n\n"
            + connect_text("en", "America/New_York", url)
        )
        assert link_payload(reply).tenant_id == tenant.id
        recorded = [m.body for m in await messages_of(db, tenant.id)][-1]
        assert recorded is not None
        assert url not in recorded
        assert LINK_PLACEHOLDER in recorded

    async def test_a_zone_that_is_not_one_changes_nothing(
        self, person: Person, db: Database
    ) -> None:
        person.phone = US_PHONE
        person.llm = ScriptedLLM(
            said("Welcome!"),
            said("", call("set_timezone", zone="Mars/Olympus_Mons")),
            said("Which city are you in?"),
        )
        await person.say("hi")
        await person.say("Mars")
        assert person.llm.calls[2][-1].content.startswith("error:")
        tenant = await tenant_by_phone(db, US_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == (None, "zone")

    async def test_set_language_switches_the_reply_and_the_tenant(
        self, person: Person, db: Database
    ) -> None:
        person.phone = US_PHONE
        person.llm = ScriptedLLM(
            said("Welcome!"), said("", call("set_language", language="he")), said("בשמחה.")
        )
        await person.say("hi")
        await person.say("please speak Hebrew")
        assert person.texts()[-1] == f"בשמחה.\n\n{t('zone_ask_city', 'he')}"
        tenant = await tenant_by_phone(db, US_PHONE)
        assert tenant is not None
        assert tenant.language == "he"

    async def test_any_connect_step_message_gets_an_answer_and_the_link(
        self, person: Person, db: Database
    ) -> None:
        await person.say("hi")
        person.llm = ScriptedLLM(said("Yes: Google shows a warning because the app is private."))
        await person.say("is this safe?")
        reply = person.texts()[-1]
        url = reply.rsplit("\n", 1)[-1]
        assert reply == (
            "Yes: Google shows a warning because the app is private.\n\n"
            + t("connect_link", "en", url=url)
        )
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert link_payload(reply).tenant_id == tenant.id
        assert (await _kinds(person.conn))[-1] == "onboarding:reply"

    @pytest.mark.parametrize(
        "written",
        ["Open https://evil.example/x", f"Tap {LINK_PLACEHOLDER} below", "  ", "x" * 5000],
    )
    async def test_a_url_an_empty_or_an_overlong_reply_is_dropped(
        self, person: Person, written: str
    ) -> None:
        await person.say("hi")
        person.llm = ScriptedLLM(said(written))
        await person.say("the link doesn't work")
        reply = person.texts()[-1]
        assert reply == t("connect_link", "en", url=reply.rsplit("\n", 1)[-1])

    async def test_a_model_that_is_down_is_retried_then_falls_back(
        self, person: Person, db: Database
    ) -> None:
        """D3: raise for the job's retry; on the last attempt, today's text, never silence."""
        person.llm = ScriptedLLM(LLMUnavailableError("down"), LLMUnavailableError("down"))
        with pytest.raises(LLMUnavailableError):
            await person.say("hi")
        assert person.gowa.attempts == []
        assert (await _inbox(person.conn, person.last))["processed_at"] is None
        assert person.last is not None

        person.last_attempt = True
        assert await person.run(person.last) == "onboarding"
        [text] = person.texts()
        assert text == guessed_text("en", AMSTERDAM, _url(text), welcome=True)
        assert await _kinds(person.conn) == ["onboarding:reply"]
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == (AMSTERDAM, "connect")

    async def test_a_refusal_falls_back_at_once(self, person: Person) -> None:
        person.phone = US_PHONE
        await person.say("hi")
        person.llm = ScriptedLLM(LLMRejectedError(None))
        await person.say("Mars")
        assert person.texts()[-1] == t("zone_retry", "en")

    async def test_a_re_run_that_decides_differently_sends_nothing_new(
        self, person: Person, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-run doesn't ask the model again: what it decided would not be what was sent.
        The guessed zone needs no model, so the re-run still sets it."""
        person.llm = ScriptedLLM(
            said("First answer."),
            said("", call("set_timezone", zone="America/New_York")),
            said("Second answer."),
        )
        _fail_finishing_once(monkeypatch)
        with pytest.raises(ConnectionResetError):
            await person.say("what is this?")
        assert person.last is not None
        assert await person.run(person.last) == "onboarding"
        [text] = person.texts()
        assert text == f"First answer.\n\n{guessed_text('en', AMSTERDAM, _url(text))}"
        assert await _kinds(person.conn) == ["onboarding:reply"]
        assert len(person.llm.calls) == 1
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == (AMSTERDAM, "connect")
        assert [(m.direction, m.body) for m in await messages_of(db, tenant.id)] == [
            ("in", "what is this?")
        ]

    @pytest.mark.parametrize("before", ["hi", "Mars", "did it work?"])
    async def test_the_agent_turned_on_between_send_and_commit_sends_nothing_new(
        self, person: Person, monkeypatch: pytest.MonkeyPatch, before: str
    ) -> None:
        """Welcome, unmatched zone and connect resend: one kind with or without the model."""
        if before == "Mars":
            person.phone = US_PHONE  # no guess, so "Mars" is an unmatched zone message
        if before != "hi":
            await person.say("hi")
        sent = len(person.texts())
        _fail_finishing_once(monkeypatch)
        with pytest.raises(ConnectionResetError):
            await person.say(before)
        assert person.last is not None
        person.llm = NoLLM()
        assert await person.run(person.last) == "onboarding"
        assert len(person.texts()) == sent + 1
