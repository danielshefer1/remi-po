from __future__ import annotations

import functools
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import procrastinate
import pytest

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    CalendarProviderUnavailableError,
    LLMUnavailableError,
    RejectedChannelError,
    TranscriptionUnavailableError,
    TransientChannelError,
)
from personal_organizer.db.engine import Database
from personal_organizer.messaging.agent import agent_turn
from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks import channel as channel_tasks
from personal_organizer.worker.tasks.channel import (
    HANDLE_INBOUND_TASK,
    INBOUND_RETRY,
    is_last_attempt,
)
from personal_organizer.worker.tasks.onboarding import ONBOARDING_CONNECTED_TASK
from personal_organizer.worker.tasks.system import PING_TASK, RETRY_STALLED_TASK


class TestProcrastinateApp:
    def test_tasks_are_registered(self, settings: Settings) -> None:
        app = build_procrastinate_app(settings)
        assert PING_TASK in app.tasks

    def test_worker_connects_as_the_runtime_role(self, settings: Settings) -> None:
        """Never as the owner: the worker must own nothing and run under RLS like
        everything else at runtime."""
        app = build_procrastinate_app(settings)
        pool_args = app.connector._pool_args  # type: ignore[attr-defined]
        assert "app_user" in pool_args["conninfo"]

    def test_ping_is_on_the_maintenance_queue(self, settings: Settings) -> None:
        app = build_procrastinate_app(settings)
        assert app.tasks[PING_TASK].queue == Queue.MAINTENANCE.value

    def test_building_twice_does_not_double_prefix_task_names(self, settings: Settings) -> None:
        """App.add_tasks_from mutates a Blueprint in place, so sharing one across two Apps
        produced `system:system:ping`. Registration functions avoid the shared state."""
        first = build_procrastinate_app(settings)
        second = build_procrastinate_app(settings)
        assert PING_TASK in first.tasks
        assert PING_TASK in second.tasks


class TestQueues:
    def test_every_queue_name_is_distinct(self) -> None:
        """A slow LLM turn sharing a queue with ingress would starve webhook
        acknowledgement, and Meta retries anything it does not see acked."""
        names = [queue.value for queue in Queue]
        assert len(names) == len(set(names))

    def test_default_subscriptions_exclude_the_agent_queue(self, settings: Settings) -> None:
        assert Queue.AGENT.value not in settings.worker.queues

    def test_default_subscriptions_include_the_queue_ping_runs_on(self, settings: Settings) -> None:
        """Dropping `maintenance` from WORKER__QUEUES breaks the walking-skeleton proof
        silently: the api still returns 200 {"deferred": true} and no worker ever runs it."""
        app = build_procrastinate_app(settings)
        assert app.tasks[PING_TASK].queue in settings.worker.queues


class TestChannelTasks:
    def test_the_inbound_task_is_on_the_webhooks_queue(self, settings: Settings) -> None:
        app = build_procrastinate_app(settings)
        assert app.tasks[HANDLE_INBOUND_TASK].queue == Queue.WEBHOOKS.value

    def test_the_worker_subscribes_to_it_by_default(self, settings: Settings) -> None:
        """Otherwise the webhook answers 200 and nothing ever processes the message."""
        app = build_procrastinate_app(settings)
        assert app.tasks[HANDLE_INBOUND_TASK].queue in settings.worker.queues

    def test_only_definitely_unsent_failures_are_retried(self, settings: Settings) -> None:
        """An ambiguous or rejected send must never be retried -- see docs/adr/0003."""
        strategy = build_procrastinate_app(settings).tasks[HANDLE_INBOUND_TASK].retry_strategy
        assert isinstance(strategy, procrastinate.RetryStrategy)
        assert TransientChannelError in (strategy.retry_exceptions or set())
        assert AmbiguousDeliveryError not in (strategy.retry_exceptions or set())
        assert not any(
            issubclass(RejectedChannelError, exc) for exc in strategy.retry_exceptions or set()
        )


class TestStalledJobRecovery:
    def test_it_runs_every_minute(self, settings: Settings) -> None:
        """A job killed mid-run by a deploy holds its sender's lock until this retries it."""
        app = build_procrastinate_app(settings)
        periodic = app.periodic_registry.periodic_tasks[(RETRY_STALLED_TASK, "")]
        assert periodic.cron == "* * * * *"
        assert app.tasks[RETRY_STALLED_TASK].queue in settings.worker.queues

    def test_runs_never_pile_up(self, settings: Settings) -> None:
        app = build_procrastinate_app(settings)
        assert app.tasks[RETRY_STALLED_TASK].queueing_lock == RETRY_STALLED_TASK

    async def test_one_failing_retry_does_not_stop_the_rest(self, settings: Settings) -> None:
        jobs = [
            procrastinate.jobs.Job(
                id=n, queue="webhooks", lock=None, queueing_lock=None, task_name="t"
            )
            for n in (1, 2)
        ]
        retried: list[int | None] = []

        class Manager:
            async def get_stalled_jobs(self) -> list[procrastinate.jobs.Job]:
                return jobs

            async def retry_job(self, job: procrastinate.jobs.Job) -> None:
                if job.id == 1:
                    raise RuntimeError("unique violation")
                retried.append(job.id)

        context = SimpleNamespace(app=SimpleNamespace(job_manager=Manager()))
        task = build_procrastinate_app(settings).tasks[RETRY_STALLED_TASK]
        await task.func(context, timestamp=0)
        assert retried == [2]


class TestOnboardingTasks:
    def test_the_connected_task_runs_on_a_queue_the_worker_takes(self, settings: Settings) -> None:
        """Otherwise the callback says "connected" and the message never comes."""
        task = build_procrastinate_app(settings).tasks[ONBOARDING_CONNECTED_TASK]
        assert task.queue == Queue.WEBHOOKS.value
        assert task.queue in settings.worker.queues

    def test_it_retries_what_the_inbound_task_retries(self, settings: Settings) -> None:
        """Only definitely-unsent failures: an ambiguous send is never repeated (ADR 0003)."""
        task = build_procrastinate_app(settings).tasks[ONBOARDING_CONNECTED_TASK]
        assert task.retry_strategy is INBOUND_RETRY


class TestLastAttempt:
    @pytest.mark.parametrize(
        "error",
        [LLMUnavailableError, CalendarProviderUnavailableError, TranscriptionUnavailableError],
    )
    def test_an_llm_calendar_or_transcription_outage_is_retried(
        self, error: type[Exception]
    ) -> None:
        assert error in (INBOUND_RETRY.retry_exceptions or set())

    @pytest.mark.parametrize("attempts", range(9))
    def test_it_is_the_run_procrastinate_will_not_retry(self, attempts: int) -> None:
        """Pinned against the installed source: the fallback must come on the run whose
        failure is final, not one earlier (a wasted retry) or never (silence)."""
        job = procrastinate.jobs.Job(
            queue="webhooks",
            lock=None,
            queueing_lock=None,
            task_name=HANDLE_INBOUND_TASK,
            attempts=attempts,
        )
        decision = INBOUND_RETRY.get_retry_decision(exception=LLMUnavailableError(), job=job)
        assert is_last_attempt(attempts) == (decision is None)


class TestOnAllowed:
    def test_the_agent_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm, calendar = object(), object()
        monkeypatch.setattr(channel_tasks, "agent_llm", lambda: llm)
        monkeypatch.setattr(channel_tasks, "agent_calendar", lambda: calendar)
        db = cast(Database, object())
        handoff = channel_tasks._on_allowed(db, last_attempt=True)
        assert isinstance(handoff, functools.partial)
        assert handoff.func is agent_turn
        assert handoff.keywords == {
            "db": db,
            "llm": llm,
            "calendar": calendar,
            "last_attempt": True,
        }

    async def test_the_acknowledgement_when_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        acked: list[Any] = []

        async def acknowledge(row: Any, channel: Any, *, db: Any) -> None:
            acked.append(row)

        monkeypatch.setattr(channel_tasks, "agent_llm", lambda: None)
        monkeypatch.setattr(channel_tasks, "acknowledge", acknowledge)
        handoff = channel_tasks._on_allowed(cast(Database, object()), last_attempt=False)
        row: Any = object()
        assert await handoff(row, cast(Any, None), cast(Any, None), datetime.now(UTC)) is None
        assert acked == [row]
