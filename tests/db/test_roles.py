"""The runtime role's constraints.

This is the foundation Iteration 03's RLS sits on. If `app_user` is a superuser, owns
tables, or has BYPASSRLS, then every policy written in Iteration 03 is decorative --
and nothing else in the test suite would notice.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

pytestmark = pytest.mark.db


async def test_runtime_role_is_not_a_superuser(app_conn: Any) -> None:
    assert (
        await app_conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is False
    )


async def test_runtime_role_cannot_bypass_rls(app_conn: Any) -> None:
    assert (
        await app_conn.fetchval("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        is False
    )


async def test_runtime_role_owns_no_tables(app_conn: Any) -> None:
    owned = await app_conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON c.relowner = r.oid "
        "WHERE r.rolname = current_user AND c.relkind IN ('r', 'p')"
    )
    assert owned == 0


async def test_runtime_role_cannot_create_in_public(app_conn: Any) -> None:
    assert (
        await app_conn.fetchval("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        is False
    )


async def test_runtime_role_cannot_become_the_owner(app_conn: Any) -> None:
    """No SET ROLE escape hatch out of RLS."""
    assert await app_conn.fetchval("SELECT pg_has_role(current_user, 'app_owner', 'USAGE')") is (
        False
    )


async def test_owner_role_is_not_a_superuser(owner_conn: Any) -> None:
    """FORCE ROW LEVEL SECURITY is meaningless against a superuser table owner, so the
    owner being non-super is what makes the spec's 'second guard' real."""
    assert (
        await owner_conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is False
    )


async def test_runtime_role_can_use_the_queue(app_conn: Any) -> None:
    """Procrastinate tables are owned by app_owner; app_user reaches them via the default
    privileges bootstrap installs. Without this the worker fails confusingly at runtime."""
    assert (
        await app_conn.fetchval(
            "SELECT has_table_privilege(current_user, 'procrastinate_jobs', 'SELECT')"
        )
        is True
    )


# --- app_definer (Iteration 03, D1) -------------------------------------------------------


async def test_runtime_role_cannot_become_the_definer(app_conn: Any) -> None:
    """app_definer is BYPASSRLS; app_user holding it would make every policy optional."""
    assert await app_conn.fetchval("SELECT pg_has_role(current_user, 'app_definer', 'MEMBER')") is (
        False
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("SET ROLE app_definer")


async def test_the_definer_cannot_log_in(app_conn: Any) -> None:
    row = await app_conn.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'app_definer'"
    )
    assert row is not None, "app_definer is missing; run po-db bootstrap"
    assert dict(row) == {"rolcanlogin": False, "rolsuper": False, "rolbypassrls": True}


async def test_the_definer_owns_no_tables(app_conn: Any) -> None:
    owned = await app_conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON c.relowner = r.oid "
        "WHERE r.rolname = 'app_definer'"
    )
    assert owned == 0


#: Exactly what app_definer may own. Another function here is another door past RLS: the
#: two resolution functions (Iteration 03), the tick's two id-only scans (Iteration 05), and
#: the four group lookups (Iteration 07).
DEFINER_FUNCTIONS = {
    "resolve_tenant",
    "create_tenant",
    "due_reminders",
    "due_briefings",
    "due_syncs",
    "group_tenants",
    "peers_of",
    "detail_grantors",
    "due_nudges",
}


async def test_the_definer_owns_exactly_the_definer_functions(app_conn: Any) -> None:
    rows = await app_conn.fetch(
        "SELECT p.proname, p.prosecdef, p.proconfig FROM pg_proc p "
        "JOIN pg_roles r ON p.proowner = r.oid WHERE r.rolname = 'app_definer'"
    )
    assert {row["proname"] for row in rows} == DEFINER_FUNCTIONS
    for row in rows:
        assert row["prosecdef"] is True
        # Without a pinned search_path, a SECURITY DEFINER function resolves names through
        # whatever schema its caller put first.
        assert row["proconfig"] == ["search_path=public, pg_temp"]


async def test_only_the_runtime_role_may_call_the_definer_functions(app_conn: Any) -> None:
    """PUBLIC holds EXECUTE on every new function by default. ``acldefault`` stands in for
    a NULL ACL, which means exactly that default -- reading only ``proacl`` would miss it."""
    rows = await app_conn.fetch(
        "SELECT p.proname, coalesce(g.rolname, 'PUBLIC') AS grantee "
        "FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "LEFT JOIN pg_roles g ON a.grantee = g.oid "
        "WHERE p.proname = ANY($1::text[]) AND a.privilege_type = 'EXECUTE'",
        sorted(DEFINER_FUNCTIONS),
    )
    grantees = {(row["proname"], row["grantee"]) for row in rows}
    assert grantees == {
        (name, role) for name in DEFINER_FUNCTIONS for role in ("app_definer", "app_user")
    }
