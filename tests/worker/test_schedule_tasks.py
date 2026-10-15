"""The schedule tasks: registered under their names, on queues the worker takes."""

from __future__ import annotations

from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks.channel import INBOUND_RETRY
from personal_organizer.worker.tasks.schedule import BRIEFING_TASK, REMINDER_TASK, TICK_TASK


def test_the_tick_runs_every_minute_and_never_piles_up(settings: Settings) -> None:
    app = build_procrastinate_app(settings)
    assert app.periodic_registry.periodic_tasks[(TICK_TASK, "")].cron == "* * * * *"
    assert app.tasks[TICK_TASK].queueing_lock == TICK_TASK
    assert app.tasks[TICK_TASK].queue == Queue.MAINTENANCE.value


def test_the_jobs_send_from_the_webhooks_queue_and_retry_like_inbound(settings: Settings) -> None:
    """Only definitely-unsent failures are retried: a send is never repeated (ADR 0003)."""
    app = build_procrastinate_app(settings)
    for name in (REMINDER_TASK, BRIEFING_TASK):
        assert app.tasks[name].queue == Queue.WEBHOOKS.value
        assert app.tasks[name].queue in settings.worker.queues
        assert app.tasks[name].retry_strategy is INBOUND_RETRY
