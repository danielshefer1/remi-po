"""The once-a-minute tick: one job per due reminder and briefing, never two.

Against Postgres and the real Procrastinate app, because what keeps a second tick from
deferring again is a look at the queue for a waiting or running job with the same lock.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import procrastinate
import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db import engine
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import reminders
from personal_organizer.messaging.briefing import briefing_key
from personal_organizer.messaging.reminders import reminder_key
from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks import schedule
from personal_organizer.worker.tasks.schedule import BRIEFING_TASK, REMINDER_TASK, run_tick
from tests.db.test_reminders_repository import NOW, TODAY, _tenant
from tests.fixtures.connect import TRUNCATE_CONNECT_TABLES

pytestmark = [pytest.mark.db]


@pytest.fixture
async def db(
    db_settings: Settings, owner_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    monkeypatch.setattr(engine, "_database", database)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


@pytest.fixture
async def app(db_settings: Settings) -> AsyncIterator[procrastinate.App]:
    app = build_procrastinate_app(db_settings)
    async with app.open_async():
        yield app


async def _due_work(db: Database) -> tuple[UUID, UUID]:
    """One tenant due a briefing at ``NOW`` with one reminder due."""
    tenant_id = await _tenant(db, "+972500000001")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        reminder_id = await reminders.add_reminder(
            session, tenant_id, text="call mom", due_at=NOW - timedelta(minutes=1)
        )
    return tenant_id, reminder_id


async def _jobs(conn: Any) -> list[tuple[str, str, str, str | None, dict[str, Any]]]:
    rows = await conn.fetch(
        "SELECT task_name, queue_name, lock, queueing_lock, args FROM procrastinate_jobs"
        " ORDER BY task_name"
    )
    return [(*row[:4], json.loads(row["args"])) for row in rows]


async def test_the_tick_defers_one_job_per_due_reminder_and_briefing(
    db: Database, app: procrastinate.App, owner_conn: Any
) -> None:
    tenant_id, reminder_id = await _due_work(db)

    assert await run_tick(app, NOW, briefings=True) == (1, 1)

    briefing, reminder = briefing_key(tenant_id, TODAY), reminder_key(reminder_id)
    assert await _jobs(owner_conn) == [
        (
            BRIEFING_TASK,
            Queue.WEBHOOKS.value,
            briefing,
            None,
            {"tenant_id": str(tenant_id), "day": "2026-10-15"},
        ),
        (
            REMINDER_TASK,
            Queue.WEBHOOKS.value,
            reminder,
            None,
            {"reminder_id": str(reminder_id), "tenant_id": str(tenant_id)},
        ),
    ]


async def test_a_second_tick_in_the_same_minute_defers_nothing_new(
    db: Database, app: procrastinate.App, owner_conn: Any
) -> None:
    await _due_work(db)
    await run_tick(app, NOW, briefings=True)

    assert await run_tick(app, NOW, briefings=True) == (0, 0)
    assert len(await _jobs(owner_conn)) == 2


async def test_a_running_job_is_not_deferred_again_and_can_still_be_retried(
    db: Database, app: procrastinate.App, owner_conn: Any
) -> None:
    """A job ``doing`` when the next tick comes: a second job queued under a queueing lock
    would make retrying the first (a stalled worker) fail on that lock, and wait forever."""
    _, reminder_id = await _due_work(db)
    await run_tick(app, NOW, briefings=True)
    await owner_conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing' WHERE task_name = $1", REMINDER_TASK
    )

    assert await run_tick(app, NOW + timedelta(minutes=1), briefings=True) == (0, 0)
    assert len(await _jobs(owner_conn)) == 2

    [job] = await app.job_manager.list_jobs_async(lock=reminder_key(reminder_id))
    await app.job_manager.retry_job(job)
    assert [job.status for job in await app.job_manager.list_jobs_async(task=REMINDER_TASK)] == [
        "todo"
    ]


async def test_a_failing_briefing_scan_still_defers_the_reminders(
    db: Database, app: procrastinate.App, owner_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _due_work(db)

    async def broken(*args: Any) -> None:
        raise RuntimeError("scan failed")

    monkeypatch.setattr(schedule, "due_briefings", broken)
    with pytest.raises(RuntimeError):
        await run_tick(app, NOW, briefings=True)
    assert [job[0] for job in await _jobs(owner_conn)] == [REMINDER_TASK]


async def test_without_composio_there_is_no_briefing(
    db: Database, app: procrastinate.App, owner_conn: Any
) -> None:
    await _due_work(db)

    assert await run_tick(app, NOW, briefings=False) == (1, 0)
    assert [job[0] for job in await _jobs(owner_conn)] == [REMINDER_TASK]


async def test_nothing_due_defers_nothing(
    db: Database, app: procrastinate.App, owner_conn: Any
) -> None:
    await _due_work(db)
    assert await run_tick(app, NOW - timedelta(hours=1), briefings=True) == (0, 0)
    assert await _jobs(owner_conn) == []
