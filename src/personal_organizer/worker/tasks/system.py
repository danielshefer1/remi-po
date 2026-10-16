"""System tasks.

``ping`` is the walking skeleton's proof of life: the api defers it, the worker picks it up
and touches Postgres. That round trip exercises api -> queue -> worker -> database, which is
the point of Iteration 01.

Tasks are registered through a function rather than a module-level ``Blueprint`` because
``App.add_tasks_from`` mutates the blueprint in place, prefixing each task name with the
namespace. Registering the same blueprint with a second App therefore yields
``system:system:ping``. A registration function keeps no shared mutable state, so building
an app twice in one process (tests, a future management command) is safe.

Task arguments carry identifiers only, never user content -- see docs/adr/0001.
"""

from __future__ import annotations

import procrastinate
import structlog
from sqlalchemy import text

from personal_organizer.db.engine import get_database
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

PING_TASK = "system:ping"
RETRY_STALLED_TASK = "system:retry_stalled_jobs"


def register(app: procrastinate.App) -> None:
    @app.task(queue=Queue.MAINTENANCE.value, name=PING_TASK, pass_context=True)
    async def ping(context: procrastinate.JobContext) -> None:
        """Touch the database and log. Idempotent, so it is safe as a periodic task."""
        async with get_database().system_session() as session:
            await session.execute(text("SELECT 1"))
        log.info("ping.ok", job_id=context.job.id, queue=Queue.MAINTENANCE.value)

    @app.periodic(cron="* * * * *")
    @app.task(
        queue=Queue.MAINTENANCE.value,
        name=RETRY_STALLED_TASK,
        queueing_lock=RETRY_STALLED_TASK,
        pass_context=True,
    )
    async def retry_stalled_jobs(
        context: procrastinate.JobContext,
        timestamp: int,  # noqa: ARG001 - periodic tasks receive their schedule time
    ) -> None:
        """Put jobs whose worker died back in the queue.

        Nothing else does, and every merge to ``main`` redeploys staging, killing whatever
        was mid-run. Such a job stays ``doing`` forever -- and an inbound message job holds
        its sender's lock while it does, so everything that sender sends afterwards queues
        behind it and is never processed. Stalled means the worker stopped heartbeating,
        not merely that the job is slow. Retrying a reply mid-send is safe: see
        ``messaging.outbox``. One failing retry is logged and the rest still go.
        """
        manager = context.app.job_manager
        for job in await manager.get_stalled_jobs():
            try:
                await manager.retry_job(job)
            except Exception:  # one stuck job must not keep the rest stuck
                log.exception("job.stalled_retry_failed", job_id=job.id, task_name=job.task_name)
                continue
            log.warning(
                "job.stalled_retried", job_id=job.id, task_name=job.task_name, queue=job.queue
            )


__all__ = ["PING_TASK", "RETRY_STALLED_TASK", "register"]
