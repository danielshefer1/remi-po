"""Migration chain health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]

#: Everything the vendored Procrastinate schema creates is prefixed with this.
PREFIX = "procrastinate_"


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(_config())


def test_there_is_exactly_one_head() -> None:
    """A second head means two migrations were written in parallel and never merged;
    `alembic upgrade head` would then fail in pre-deploy, taking the deploy with it."""
    assert len(_scripts().get_heads()) == 1


async def test_migrations_have_been_applied(app_conn: Any) -> None:
    applied = await app_conn.fetchval("SELECT version_num FROM alembic_version")
    assert applied == _scripts().get_current_head()


async def test_procrastinate_tables_exist(app_conn: Any) -> None:
    exists = await app_conn.fetchval("SELECT to_regclass('public.procrastinate_jobs') IS NOT NULL")
    assert exists is True


_TABLES_SQL = (
    "SELECT tablename AS name FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE $1"
)
_FUNCTIONS_SQL = (
    "SELECT p.proname AS name FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid "
    "WHERE n.nspname = 'public' AND p.proname LIKE $1"
)
_TYPES_SQL = (
    "SELECT t.typname AS name FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid "
    "WHERE n.nspname = 'public' AND t.typname LIKE $1"
)


async def _surviving_objects(conn: Any) -> dict[str, list[str]]:
    """Every procrastinate-prefixed relation, routine and type currently in ``public``."""
    pattern = f"{PREFIX}%"
    return {
        "tables": [row["name"] for row in await conn.fetch(_TABLES_SQL, pattern)],
        "functions": [row["name"] for row in await conn.fetch(_FUNCTIONS_SQL, pattern)],
        "types": [row["name"] for row in await conn.fetch(_TYPES_SQL, pattern)],
    }


async def test_the_downgrade_round_trips(owner_conn: Any) -> None:
    """A rollback that cannot be re-applied is not a rollback.

    ``0002.downgrade`` used to drop three of the four tables and none of the types or
    functions, so ``downgrade`` followed by ``upgrade`` died on "type
    procrastinate_job_status already exists". Migrations are the deploy mechanism -- this runs
    as a pre-deploy command -- so the failure would surface mid-rollback in production.

    Ends back at head whatever happens, so this test does not disturb the ones around it.
    """
    config = _config()
    try:
        command.downgrade(config, "0001_baseline")
        assert await _surviving_objects(owner_conn) == {
            "tables": [],
            "functions": [],
            "types": [],
        }
        assert await _channel_tables(owner_conn) == []
        command.upgrade(config, "head")
        restored = await _surviving_objects(owner_conn)
        assert "procrastinate_jobs" in restored["tables"]
        assert "procrastinate_workers" in restored["tables"]
        assert "procrastinate_job_status" in restored["types"]
        assert await _channel_tables(owner_conn) == ["channel_inbox", "channel_outbox"]
    finally:
        command.upgrade(config, "head")


async def _channel_tables(conn: Any) -> list[str]:
    rows = await conn.fetch(_TABLES_SQL + " ORDER BY tablename", "channel_%")
    return [row["name"] for row in rows]


async def test_procrastinate_tables_are_owned_by_the_owner_role(app_conn: Any) -> None:
    owner = await app_conn.fetchval(
        "SELECT r.rolname FROM pg_class c JOIN pg_roles r ON c.relowner = r.oid "
        "WHERE c.relname = 'procrastinate_jobs'"
    )
    assert owner == "app_owner"


#: What 0004 adds, by catalog: tables, definer functions, and the outbox column.
_TENANT_TABLES = [
    "calendar_connections",
    "messages",
    "onboarding_links",
    "tenant_identities",
    "tenants",
]
_TENANT_TABLES_SQL = (
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
    "AND tablename = ANY($1::text[]) ORDER BY tablename"
)
_DEFINER_FUNCTIONS_SQL = (
    "SELECT p.proname, r.rolname FROM pg_proc p JOIN pg_roles r ON p.proowner = r.oid "
    "WHERE p.proname IN ('resolve_tenant', 'create_tenant') ORDER BY p.proname"
)
_OUTBOX_KEY_SQL = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'channel_outbox' AND column_name = 'idempotency_key'"
)


async def test_the_tenants_downgrade_round_trips(owner_conn: Any) -> None:
    """0004 down and up again, with an ``onboarding`` row in the inbox to carry across.

    The row is the case a plain drop-and-recreate misses: 0003's disposition check does not
    know ``onboarding``, so restoring that check over such a row fails the downgrade.
    """
    config = _config()
    await owner_conn.execute("TRUNCATE channel_outbox, channel_inbox CASCADE")
    inbox_id = await owner_conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_phone, "
        "message_type, sent_at, disposition) "
        "VALUES ('gowa', 'round-trip', 'tel:+15550000000', '+15550000000', 'text', now(), "
        "'onboarding') RETURNING id"
    )
    try:
        command.downgrade(config, "0003_channel_ledgers")
        assert await owner_conn.fetch(_TENANT_TABLES_SQL, _TENANT_TABLES) == []
        assert await owner_conn.fetch(_DEFINER_FUNCTIONS_SQL) == []
        assert await owner_conn.fetchval(_OUTBOX_KEY_SQL) == 0
        assert (
            await owner_conn.fetchval(
                "SELECT disposition FROM channel_inbox WHERE id = $1", inbox_id
            )
            == "allowed"
        )

        command.upgrade(config, "head")
        tables = await owner_conn.fetch(_TENANT_TABLES_SQL, _TENANT_TABLES)
        assert [row["tablename"] for row in tables] == _TENANT_TABLES
        functions = await owner_conn.fetch(_DEFINER_FUNCTIONS_SQL)
        assert [(row["proname"], row["rolname"]) for row in functions] == [
            ("create_tenant", "app_definer"),
            ("resolve_tenant", "app_definer"),
        ]
        assert await owner_conn.fetchval(_OUTBOX_KEY_SQL) == 1
    finally:
        command.upgrade(config, "head")
        await owner_conn.execute("TRUNCATE channel_outbox, channel_inbox CASCADE")


_INVITES_SQL = "SELECT to_regclass('public.invites')::text"


async def test_the_invites_downgrade_round_trips(owner_conn: Any) -> None:
    """0005 down and up again; the up must re-revoke DELETE (checked by the repository suite,
    which runs at head)."""
    config = _config()
    try:
        command.downgrade(config, "0004_tenants")
        assert await owner_conn.fetchval(_INVITES_SQL) is None
        command.upgrade(config, "head")
        assert await owner_conn.fetchval(_INVITES_SQL) == "invites"
    finally:
        command.upgrade(config, "head")


_PENDING_ACTION_SQL = (
    "SELECT data_type FROM information_schema.columns "
    "WHERE table_name = 'tenants' AND column_name = 'pending_action'"
)


async def test_the_pending_action_downgrade_round_trips(owner_conn: Any) -> None:
    config = _config()
    try:
        command.downgrade(config, "0005_invites")
        assert await owner_conn.fetchval(_PENDING_ACTION_SQL) is None
        command.upgrade(config, "head")
        assert await owner_conn.fetchval(_PENDING_ACTION_SQL) == "jsonb"
    finally:
        command.upgrade(config, "head")
