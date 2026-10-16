"""Tenant isolation, proved against Postgres as ``app_user`` (Done-When 2).

Two tenants, A and B, each with at least one row in every tenant table. Four things hold:

1. Under A's GUC, no B row is visible -- by raw SQL on every table, and through every
   repository function.
2. Under A's GUC, a row carrying B's id cannot be written: ``WITH CHECK`` refuses it, on
   insert and on update.
3. With no GUC -- or the ``''`` a pooled connection is left with after a ``SET LOCAL`` --
   every tenant table shows zero rows and accepts none.
4. The ``SECURITY DEFINER`` functions return a tenant id and nothing else.

The whole module runs twice: once as written, once with
:func:`~personal_organizer.db.repositories.scope.scoped` replaced by a no-op in every
repository module. The second run is the one that matters -- it shows RLS alone keeps tenants
apart, so an app-level filter forgotten in some future query leaks nothing.

Every tenant table needs a seed factory in :data:`SEEDS`. A model added without one fails
:func:`test_every_tenant_table_has_a_seed_factory` and the ``seeded`` fixture, so the suite
cannot silently stop covering a table.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from personal_organizer.core.types import TenantId
from personal_organizer.db import repositories
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories import connections, links, messages, tenants
from tests.db.tenant_tables import tenant_tables

pytestmark = [
    pytest.mark.db,
    pytest.mark.rls,
    pytest.mark.usefixtures("clean_tenant_tables", "clean_channel_tables"),
]

PHONES = {"a": "+972500000001", "b": "+972500000002", "x": "+972500000009"}
CHANNELS = {"a": "gowa", "b": "whatsapp", "x": "gowa"}
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

Seed = Callable[[Any, UUID, str], Awaitable[None]]


# --- seed factories: one per tenant table, run under the tenant's own GUC -----------------


async def _seed_tenant(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO tenants (id, language, timezone) VALUES ($1, 'en', 'Asia/Jerusalem')", tenant
    )


async def _seed_identity(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO tenant_identities (tenant_id, network, external_id, phone) "
        "VALUES ($1, 'whatsapp', $2, $3)",
        tenant,
        f"tel:{PHONES[tag]}",
        PHONES[tag],
    )


async def _seed_connection(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO calendar_connections (tenant_id, composio_user_id, connected_account_id, "
        "auth_config_id) VALUES ($1, $2, $3, 'ac_test')",
        tenant,
        str(tenant),
        f"ca_{tag}",
    )


async def _seed_link(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO onboarding_links (tenant_id, nonce, expires_at) VALUES ($1, $2, $3)",
        tenant,
        f"nonce-{tag}",
        NOW + timedelta(days=1),
    )


async def _seed_message(conn: Any, tenant: UUID, tag: str) -> None:
    inbox_id = await conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_phone, "
        "message_type, sent_at) VALUES ($1, $2, $3, $4, 'text', $5) RETURNING id",
        CHANNELS[tag],
        f"iso.{uuid4().hex}",
        f"tel:{PHONES[tag]}",
        PHONES[tag],
        NOW,
    )
    await conn.execute(
        "INSERT INTO messages (tenant_id, direction, channel, inbox_id, message_type, body, "
        "sent_at) VALUES ($1, 'in', $2, $3, 'text', $4, $5)",
        tenant,
        CHANNELS[tag],
        inbox_id,
        f"secret of {tag}",
        NOW,
    )


async def _seed_reminder(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO reminders (tenant_id, text, due_at) VALUES ($1, $2, $3)",
        tenant,
        f"secret reminder of {tag}",
        NOW + timedelta(hours=1),
    )


#: ``tenants`` first: every other table references it.
SEEDS: dict[str, Seed] = {
    "tenants": _seed_tenant,
    "tenant_identities": _seed_identity,
    "calendar_connections": _seed_connection,
    "onboarding_links": _seed_link,
    "messages": _seed_message,
    "reminders": _seed_reminder,
}

TABLES = sorted(tenant_tables())


@asynccontextmanager
async def as_tenant(conn: Any, tenant: UUID | None) -> AsyncIterator[None]:
    """A transaction on ``conn`` with ``app.tenant_id`` set as the application sets it."""
    async with conn.transaction():
        if tenant is not None:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        yield


@dataclass(frozen=True, slots=True)
class Seeded:
    a: UUID
    b: UUID


@pytest.fixture
async def seeded(app_conn: Any) -> Seeded:
    assert set(SEEDS) == set(TABLES), "every tenant table needs a seed factory"
    pair = Seeded(a=uuid4(), b=uuid4())
    for tenant, tag in ((pair.a, "a"), (pair.b, "b")):
        async with as_tenant(app_conn, tenant):
            for seed in SEEDS.values():
                await seed(app_conn, tenant, tag)
    return pair


# --- the two modes ------------------------------------------------------------------------


def _no_scope(stmt: Any, model: Any, tenant_id: UUID) -> Any:
    return stmt


def _disable_app_filter(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Replace ``scoped`` in every repository module, including ones added later."""
    patched: set[str] = set()
    for info in pkgutil.iter_modules(repositories.__path__):
        module = importlib.import_module(f"{repositories.__name__}.{info.name}")
        if hasattr(module, "scoped"):
            monkeypatch.setattr(module, "scoped", _no_scope)
            patched.add(info.name)
    return patched


@pytest.fixture(autouse=True, params=["app-filter", "rls-only"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "rls-only":
        patched = _disable_app_filter(monkeypatch)
        assert patched >= {"scope", "tenants", "links", "connections", "messages"}
    return str(request.param)


async def _snapshot(conn: Any, tenant: UUID) -> dict[str, list[tuple[Any, ...]]]:
    """Every row ``tenant`` owns, in every tenant table: what "B is untouched" compares."""
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    async with as_tenant(conn, tenant):
        for table in TABLES:
            rows = await conn.fetch(f"SELECT * FROM {table}")  # noqa: S608 - names from models
            snapshot[table] = sorted(tuple(row.values()) for row in rows)
    return snapshot


# --- 0. the suite covers every table ------------------------------------------------------


def test_every_tenant_table_has_a_seed_factory() -> None:
    missing = set(TABLES) - set(SEEDS)
    assert not missing, f"add a seed factory to SEEDS for {sorted(missing)}"
    assert set(SEEDS) <= set(TABLES), "SEEDS names a table no model scopes"


# --- 1. A sees no B rows ------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_a_sees_only_its_own_rows(app_conn: Any, seeded: Seeded, table: str) -> None:
    column = tenant_tables()[table]
    async with as_tenant(app_conn, seeded.a):
        owners = await app_conn.fetch(f"SELECT DISTINCT {column} AS owner FROM {table}")  # noqa: S608
    assert [row["owner"] for row in owners] == [seeded.a]


async def test_the_app_filter_is_really_off_in_rls_only_mode(
    database: Database, seeded: Seeded, mode: str
) -> None:
    """The canary for the patch: asking for B under A returns A's row once the WHERE is
    gone, and nothing while it is there. Either way, never B's."""
    async with database.tenant_session(TenantId(seeded.a)) as session:
        tenant = await tenants.get_tenant(session, seeded.b)
    expected = seeded.a if mode == "rls-only" else None
    assert (tenant.id if tenant else None) == expected


async def test_reads_through_every_repository_function(database: Database, seeded: Seeded) -> None:
    a, b = seeded.a, seeded.b
    async with database.tenant_session(TenantId(a)) as session:
        tenant = await tenants.get_tenant(session, b)
        assert tenant is None or tenant.id == a
        assert await tenants.primary_phone(session, b) != PHONES["b"]
        link = await links.latest_usable_link(session, b, now=NOW)
        assert link is None or link.tenant_id == a
        assert await messages.latest_inbound_channel(session, b) != CHANNELS["b"]
        assert await links.consume_link(session, b, nonce="nonce-b", now=NOW) is False


async def test_updates_through_every_repository_function_leave_b_alone(
    database: Database, app_conn: Any, seeded: Seeded
) -> None:
    before = await _snapshot(app_conn, seeded.b)
    async with database.tenant_session(TenantId(seeded.a)) as session:
        await tenants.set_timezone(session, seeded.b, "Europe/London")
        await tenants.set_onboarding_step(session, seeded.b, "connect")
        await tenants.activate(session, seeded.b)
        await links.consume_link(session, seeded.b, nonce="nonce-b", now=NOW)
    assert await _snapshot(app_conn, seeded.b) == before


@pytest.mark.parametrize(
    "write",
    [
        lambda s, b: links.create_link(s, b, nonce="nonce-new", expires_at=NOW),
        lambda s, b: connections.bind_connection(
            s, b, connected_account_id="ca_new", auth_config_id="ac_test"
        ),
        lambda s, b: messages.record_inbound(
            s, b, inbox_id=uuid4(), channel="gowa", message_type="text", body="x", sent_at=NOW
        ),
        lambda s, b: tenants.add_identity(
            s, b, network=NETWORK_WHATSAPP, external_id="uid:new-b", phone=None
        ),
    ],
    ids=["create_link", "bind_connection", "record_inbound", "add_identity"],
)
async def test_inserts_for_b_under_a_are_refused(
    database: Database,
    app_conn: Any,
    seeded: Seeded,
    write: Callable[[Any, UUID], Awaitable[object]],
) -> None:
    before = await _snapshot(app_conn, seeded.b)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with database.tenant_session(TenantId(seeded.a)) as session:
            await write(session, seeded.b)
    assert await _snapshot(app_conn, seeded.b) == before


async def test_an_account_bound_to_b_cannot_be_bound_to_a(
    database: Database, app_conn: Any, seeded: Seeded
) -> None:
    """``connected_account_id`` is unique across tenants, and RLS hides B's row from A: the
    insert conflicts with a row A cannot read. Refused, and A's own connection survives."""
    before = {
        tag: await _snapshot(app_conn, tenant) for tag, tenant in (("a", seeded.a), ("b", seeded.b))
    }
    with pytest.raises(LookupError):
        async with database.tenant_session(TenantId(seeded.a)) as session:
            await connections.bind_connection(
                session, seeded.a, connected_account_id="ca_b", auth_config_id="ac_test"
            )
    after = {
        tag: await _snapshot(app_conn, tenant) for tag, tenant in (("a", seeded.a), ("b", seeded.b))
    }
    assert after == before


async def test_a_key_owned_by_b_is_skipped_not_attached_to_a(
    database: Database, app_conn: Any, seeded: Seeded
) -> None:
    """``add_identity`` under A with a key B owns: ``False``, no error, B untouched."""
    before = await _snapshot(app_conn, seeded.b)
    async with database.tenant_session(TenantId(seeded.a)) as session:
        added = await tenants.add_identity(
            session,
            seeded.a,
            network=NETWORK_WHATSAPP,
            external_id=f"tel:{PHONES['b']}",
            phone=PHONES["b"],
        )
    assert added is False
    assert await _snapshot(app_conn, seeded.b) == before


# --- 2. WITH CHECK ------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_writing_b_rows_under_a_fails_with_check(
    app_conn: Any, seeded: Seeded, table: str
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with as_tenant(app_conn, seeded.a):
            await SEEDS[table](app_conn, seeded.b, "x")


@pytest.mark.parametrize("table", TABLES)
async def test_moving_a_row_to_b_fails_with_check(
    app_conn: Any, seeded: Seeded, table: str
) -> None:
    column = tenant_tables()[table]
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with as_tenant(app_conn, seeded.a):
            await app_conn.execute(f"UPDATE {table} SET {column} = $1", seeded.b)  # noqa: S608


# --- 3. no GUC ----------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_no_guc_means_zero_rows(app_conn: Any, seeded: Seeded, table: str) -> None:
    assert await app_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


@pytest.mark.parametrize("table", TABLES)
async def test_a_reset_guc_means_zero_rows_not_an_error(
    app_conn: Any, seeded: Seeded, table: str
) -> None:
    """After a ``SET LOCAL`` ends, the setting reads ``''``, not NULL -- the pooled
    connection's normal state, and the reason every policy says ``NULLIF``."""
    async with as_tenant(app_conn, seeded.a):
        pass
    assert await app_conn.fetchval("SELECT current_setting('app.tenant_id', true)") == ""
    assert await app_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


@pytest.mark.parametrize("table", TABLES)
async def test_no_guc_means_no_writes(app_conn: Any, seeded: Seeded, table: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with as_tenant(app_conn, None):
            await SEEDS[table](app_conn, seeded.b, "x")


# --- 4. the definer functions -------------------------------------------------------------


async def test_the_definer_functions_return_a_single_uuid(app_conn: Any) -> None:
    rows = await app_conn.fetch(
        "SELECT proname, proretset, pg_get_function_result(oid) AS result FROM pg_proc "
        "WHERE proname IN ('resolve_tenant', 'create_tenant') ORDER BY proname"
    )
    assert [(row["proname"], row["proretset"], row["result"]) for row in rows] == [
        ("create_tenant", False, "uuid"),
        ("resolve_tenant", False, "uuid"),
    ]


async def test_the_definer_functions_open_nothing_else(app_conn: Any, seeded: Seeded) -> None:
    """With no GUC, the door answers with B's id -- its job -- and the session can still
    read nothing: the function's BYPASSRLS does not outlive the call."""
    key = f"tel:{PHONES['b']}"
    assert await app_conn.fetchval("SELECT resolve_tenant('whatsapp', $1)", key) == seeded.b
    assert (
        await app_conn.fetchval("SELECT create_tenant('whatsapp', $1, NULL, 'he')", key) == seeded.b
    )
    for table in TABLES:
        assert await app_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    rows = await _snapshot(app_conn, seeded.b)
    assert (len(rows["tenants"]), len(rows["tenant_identities"])) == (1, 1)
