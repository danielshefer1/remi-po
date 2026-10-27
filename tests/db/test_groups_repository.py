"""Family groups (Iteration 07), against Postgres as ``app_user``.

What the group turns rely on: the rolling context keeps only the newest 20 messages of the
last six hours, and the four definer functions name exactly the right tenants -- active
members only, details only with a grant *and* sharing in that group, and a nudge only inside
the local daytime window, three hours after the poll, never to someone who answered or was
nudged in the last 20 hours.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.group import GroupPoll
from personal_organizer.db.models.tenant import GroupMember, PollAnswer
from personal_organizer.db.repositories import groups
from personal_organizer.db.repositories.tenants import set_timezone
from personal_organizer.settings import Settings
from tests.fixtures.connect import make_active, seed_tenant, suspend

pytestmark = [pytest.mark.db]

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)  # 10:00 in Jerusalem (UTC+3 in October)
_TRUNCATE = "TRUNCATE tenants, wa_groups, channel_outbox, channel_inbox, procrastinate_jobs CASCADE"


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(_TRUNCATE)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(_TRUNCATE)


async def _group(db: Database, jid: str = "120363000000000001@g.us") -> UUID:
    async with db.system_session() as session:
        return (await groups.ensure_group(session, channel="gowa", jid=jid)).id


async def _tenant(
    db: Database, phone: str, *, zone: str | None = "Asia/Jerusalem", active: bool = True
) -> UUID:
    tenant_id = await seed_tenant(db, phone=phone)
    if active:
        await make_active(db, tenant_id)
    if zone is not None:
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await set_timezone(session, tenant_id, zone)
    return tenant_id


async def _join(
    db: Database, tenant_id: UUID, group_id: UUID, *, sharing: bool, at: datetime = NOW
) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await groups.touch_member(session, tenant_id, group_id, at)
        if sharing:  # a member starts out not sharing; set_sharing(False) is an explicit stop
            await groups.set_sharing(session, tenant_id, group_id, True)


async def _member(
    db: Database, phone: str, group_id: UUID, *, sharing: bool = True, **tenant: Any
) -> UUID:
    tenant_id = await _tenant(db, phone, **tenant)
    await _join(db, tenant_id, group_id, sharing=sharing)
    return tenant_id


# --- the rolling context ------------------------------------------------------------------


async def _add_context(db: Database, group_id: UUID, body: str, sent_at: datetime) -> None:
    async with db.system_session() as session:
        await groups.add_context(session, group_id, sender_name="Eden", body=body, sent_at=sent_at)


async def _context(db: Database, group_id: UUID) -> list[str]:
    async with db.system_session() as session:
        return [row.body for row in await groups.recent_context(session, group_id)]


async def test_context_keeps_newest_twenty(db: Database) -> None:
    group = await _group(db)
    for i in range(25):
        await _add_context(db, group, f"m{i}", NOW + timedelta(minutes=i))
    assert await _context(db, group) == [f"m{i}" for i in range(5, 25)]


async def test_context_drops_older_than_six_hours(db: Database) -> None:
    group, other = await _group(db), await _group(db, "120363000000000002@g.us")
    await _add_context(db, other, "elsewhere", NOW - timedelta(days=1))
    await _add_context(db, group, "stale", NOW - timedelta(hours=6, minutes=1))
    await _add_context(db, group, "edge", NOW - timedelta(hours=6))
    await _add_context(db, group, "fresh", NOW)
    assert await _context(db, group) == ["edge", "fresh"]
    assert await _context(db, other) == ["elsewhere"]


async def test_a_group_is_created_once_and_served_once(db: Database) -> None:
    group = await _group(db)
    assert await _group(db) == group
    async with db.system_session() as session:
        await groups.mark_served(session, group, NOW)
        await groups.mark_served(session, group, NOW + timedelta(days=1))
        served = await groups.ensure_group(session, channel="gowa", jid="120363000000000001@g.us")
    assert served.served_at == NOW


# --- who is in a group --------------------------------------------------------------------


async def test_group_tenants_lists_active_members_with_sharing(db: Database) -> None:
    group = await _group(db)
    a = await _tenant(db, "+972500000001")
    b = await _tenant(db, "+972500000002")
    c = await _tenant(db, "+972500000003")
    await _join(db, a, group, sharing=True)
    await _join(db, b, group, sharing=False, at=NOW + timedelta(seconds=1))
    await _join(db, c, group, sharing=True, at=NOW + timedelta(seconds=2))
    await suspend(db, c)
    async with db.system_session() as session:
        assert await groups.group_tenants(session, group) == [(a, True), (b, False)]


async def test_peers_of_only_shared_groups(db: Database) -> None:
    g1, g2 = await _group(db), await _group(db, "120363000000000002@g.us")
    a = await _member(db, "+972500000001", g1)
    b = await _member(db, "+972500000002", g1, sharing=False)
    await _member(db, "+972500000003", g1, active=False)
    await _member(db, "+972500000004", g2)
    async with db.system_session() as session:
        assert await groups.peers_of(session, a) == [b]


async def test_detail_grantors_needs_grant_and_sharing(db: Database) -> None:
    group = await _group(db)
    a = await _member(db, "+972500000001", group)
    b = await _member(db, "+972500000002", group)
    await _member(db, "+972500000003", group)
    async with db.tenant_session(TenantId(b)) as session:
        assert await groups.grant(session, b, a) is True

    async def grantors() -> list[UUID]:
        async with db.system_session() as session:
            return await groups.detail_grantors(session, a, group)

    assert await grantors() == [b]
    async with db.tenant_session(TenantId(b)) as session:
        await groups.set_sharing(session, b, group, False)
    assert await grantors() == []


async def test_grant_and_revoke_report_whether_anything_changed(db: Database) -> None:
    a = await _tenant(db, "+972500000001")
    b = await _tenant(db, "+972500000002")
    async with db.tenant_session(TenantId(b)) as session:
        assert await groups.grant(session, b, a) is True
        assert await groups.grant(session, b, a) is False
        assert await groups.revoke(session, b, a) is True
        assert await groups.revoke(session, b, a) is False


# --- follow-ups ---------------------------------------------------------------------------


async def _poll(db: Database, group_id: UUID, *, created_at: datetime) -> UUID:
    async with db.system_session() as session:
        result = await session.execute(
            insert(GroupPoll)
            .values(
                group_id=group_id,
                inbox_id=uuid4(),
                title="Dinner",
                starts_at=NOW + timedelta(days=2),
                ends_at=NOW + timedelta(days=2, hours=2),
                all_day=False,
                language="en",
                in_option="I'm in",
                out_option="Can't make it",
                created_at=created_at,
            )
            .returning(GroupPoll.id)
        )
        poll_id: UUID = result.scalar_one()
        return poll_id


async def _answer(db: Database, tenant_id: UUID, poll_id: UUID, **values: Any) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await session.execute(
            insert(PollAnswer).values(tenant_id=tenant_id, poll_id=poll_id, **values)
        )


async def _move_poll(db: Database, poll_id: UUID, **values: Any) -> None:
    async with db.system_session() as session:
        await session.execute(update(GroupPoll).where(GroupPoll.id == poll_id).values(**values))


async def _due(db: Database, now: datetime) -> set[tuple[UUID, UUID]]:
    async with db.system_session() as session:
        return set(await groups.due_nudges(session, now))


async def test_due_nudges_window(db: Database) -> None:
    group = await _group(db)
    poll = await _poll(db, group, created_at=NOW - timedelta(days=1))
    eligible = await _member(db, "+972500000001", group)
    await _member(db, "+972500000002", group, sharing=False)
    await _member(db, "+972500000003", group, active=False)
    await _member(db, "+972500000004", group, zone=None)
    answered = await _member(db, "+972500000005", group)
    await _answer(db, answered, poll, answer="in")
    gave_reason = await _member(db, "+972500000006", group)
    await _answer(db, gave_reason, poll, reason="at work")
    nudged_19h = await _member(db, "+972500000007", group)
    await _answer(db, nudged_19h, poll, nudged_at=NOW - timedelta(hours=19))
    nudged_21h = await _member(db, "+972500000008", group)
    await _answer(db, nudged_21h, poll, nudged_at=NOW - timedelta(hours=21))

    # The local daytime window: 10:00 to 20:00 Jerusalem.
    assert await _due(db, NOW - timedelta(minutes=1)) == set()
    assert await _due(db, NOW) == {(poll, eligible), (poll, nudged_21h)}
    late = NOW + timedelta(hours=10)
    assert await _due(db, late - timedelta(minutes=1)) == {
        (poll, eligible),
        (poll, nudged_19h),
        (poll, nudged_21h),
    }
    assert await _due(db, late) == set()

    # Three hours after the poll went out, not before.
    await _move_poll(db, poll, created_at=NOW - timedelta(hours=2, minutes=59))
    assert await _due(db, NOW) == set()
    await _move_poll(db, poll, created_at=NOW - timedelta(hours=3))
    assert await _due(db, NOW) == {(poll, eligible), (poll, nudged_21h)}

    # Never once the event has started, nor on a closed poll.
    await _move_poll(db, poll, starts_at=NOW)
    assert await _due(db, NOW) == set()
    await _move_poll(db, poll, starts_at=NOW + timedelta(days=2), status="closed")
    assert await _due(db, NOW) == set()


# --- asking to share ----------------------------------------------------------------------


async def _open_ask(db: Database, tenant_id: UUID, group_id: UUID, now: datetime) -> bool:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await groups.open_ask(session, tenant_id, group_id, now)


async def _ask(db: Database, tenant_id: UUID, group_id: UUID, now: datetime) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await groups.ask(session, tenant_id, group_id, now)


async def test_ask_opens(db: Database) -> None:
    group_id = await _group(db)
    eden = await _member(db, "+972501110001", group_id, sharing=False)
    assert not await _open_ask(db, eden, group_id, NOW)
    await _ask(db, eden, group_id, NOW)
    assert await _open_ask(db, eden, group_id, NOW + timedelta(hours=47))


async def test_ask_expires(db: Database) -> None:
    group_id = await _group(db)
    eden = await _member(db, "+972501110001", group_id, sharing=False)
    await _ask(db, eden, group_id, NOW)
    assert not await _open_ask(db, eden, group_id, NOW + timedelta(hours=49))


async def test_set_sharing_clears_ask(db: Database) -> None:
    group_id = await _group(db)
    eden = await _member(db, "+972501110001", group_id, sharing=False)
    await _ask(db, eden, group_id, NOW)
    async with db.tenant_session(TenantId(eden)) as session:
        await groups.set_sharing(session, eden, group_id, True)
        await groups.set_sharing(session, eden, group_id, False)
    assert not await _open_ask(db, eden, group_id, NOW)


async def test_ask_ignores_sharing_member(db: Database) -> None:
    group_id = await _group(db)
    eden = await _member(db, "+972501110001", group_id, sharing=True)
    await _ask(db, eden, group_id, NOW)
    assert not await _open_ask(db, eden, group_id, NOW)


async def test_clear_ask_leaves_sharing(db: Database) -> None:
    group_id = await _group(db)
    eden = await _member(db, "+972501110001", group_id, sharing=False)
    await _ask(db, eden, group_id, NOW)
    async with db.tenant_session(TenantId(eden)) as session:
        await groups.clear_ask(session, eden, group_id)
    assert not await _open_ask(db, eden, group_id, NOW)
    async with db.system_session() as session:
        assert await groups.group_tenants(session, group_id) == [(eden, False)]


async def _stopped(db: Database, tenant_id: UUID) -> bool:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return (await session.scalars(select(GroupMember.stopped_at))).one() is not None


async def test_stop_is_not_asked_again(db: Database) -> None:
    group_id = await _group(db)
    eden = await _member(db, "+972501110001", group_id, sharing=True)
    assert not await _stopped(db, eden)
    async with db.tenant_session(TenantId(eden)) as session:
        await groups.set_sharing(session, eden, group_id, False)
    assert await _stopped(db, eden)

    async with db.tenant_session(TenantId(eden)) as session:
        assert await groups.ask(session, eden, group_id, NOW) is False
    assert not await _open_ask(db, eden, group_id, NOW)

    async with db.tenant_session(TenantId(eden)) as session:
        await groups.set_sharing(session, eden, group_id, True)
    assert not await _stopped(db, eden)
