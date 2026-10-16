"""Voice notes through the tenant gate (Iteration 06), with a fake download and transcriber.

Done-When: a member's or invitee's note is handled as its words everywhere and recorded so; a
stranger's is never fetched; one that can't be heard gets one warm line, never a guess.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.errors import (
    TranscriptionRefusedError,
    TranscriptionUnavailableError,
)
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import pending
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.messaging import inbound
from personal_organizer.messaging.agent import agent_turn
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState, load_state
from personal_organizer.providers.calendar.memory import InMemoryCalendar
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.llm import NoLLM, ScriptedLLM, call, said
from tests.fixtures.tenants import IL_PHONE, messages_of, new_tenant, tenant_by_phone
from tests.fixtures.voice import FakeTranscriber, ListeningOutbound

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


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


def _fail_finishing_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job dies in the transaction that finishes the row, after its sends."""
    real, calls = record_inbound, []

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError("the database went away at the commit")
        return await real(*args, **kwargs)

    monkeypatch.setattr(inbound, "record_inbound", flaky)


async def _handle(
    db: Database,
    settings: Settings,
    inbox_id: UUID,
    channel: FakeOutbound,
    *,
    on_allowed: Any,
    transcriber: Any,
    allowlist: frozenset[str] = frozenset(),
    llm: Any = None,
    last_attempt: bool = False,
) -> str | None:
    return await handle_inbound(
        inbox_id,
        db=db,
        channels={channel.name: channel}.__getitem__,
        allowlist=allowlist,
        on_allowed=on_allowed,
        settings=settings,
        llm=llm,
        transcriber=transcriber,
        last_attempt=last_attempt,
        now=lambda: NOW,
    )


class TestActiveMember:
    async def test_a_voice_note_is_answered_as_text_and_recorded(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant = await _active(db)
        channel = ListeningOutbound()
        llm = ScriptedLLM(said("בטח, אזכיר לך."))
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        disposition = await _handle(
            db,
            onboarding_settings,
            inbox_id,
            channel,
            on_allowed=functools.partial(agent_turn, db=db, llm=llm, calendar=InMemoryCalendar()),
            transcriber=FakeTranscriber("תזכיר לי מחר בשש להתקשר לאמא"),
        )
        assert disposition == "allowed"
        assert llm.calls[0][-1].content == "תזכיר לי מחר בשש להתקשר לאמא"
        assert [m.body for m in channel.sent] == ["בטח, אזכיר לך."]
        assert [
            (m.direction, m.message_type, m.body) for m in await messages_of(db, tenant.id)
        ] == [
            ("in", "audio", "תזכיר לי מחר בשש להתקשר לאמא"),
            ("out", "text", "בטח, אזכיר לך."),
        ]

    async def test_an_unheard_note_gets_the_fixed_line_and_no_model_call(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant = await _active(db)
        channel = ListeningOutbound()
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        disposition = await _handle(
            db,
            onboarding_settings,
            inbox_id,
            channel,
            on_allowed=functools.partial(
                agent_turn, db=db, llm=NoLLM(), calendar=InMemoryCalendar()
            ),
            transcriber=FakeTranscriber(""),
        )
        assert disposition == "allowed"
        assert [m.body for m in channel.sent] == [t("voice_unheard", "he")]
        bodies = [(m.direction, m.body) for m in await messages_of(db, tenant.id)]
        assert bodies == [("in", None), ("out", t("voice_unheard", "he"))]

    async def test_a_re_run_of_an_unheard_note_sends_nothing_new(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The job dies in the transaction that finishes the row, after the send."""
        tenant = await _active(db)
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        real, calls = record_inbound, []

        async def flaky(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError("the database went away at the commit")
            return await real(*args, **kwargs)

        monkeypatch.setattr(inbound, "record_inbound", flaky)
        refused = FakeTranscriber(error=TranscriptionRefusedError("no"))
        first, again = ListeningOutbound(), ListeningOutbound()
        with pytest.raises(ConnectionResetError):
            await _handle(
                db, onboarding_settings, inbox_id, first, on_allowed=Spy(), transcriber=refused
            )
        await _handle(
            db, onboarding_settings, inbox_id, again, on_allowed=Spy(), transcriber=refused
        )
        assert len(first.sent) == 1
        assert again.attempts == []
        assert [m.direction for m in await messages_of(db, tenant.id)] == ["in", "out"]

    async def test_a_note_heard_then_unheard_on_the_re_run_sends_one_reply(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tenant = await _active(db)
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        _fail_finishing_once(monkeypatch)
        channel = ListeningOutbound()
        agent = functools.partial(
            agent_turn, db=db, llm=ScriptedLLM(said("בטח.")), calendar=InMemoryCalendar()
        )
        with pytest.raises(ConnectionResetError):
            await _handle(
                db,
                onboarding_settings,
                inbox_id,
                channel,
                on_allowed=agent,
                transcriber=FakeTranscriber("תזכיר לי"),
            )
        await _handle(
            db,
            onboarding_settings,
            inbox_id,
            channel,
            on_allowed=Spy(),
            transcriber=FakeTranscriber(""),
        )
        assert [m.body for m in channel.sent] == ["בטח."]
        # The re-run records only the message: agent_turn's own re-run path returns no reply.
        assert [m.direction for m in await messages_of(db, tenant.id)] == ["in"]

    async def test_a_note_unheard_then_heard_on_the_re_run_sends_one_reply(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tenant = await _active(db)
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        _fail_finishing_once(monkeypatch)
        channel = ListeningOutbound()
        with pytest.raises(ConnectionResetError):
            await _handle(
                db,
                onboarding_settings,
                inbox_id,
                channel,
                on_allowed=Spy(),
                transcriber=FakeTranscriber(""),
            )
        llm = NoLLM()
        await _handle(
            db,
            onboarding_settings,
            inbox_id,
            channel,
            on_allowed=functools.partial(agent_turn, db=db, llm=llm, calendar=InMemoryCalendar()),
            transcriber=FakeTranscriber("תזכיר לי"),
        )
        assert [m.body for m in channel.sent] == [t("voice_unheard", "he")]
        assert [m.direction for m in await messages_of(db, tenant.id)] == ["in", "out"]

    async def test_a_spoken_yes_confirms_the_pending_event(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """The confirm reads the transcript: ``decide`` gets the words, not ``[audio]``."""
        tenant = await _active(db)
        calendar = InMemoryCalendar()
        channel = ListeningOutbound()  # one channel: its fake message ids are unique per instance
        dentist = call(
            "propose_event", title="רופא שיניים", start="2026-10-13T09:00", end="2026-10-13T10:00"
        )
        typed = await insert_inbox(owner_conn, phone=IL_PHONE, body="תוסיף רופא שיניים מחר")
        await _handle(
            db,
            onboarding_settings,
            typed,
            channel,
            on_allowed=functools.partial(
                agent_turn,
                db=db,
                llm=ScriptedLLM(said("", dentist), said("להוסיף?")),
                calendar=calendar,
            ),
            transcriber=FakeTranscriber("unused"),
        )
        async with db.tenant_session(TenantId(tenant.id)) as session:
            assert await pending.current(session, tenant.id, now=NOW) is not None

        confirm = ScriptedLLM(said("ADD"))
        spoken = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        await _handle(
            db,
            onboarding_settings,
            spoken,
            channel,
            on_allowed=functools.partial(agent_turn, db=db, llm=confirm, calendar=calendar),
            transcriber=FakeTranscriber("כן, תוסיף"),
        )
        [[_system, user]] = confirm.calls
        assert user.content == "כן, תוסיף"
        [event] = calendar.events
        assert event.title == "רופא שיניים"

    async def test_an_outage_raises_for_a_retry_then_falls_back(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _active(db)
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        down = FakeTranscriber(error=TranscriptionUnavailableError("down"))
        with pytest.raises(TranscriptionUnavailableError):
            await _handle(
                db,
                onboarding_settings,
                inbox_id,
                ListeningOutbound(),
                on_allowed=Spy(),
                transcriber=down,
            )
        channel = ListeningOutbound()
        await _handle(
            db,
            onboarding_settings,
            inbox_id,
            channel,
            on_allowed=Spy(),
            transcriber=down,
            last_attempt=True,
        )
        assert [m.body for m in channel.sent] == [t("voice_unheard", "he")]


class TestStrangersAndInvitees:
    async def test_a_strangers_audio_is_never_fetched(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        channel, transcriber = ListeningOutbound(), FakeTranscriber("hi")
        inbox_id = await insert_inbox(
            owner_conn, phone="+972500000001", body=None, message_type="audio", sent_at=NOW
        )
        disposition = await _handle(
            db, onboarding_settings, inbox_id, channel, on_allowed=Spy(), transcriber=transcriber
        )
        assert disposition == "stranger"
        assert channel.downloads == []
        assert transcriber.calls == []

    async def test_an_invitees_first_voice_note_sets_the_language(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        await _handle(
            db,
            onboarding_settings,
            inbox_id,
            ListeningOutbound(),
            on_allowed=Spy(),
            transcriber=FakeTranscriber("hello there, I got your invite"),
            allowlist=frozenset({IL_PHONE}),
        )
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert tenant.language == "en"  # from the words, though the number is Israeli

    async def test_an_invitees_unheard_note_still_gets_the_welcome(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        channel = ListeningOutbound()
        inbox_id = await insert_inbox(
            owner_conn, phone=IL_PHONE, body=None, message_type="audio", sent_at=NOW
        )
        disposition = await _handle(
            db,
            onboarding_settings,
            inbox_id,
            channel,
            on_allowed=Spy(),
            transcriber=FakeTranscriber(""),
            allowlist=frozenset({IL_PHONE}),
        )
        assert disposition == "onboarding"
        assert len(channel.sent) == 1  # the onboarding reply, not voice_unheard
        assert t("voice_unheard", "en") not in channel.sent[0].body
