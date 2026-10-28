"""The connect flow against Postgres, with a fake Composio and a fake queue."""

from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from structlog.testing import capture_logs

from personal_organizer.api.app import create_app
from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import CalendarConnection, OnboardingLink, Tenant
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.links import REPLAY_WINDOW
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.onboarding.connected import announce_connected
from personal_organizer.onboarding.page_text import page_text
from personal_organizer.onboarding.tokens import LINK_SALT, STATE_SALT, verify
from personal_organizer.providers.calendar.composio import ComposioConnector
from personal_organizer.settings import Settings
from personal_organizer.worker.tasks.onboarding import ONBOARDING_CONNECTED_TASK
from tests.api.conftest import DeferredCall, FakeProcrastinate
from tests.db.test_handle_inbound import FakeOutbound
from tests.fixtures.connect import (
    ACCOUNT_ID,
    AUTH_CONFIG_ID,
    BASE_URL,
    LINK_SECRET,
    REDIRECT_URL,
    TRUNCATE_CONNECT_TABLES,
    FakeConnectLinker,
    account,
    assert_hardened,
    assert_page,
    connect_app,
    link_token,
    make_active,
    seed_inbound,
    seed_link,
    seed_tenant,
    sentry_events,
    stale,
    state_token,
    suspend,
    with_connect,
)

pytestmark = [pytest.mark.db]


@pytest.fixture
def connect_settings(db_settings: Settings) -> Settings:
    return with_connect(db_settings)


@pytest.fixture
async def db(connect_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(connect_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


@pytest.fixture
def linker() -> FakeConnectLinker:
    return FakeConnectLinker()


@pytest.fixture
def queue() -> FakeProcrastinate:
    return FakeProcrastinate()


@pytest.fixture
async def http(
    connect_settings: Settings, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
) -> AsyncIterator[AsyncClient]:
    application = connect_app(connect_settings, db=db, linker=linker, queue=queue)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestConnectPage:
    async def test_it_speaks_the_tenants_language(self, http: AsyncClient, db: Database) -> None:
        tenant_id = await seed_tenant(db, language="he")
        response = await http.get(f"/connect/{await seed_link(db, tenant_id)}")
        assert response.status_code == 200
        assert '<html lang="he" dir="rtl">' in response.text
        assert page_text("connect", "he")["button"] in html.unescape(response.text)
        assert "4567" in response.text  # the last digits of PHONE
        assert_hardened(response)

    async def test_english_reads_left_to_right(self, http: AsyncClient, db: Database) -> None:
        tenant_id = await seed_tenant(db, language="en")
        response = await http.get(f"/connect/{await seed_link(db, tenant_id)}")
        assert '<html lang="en" dir="ltr">' in response.text
        assert page_text("connect", "en")["button"] in html.unescape(response.text)

    async def test_previews_do_not_spend_the_link(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """WhatsApp fetches a link to build its preview. Only the button's POST may spend it."""
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)
        for _ in range(3):
            await http.get(f"/connect/{token}")
        await http.head(f"/connect/{token}")
        response = await http.post(f"/connect/{token}")
        assert response.status_code == 303
        assert len(linker.links) == 1

    async def test_a_get_writes_nothing(self, http: AsyncClient, db: Database) -> None:
        """The link row is untouched after GETs: not used, and not updated in any column."""
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)

        async def snapshot() -> list[tuple[Any, ...]]:
            async with db.tenant_session(TenantId(tenant_id)) as session:
                rows = await session.execute(
                    select(
                        OnboardingLink.nonce,
                        OnboardingLink.used_at,
                        OnboardingLink.expires_at,
                        Tenant.status,
                        Tenant.onboarding_step,
                        Tenant.updated_at,
                    ).join(Tenant, Tenant.id == OnboardingLink.tenant_id)
                )
                return [tuple(row) for row in rows]

        before = await snapshot()
        for _ in range(3):
            assert (await http.get(f"/connect/{token}")).status_code == 200
        after = await snapshot()
        assert after == before
        assert after[0][1] is None

    @pytest.mark.parametrize("age", ["spent", "past_its_ttl"])
    async def test_a_tenant_already_connected_is_told_so(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, age: str
    ) -> None:
        """An old link tapped after connecting: not "this link has expired, send a message for
        a new one", which they don't need."""
        tenant_id = await seed_tenant(db, language="en")
        token = await seed_link(db, tenant_id)
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_bound", auth_config_id=AUTH_CONFIG_ID
            )
            if age == "spent":
                spent_at = datetime.now(UTC) - REPLAY_WINDOW - timedelta(seconds=1)
                await session.execute(update(OnboardingLink).values(used_at=spent_at))
        if age == "past_its_ttl":
            token = stale(lambda: link_token(tenant_id))
        await make_active(db, tenant_id)
        for response in (await http.get(f"/connect/{token}"), await http.post(f"/connect/{token}")):
            assert response.status_code == 200
            assert_page(response, "already_connected", "en")
            assert page_text("already_connected", "he")["title"] not in response.text
            assert_hardened(response)
        assert linker.links == []

    @pytest.mark.parametrize("connected", [False, True])
    async def test_an_active_tenant_with_a_fresh_link_connects(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, connected: bool
    ) -> None:
        """The calendar is optional, so setup ends active: the link connects them later, and
        a fresh one reconnects a calendar that stopped working."""
        tenant_id = await seed_tenant(db, language="en")
        token = await seed_link(db, tenant_id)
        await make_active(db, tenant_id)
        if connected:
            async with db.tenant_session(TenantId(tenant_id)) as session:
                await bind_connection(
                    session, tenant_id, connected_account_id="ca_old", auth_config_id=AUTH_CONFIG_ID
                )
        page = await http.get(f"/connect/{token}")
        assert page.status_code == 200
        assert page_text("connect", "en")["button"] in page.text
        response = await http.post(f"/connect/{token}")
        assert response.status_code == 303
        assert response.headers["location"] == REDIRECT_URL
        assert len(linker.links) == 1

    async def test_the_page_shows_apple_users_how_to_sync_through_google(
        self, http: AsyncClient, db: Database
    ) -> None:
        tenant_id = await seed_tenant(db, language="en")
        token = await seed_link(db, tenant_id)
        response = await http.get(f"/connect/{token}")
        text = page_text("connect", "en")
        for key in ("apple", "apple_step_1", "apple_step_2", "apple_step_3", "apple_after"):
            assert text[key] in html.unescape(response.text)
        # The Apple path ends in the same Google connect: two buttons, one action.
        assert response.text.count(f'action="/connect/{token}"') == 2
        assert_hardened(response)

    async def test_an_expired_link_of_a_tenant_still_onboarding_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        await seed_link(db, tenant_id)
        token = stale(lambda: link_token(tenant_id))
        for response in (await http.get(f"/connect/{token}"), await http.post(f"/connect/{token}")):
            assert response.status_code == 410
            assert_page(response, "link_unusable", "he", "en")
        assert linker.links == []


class TestStart:
    async def test_the_button_goes_to_composio(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        response = await http.post(f"/connect/{await seed_link(db, tenant_id)}")
        assert response.status_code == 303
        assert response.headers["location"] == REDIRECT_URL
        assert_hardened(response)
        [call] = linker.links
        assert call["user_id"] == str(tenant_id)  # the tenant UUID, never a phone number
        assert call["auth_config_id"] == AUTH_CONFIG_ID
        callback = urlsplit(call["callback_url"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == (
            f"{BASE_URL}/connect/callback"
        )
        [state] = parse_qs(callback.query)["state"]
        payload = verify(state, secret=LINK_SECRET, salt=STATE_SALT, max_age_s=60)
        assert payload is not None
        assert payload.tenant_id == tenant_id
        # A state is not a link: the salts keep the two kinds of token apart.
        assert verify(state, secret=LINK_SECRET, salt=LINK_SALT, max_age_s=60) is None

    async def test_a_resent_post_still_goes_to_composio(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """Desktop Chrome re-sent the POST ~0.5s after our 303 and showed the second answer."""
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        first, second = await http.post(url), await http.post(url)
        assert (first.status_code, second.status_code) == (303, 303)
        assert len(linker.links) == 2

    async def test_two_taps_at_once_both_redirect(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        responses = await asyncio.gather(http.post(url), http.post(url))
        assert [r.status_code for r in responses] == [303, 303]

    async def test_a_link_used_past_the_window_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """Reopened from the chat later: the page says so up front, not after the tap."""
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        assert (await http.post(url)).status_code == 303
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await session.execute(
                update(OnboardingLink).values(used_at=datetime.now(UTC) - REPLAY_WINDOW)
            )
        for response in (await http.get(url), await http.post(url)):
            assert response.status_code == 410
            assert_page(response, "link_unusable", "he", "en")
        assert len(linker.links) == 1

    async def test_an_expired_link_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id, expires_at=datetime.now(UTC) - timedelta(minutes=1))
        assert (await http.post(f"/connect/{token}")).status_code == 410
        assert linker.links == []

    async def test_a_composio_outage_can_be_retried_at_once(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """Spent before Composio is called; a retry within the replay window still works."""
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        linker.failure = CalendarProviderUnavailableError("down")
        first = await http.post(url)
        assert first.status_code == 503
        assert_page(first, "unavailable_new_link", "he", "en")
        assert_hardened(first)
        linker.failure = None
        assert (await http.post(url)).status_code == 303


class FailingProcrastinate(FakeProcrastinate):
    def configure_task(self, name: str, **options: Any) -> Any:
        class Deferrer:
            async def defer_async(self, **kwargs: Any) -> int:
                msg = "queue down"
                raise ConnectionError(msg)

        return Deferrer()


async def callback_url(
    http: AsyncClient,
    db: Database,
    linker: FakeConnectLinker,
    tenant_id: UUID,
    *,
    param: str = "connected_account_id",
) -> str:
    """Press the button, then build the URL Composio sends the browser back to."""
    response = await http.post(f"/connect/{await seed_link(db, tenant_id)}")
    assert response.status_code == 303
    sent = urlsplit(linker.links[-1]["callback_url"])
    return f"{sent.path}?{sent.query}&{param}={ACCOUNT_ID}"


async def tenant_row(db: Database, tenant_id: UUID) -> Tenant:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
    assert tenant is not None
    return tenant


async def connections(db: Database, tenant_id: UUID) -> list[CalendarConnection]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        rows = await session.scalars(
            select(CalendarConnection).order_by(CalendarConnection.connected_at)
        )
        return list(rows.all())


def late(url: str, *, age_s: int = 7200) -> str:
    """``url`` with its state re-signed ``age_s`` ago: by default past STATE_MAX_AGE_S."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    [state] = query["state"]
    payload = verify(state, secret=LINK_SECRET, salt=STATE_SALT, max_age_s=3600)
    assert payload is not None
    old = stale(lambda: state_token(payload.tenant_id, nonce=payload.nonce), age_s=age_s)
    query["state"] = [old]
    return f"{parts.path}?{urlencode(query, doseq=True)}"


async def wait_for_a_lock_wait(owner_conn: Any) -> None:
    """Until some backend waits on a lock: the callback's insert, on our uncommitted row."""
    for _ in range(500):
        if await owner_conn.fetchval("SELECT count(*) FROM pg_locks WHERE NOT granted"):
            return
        await asyncio.sleep(0.01)
    msg = "the callback never waited on the uncommitted row"
    raise AssertionError(msg)


class TestCallback:
    async def test_it_binds_activates_and_defers_the_message(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db, language="he")
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        response = await http.get(url)

        assert response.status_code == 200
        assert '<html lang="he" dir="rtl">' in response.text
        assert_page(response, "connected", "he")
        assert_hardened(response)
        tenant = await tenant_row(db, tenant_id)
        assert (tenant.status, tenant.onboarding_step) == ("active", None)
        [connection] = await connections(db, tenant_id)
        assert (
            connection.connected_account_id,
            connection.auth_config_id,
            connection.status,
            connection.composio_user_id,
        ) == (ACCOUNT_ID, AUTH_CONFIG_ID, "active", str(tenant_id))
        # Ids only (docs/adr/0001).
        assert queue.calls == [
            DeferredCall(
                ONBOARDING_CONNECTED_TASK,
                {},
                {"tenant_id": str(tenant_id), "connection_id": str(connection.id)},
            )
        ]

    async def test_composios_camel_case_parameter_works_too(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id, param="connectedAccountId")
        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        assert (await http.get(url)).status_code == 200

    async def test_a_refreshed_callback_sends_one_message(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db, language="he")
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        assert (await http.get(url)).status_code == 200
        assert (await http.get(url)).status_code == 200

        assert len(await connections(db, tenant_id)) == 1
        assert len(queue.calls) == 2
        assert queue.calls[0] == queue.calls[1]
        gowa = FakeOutbound(name="gowa")
        for call in queue.calls:  # what the worker does with each deferred job
            await announce_connected(
                UUID(call.kwargs["tenant_id"]),
                UUID(call.kwargs["connection_id"]),
                db=db,
                channels={"gowa": gowa}.__getitem__,
            )
        # No Google provider here, so the line says the events are still to come.
        assert [message.body for message in gowa.sent] == [t("all_set_pending", "he")]

    async def test_an_account_not_yet_active_can_be_reloaded(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id, status="INITIATED")

        first = await http.get(url)
        assert first.status_code == 400
        assert_page(first, "not_ready", "he", "en")
        assert (await tenant_row(db, tenant_id)).status == "onboarding"
        assert queue.calls == []

        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        second = await http.get(url)
        assert second.status_code == 200
        assert_page(second, "connected", "he")

    async def test_a_suspended_tenant_is_not_bound(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        await suspend(db, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        assert (await http.get(url)).status_code == 400
        assert await connections(db, tenant_id) == []
        assert (await tenant_row(db, tenant_id)).status == "suspended"
        assert queue.calls == []

    async def test_reconnecting_revokes_the_old_connection(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_old", auth_config_id=AUTH_CONFIG_ID
            )
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        assert (await http.get(url)).status_code == 200

        statuses = {c.connected_account_id: c.status for c in await connections(db, tenant_id)}
        assert statuses == {"ca_old": "revoked", ACCOUNT_ID: "active"}

    async def test_an_account_already_bound_to_another_tenant_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        """Unreachable while D5's user_id check holds, but if it ever happens: a friendly
        failure page, no activation, no job, and neither tenant changes."""
        tenant_a = await seed_tenant(db, phone="972501110001")
        tenant_b = await seed_tenant(db, phone="972501110002")
        async with db.tenant_session(TenantId(tenant_b)) as session:
            await bind_connection(
                session, tenant_b, connected_account_id=ACCOUNT_ID, auth_config_id=AUTH_CONFIG_ID
            )
        before = await connections(db, tenant_b)
        url = await callback_url(http, db, linker, tenant_a)
        linker.accounts[ACCOUNT_ID] = account(tenant_a)

        with sentry_events() as events:
            response = await http.get(url)

        assert response.status_code == 409
        assert_page(response, "failed", "he", "en")
        assert_hardened(response)
        # Logging does not reach Sentry (event_level=None), so the conflict is sent there.
        [event] = events
        assert (event["message"], event["level"]) == ("connect.account_conflict", "error")
        assert event["fingerprint"] == ["connect.account_conflict"]
        assert event["tags"] == {"tenant_id": str(tenant_a)}
        assert ACCOUNT_ID not in str(event)
        assert (await tenant_row(db, tenant_a)).status == "onboarding"
        assert await connections(db, tenant_a) == []
        assert queue.calls == []
        assert [(c.id, c.status) for c in await connections(db, tenant_b)] == [
            (c.id, c.status) for c in before
        ]

    async def test_a_failed_defer_still_shows_connected(
        self,
        connect_settings: Settings,
        db: Database,
        linker: FakeConnectLinker,
        queue: FakeProcrastinate,
    ) -> None:
        """The tenant is bound and active. The page says so, and a reload re-defers."""
        application = connect_app(
            connect_settings, db=db, linker=linker, queue=FailingProcrastinate()
        )
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            tenant_id = await seed_tenant(db)
            url = await callback_url(client, db, linker, tenant_id)
            linker.accounts[ACCOUNT_ID] = account(tenant_id)
            response = await client.get(url)

            assert response.status_code == 200
            assert_page(response, "connected", "he")
            assert_hardened(response)
            assert (await tenant_row(db, tenant_id)).status == "active"

            # The reload, with a working queue, mends it: one job for the one connection.
            application.state.procrastinate = queue
            assert (await client.get(url)).status_code == 200
            [connection] = await connections(db, tenant_id)
            assert queue.calls == [
                DeferredCall(
                    ONBOARDING_CONNECTED_TASK,
                    {},
                    {"tenant_id": str(tenant_id), "connection_id": str(connection.id)},
                )
            ]

    async def test_a_late_reload_of_a_connected_tenant_still_says_connected(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        """The tab reloaded after the state's hour: no lookup, no bind, no second message."""
        tenant_id = await seed_tenant(db, language="en")
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        assert (await http.get(url)).status_code == 200
        before = [(c.id, c.status) for c in await connections(db, tenant_id)]
        linker.lookups.clear()
        queue.calls.clear()

        response = await http.get(late(url))

        assert response.status_code == 200
        assert_page(response, "connected", "en")
        assert_hardened(response)
        assert linker.lookups == []
        assert queue.calls == []
        assert [(c.id, c.status) for c in await connections(db, tenant_id)] == before

    async def test_a_late_callback_for_a_tenant_not_connected_fails(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        response = await http.get(late(url))

        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
        assert linker.lookups == []
        assert queue.calls == []
        assert await connections(db, tenant_id) == []
        assert (await tenant_row(db, tenant_id)).status == "onboarding"

    async def test_a_late_callback_for_an_active_tenant_with_no_connection_fails(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        await make_active(db, tenant_id)
        response = await http.get(late(url))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")

    async def test_a_callback_past_recognition_fails(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        assert (await http.get(url)).status_code == 200
        response = await http.get(late(url, age_s=31 * 24 * 3600))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")

    async def test_two_accounts_binding_at_once_ask_for_a_reload(
        self,
        http: AsyncClient,
        db: Database,
        linker: FakeConnectLinker,
        queue: FakeProcrastinate,
        owner_conn: Any,
    ) -> None:
        """Two callbacks for different accounts of one tenant, at the same moment. The loser's
        insert waits on the winner's uncommitted active row, then violates the one-active-per-
        tenant index. It gets a reload page, and the reload binds cleanly."""
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_winner", auth_config_id=AUTH_CONFIG_ID
            )
            loser = asyncio.create_task(http.get(url))
            await wait_for_a_lock_wait(owner_conn)
        response = await loser

        assert response.status_code == 503
        assert_page(response, "unavailable_retry", "he", "en")
        assert_hardened(response)
        assert queue.calls == []
        statuses = {c.connected_account_id: c.status for c in await connections(db, tenant_id)}
        assert statuses == {"ca_winner": "active"}

        reload = await http.get(url)
        assert reload.status_code == 200
        statuses = {c.connected_account_id: c.status for c in await connections(db, tenant_id)}
        assert statuses == {"ca_winner": "revoked", ACCOUNT_ID: "active"}

    async def test_a_fault_in_activate_is_not_taken_for_a_conflict(
        self,
        http: AsyncClient,
        db: Database,
        linker: FakeConnectLinker,
        queue: FakeProcrastinate,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KeyError is a LookupError. Only bind_connection's refusal is the conflict."""

        async def broken(*_args: object, **_kwargs: object) -> None:
            raise KeyError("boom")

        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        monkeypatch.setattr("personal_organizer.onboarding.connect.activate", broken)

        with capture_logs() as logs:
            response = await http.get(url)

        assert response.status_code == 500
        assert_page(response, "unavailable_retry", "he", "en")
        assert_hardened(response)
        assert not [entry for entry in logs if entry["event"] == "connect.account_conflict"]
        assert [entry["error_type"] for entry in logs if entry["event"] == "connect.failed"] == [
            "KeyError"
        ]
        assert queue.calls == []
        assert (await tenant_row(db, tenant_id)).status == "onboarding"


def _no_network(*_args: object, **_kwargs: object) -> httpx.Response:
    msg = "the lifespan must not call Composio"
    raise AssertionError(msg)


class TestLifespan:
    """``tests/api`` stubs ``app.state.connect_linker``; here the real lifespan builds it."""

    async def test_composio_enabled_builds_the_connector(
        self, connect_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(httpx.Client, "send", _no_network)
        application = create_app(connect_settings)
        async with application.router.lifespan_context(application):
            assert isinstance(application.state.connect_linker, ComposioConnector)

    async def test_composio_disabled_builds_none(self, db_settings: Settings) -> None:
        assert not db_settings.composio.enabled
        application = create_app(db_settings)
        async with application.router.lifespan_context(application):
            assert not hasattr(application.state, "connect_linker")
