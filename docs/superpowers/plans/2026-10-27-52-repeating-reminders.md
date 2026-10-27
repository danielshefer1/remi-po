# #52 Repeating Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "every Sunday at 8 remind me to take out the trash" becomes one reminder that keeps
firing, in the person's zone, until they stop it.

**Architecture:** A repeating reminder is a chain of rows. Each `reminders` row is one
occurrence. It carries the series' `rule` (an RRULE body with no DTSTART) and a `series_id`.
When an occurrence leaves `pending` (sent, failed, or skipped), the same transaction inserts the
next occurrence, computed in local wall time. Stopping the series means cancelling its one
pending row. `messaging/recurrence.py` checks, steps and describes rules, and #53 reuses it.

**Tech Stack:** Python 3.14, SQLAlchemy async, Alembic, Procrastinate, `python-dateutil`
(`dateutil.rrule`, already installed through `icalendar`, now declared).

**Spec:** GitHub issue #52 and `docs/superpowers/plans/2026-10-27-open-issues-roadmap.md`.

## Global Constraints

- Allowed rules, and nothing else: `FREQ` ∈ {`DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`};
  `INTERVAL` 1–52; `BYDAY` only with `WEEKLY`, plain codes `MO`…`SU` (no `1MO`); `BYMONTHDAY`
  only with `MONTHLY`, 1–31 or -1; at most one of `UNTIL` or `COUNT`, `COUNT` ≤ 400. No
  `DTSTART`, `BYHOUR`, `BYMINUTE` or `BYSETPOS`. Anything else is rejected with an error the
  model reads. Never fire more often than once a day.
- `COUNT` is turned into an `UNTIL` when the reminder is set. A stored rule never has `COUNT`,
  so stepping from any occurrence gives the same series.
- Occurrences are computed on local wall time in the tenant's zone, then made aware. A 08:00
  reminder stays 08:00 across DST.
- `STALE = timedelta(hours=12)`, the existing `due_reminders` cutoff. A repeating occurrence
  older than that is skipped without a send, and the series continues.
- Product voice (CLAUDE.md): the model says rules in plain words ("every Sunday at 8"). No menus.
- Migration revision `0010_reminder_series`, `down_revision = "0009_groups"`.

## Review Focus

1. **The worker is down for more than 12 hours** (as on 2026-10-25). Expect the series to resume
   at its next occurrence, without a late send. Pinned in Task 3
   (`test_stale_occurrence_skips_to_next`).
2. **The job runs twice for one occurrence.** Expect one send and one next row. Pinned in Task 3
   (`test_rerun_schedules_one_next`).
3. **A rule from the model that would spam** (`FREQ=HOURLY`, `FREQ=MINUTELY`, `BYHOUR=…`).
   Expect it rejected. Pinned in Task 1 (`test_rejects_sub_daily`).
4. **The last occurrence before UNTIL, or after COUNT runs out.** Expect no next row. Pinned in
   Task 2 (`test_schedule_next_stops_at_until`).
5. **"Did my trash reminder go off?" after it fired.** Expect the model to see it in
   `went_off`, and not set it again. Pinned in Task 4
   (`test_list_reminders_shows_recently_sent`) and by the `-k fired` evals.

---

### Task 1: `messaging/recurrence.py`

**Files:**
- Create: `src/personal_organizer/messaging/recurrence.py`
- Modify: `pyproject.toml` (add `"python-dateutil>=2.9.0"`), `uv.lock`
- Test: `tests/unit/test_recurrence.py`

**Interfaces:**
- Produces: `check_rule(rule: str, first: datetime) -> str`. It returns the normalised rule
  (keys in a fixed order, `COUNT` turned into `UNTIL` from the aware `first`), or raises
  `ValueError` with a short message the model can read.
  `next_after(rule: str, after: datetime) -> datetime | None` gives the first occurrence
  strictly after the aware `after`, with `after` as DTSTART, in `after.tzinfo`, or `None` past
  `UNTIL`.
  `describe_rule(rule: str) -> str` gives plain English ("every day", "every Sunday", "every 2
  weeks on Monday and Wednesday", "monthly on the 1st", "… until 2027-06-30").

- [ ] **Step 1: Write the failing tests**

```python
J = ZoneInfo("Asia/Jerusalem")
def test_weekly_byday_steps(): assert next_after("FREQ=WEEKLY;BYDAY=MO,WE", datetime(2026,10,26,7,0,tzinfo=J)) == datetime(2026,10,28,7,0,tzinfo=J)
def test_keeps_wall_time_across_dst(): assert next_after("FREQ=DAILY", datetime(2026,10,24,8,0,tzinfo=J)).hour == 8  # DST ends 2026-10-25 in Israel
def test_count_becomes_until(): assert check_rule("FREQ=DAILY;COUNT=3", datetime(2026,11,1,9,0,tzinfo=J)) == "FREQ=DAILY;UNTIL=20261103T090000"
def test_none_after_until(): assert next_after("FREQ=DAILY;UNTIL=20261103T090000", datetime(2026,11,3,9,0,tzinfo=J)) is None
def test_rejects_sub_daily():
    for bad in ("FREQ=HOURLY", "FREQ=MINUTELY", "FREQ=DAILY;BYHOUR=9,10", "FREQ=WEEKLY;BYDAY=1MO", "DTSTART=20260101T000000;FREQ=DAILY", "FREQ=DAILY;COUNT=2;UNTIL=20270101T000000", "nonsense"):
        with pytest.raises(ValueError): check_rule(bad, datetime(2026,11,1,9,0,tzinfo=J))
def test_describe(): assert describe_rule("FREQ=WEEKLY;BYDAY=SU") == "every Sunday"; assert describe_rule("FREQ=MONTHLY;BYMONTHDAY=1") == "monthly on the 1st"
```

- [ ] **Step 2: Run `uv run pytest tests/unit/test_recurrence.py -q`.** Expect FAIL (no module).
- [ ] **Step 3: Implement.** Parse the `KEY=VALUE` pairs yourself, to validate against the
  allowed set. Step with `dateutil.rrule.rrulestr(rule, dtstart=after.replace(tzinfo=None))`
  and `.after(naive, inc=False)`, then `.replace(tzinfo=after.tzinfo)`. `UNTIL` is local wall
  time, with no `Z`.
- [ ] **Step 4: Run the tests.** Expect PASS. Then the task gate (ruff, mypy).
- [ ] **Step 5: Commit** `feat(reminders): rules for repeating reminders`.

### Task 2: Migration 0010, model, repository

**Files:**
- Create: `alembic/versions/0010_reminder_series.py`
- Modify: `src/personal_organizer/db/models/tenant.py` (`Reminder`),
  `src/personal_organizer/db/repositories/reminders.py`
- Test: `tests/db/test_reminders_repository.py`. `tests/db/test_models_match_migrations.py`
  and `test_migrations.py` must stay green.

**Interfaces:**
- Consumes: `next_after` (Task 1).
- Produces: `Reminder.rule: str | None`, `Reminder.series_id: UUID | None`.
  `add_reminder(session, tenant_id, *, text, due_at, rule: str | None = None) -> UUID`, where
  the dedupe also matches `rule`.
  `schedule_next(session, tenant_id, reminder: Reminder, zone: ZoneInfo, *, after: datetime)
  -> UUID | None` inserts the next pending occurrence strictly after `max(reminder.due_at,
  after)`, with the same text, rule and `series_id or id`. It returns `None` for a one-off or
  after `UNTIL`.
  `recent_sent(session, tenant_id, *, limit: int = 5) -> list[Reminder]` gives `sent` rows,
  newest `due_at` first, of any age.

- [ ] **Step 1: Write failing DB tests:** `test_schedule_next_inserts_following_occurrence`,
  `test_schedule_next_stops_at_until`, `test_one_off_has_no_next`,
  `test_recent_sent_is_count_based_not_age_based` (a reminder sent 10 days ago is listed),
  `test_due_reminders_keeps_stale_repeating_rows` (a rule row 20 h overdue is returned, a
  one-off 20 h overdue is not).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Write migration 0010.** Add the columns `rule text NULL` and `series_id uuid
  NULL`. Then `CREATE OR REPLACE FUNCTION due_reminders`, as in 0007 but with the window
  condition `AND (r.due_at > p_now - interval '12 hours' OR r.rule IS NOT NULL)`. Downgrade
  restores 0007's body and drops the columns. Then update the model and the repository.
- [ ] **Step 4: Run** `tests/db/test_reminders_repository.py`, `test_models_match_migrations.py`
  and `test_migrations.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(reminders): store a repeating reminder as a series`. **This
  commit is Track B's base.** Tell the controller its SHA.

### Task 3: Firing schedules the next occurrence

**Files:**
- Modify: `src/personal_organizer/messaging/reminders.py` (`send_reminder`),
  `src/personal_organizer/worker/tasks/schedule.py` (pass `now`)
- Test: `tests/db/test_reminder_delivery.py`

**Interfaces:**
- Consumes: `schedule_next`, `finish_reminder` (Task 2).
- Produces: `send_reminder(reminder_id, tenant_id, *, db, channels, now: datetime) -> str | None`.

- [ ] **Step 1: Write failing tests:** `test_sent_repeating_reminder_schedules_next` (one new
  pending row at the next occurrence), `test_failed_send_still_schedules_next`,
  `test_rerun_schedules_one_next` (call twice, still one new row and one send),
  `test_stale_occurrence_skips_to_next` (`due_at = now - 13h`: no send, the row is `failed`,
  the next row is after `now`).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.** `schedule_next` runs in the same `tenant_session` as the
  `finish_reminder` that returned `True`, so only the finishing run inserts. For a stale
  occurrence (`rule` set and `now - due_at > STALE`), finish it as `failed` with no send, and
  schedule after `now`. The zone comes from `target.tenant.timezone`, or UTC when the target is
  `None`.
- [ ] **Step 4: Run the tests.** Expect PASS, along with `tests/db/test_schedule_tick.py`.
- [ ] **Step 5: Commit** `feat(reminders): a repeating reminder schedules its next time when it fires`.

### Task 4: Agent tools, and the fired-reminder fix

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py` (`TOOLS` for `set_reminder`,
  `list_reminders` and `cancel_reminder`; `remind`, `list_reminders`, `cancel`; the Reminders
  line in `_PROMPT`), `src/personal_organizer/messaging/schedule.py` (`TenantSchedule`,
  `InMemorySchedule`)
- Test: `tests/unit/test_reminder_tools.py`

**Interfaces:**
- Consumes: `check_rule`, `describe_rule`, `add_reminder(rule=)`, `schedule_next`,
  `recent_sent`.
- Produces: `Schedule.add(text, due_at, rule: str | None = None)`,
  `Schedule.cancel(reminder_id, *, just_once: bool = False) -> bool` (with `just_once`, it
  cancels the occurrence and schedules the next), `Schedule.recent_sent() -> list[Reminder]`.
  The tool argument names are `set_reminder.repeat` (an RRULE string, optional) and
  `cancel_reminder.just_this_one` (a boolean, default false).

- [ ] **Step 1: Write failing tests:**

```python
async def test_set_repeating(): out = await remind({"text": "trash", "at": "2026-11-01T08:00", "repeat": "FREQ=WEEKLY;BYDAY=SU"}, schedule=s, zone=J, now=NOW); assert out.startswith("ok") and s.reminders[0].rule == "FREQ=WEEKLY;BYDAY=SU"
async def test_bad_rule_is_an_error(): assert (await remind({..., "repeat": "FREQ=HOURLY"}, ...)).startswith("error:")
async def test_list_shows_repeat_in_words(): assert json.loads(await list_reminders(s, J, []))["reminders"][0]["repeats"] == "every Sunday"
async def test_list_reminders_shows_recently_sent(): assert json.loads(out)["went_off"] == [{"text": "Eden", "at": "2026-10-16T18:00"}]
async def test_skip_one_keeps_series(): await cancel({"n": 1, "just_this_one": True}, s, listed); assert [r.status for r in s.reminders] == ["cancelled", "pending"]
async def test_stop_series(): await cancel({"n": 1}, s, listed); assert all(r.status != "pending" for r in s.reminders)
```

- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.** The tool descriptions explain `repeat`: "an RRULE such as
  FREQ=WEEKLY;BYDAY=SU, or FREQ=MONTHLY;BYMONTHDAY=1;UNTIL=20270630T235900; at is the first
  time". In `_PROMPT`'s reminder rule, replace "list_reminders shows only reminders still to
  come …" with: "list_reminders shows the upcoming ones, and under went_off the last few that
  already went off. One there was sent, so never set it again unless they ask. 'Skip this week'
  is cancel_reminder with just_this_one, and 'stop reminding me' cancels the whole series. Say a
  repeat in plain words."
- [ ] **Step 4: Run the tests and the task gate.** Expect PASS.
- [ ] **Step 5: Commit** `feat(agent): set, list and stop repeating reminders; list what already went off`.

### Task 5: Evals, PR, staging

**Files:** `tests/evals/test_agent_evals.py`

- [ ] **Step 1: Add evals.** `test_weekly_trash_is_one_repeating_reminder` (English, one
  `set_reminder` with `FREQ=WEEKLY;BYDAY=SU`). `test_hebrew_daily_pills` ("תזכירי לי כל יום
  ב-9 לקחת כדורים", gives `FREQ=DAILY`). `test_stop_reminding_cancels_series`.
  `test_skip_this_week_keeps_series`. `test_month_of_class_reminders_is_repeating` (the
  2026-10-26 staging ask, ending in fewer than 10 `set_reminder` calls).
- [ ] **Step 2: Run** `uv run pytest -m eval tests/evals -k "repeat or fired or reminder"`
  3 times. Each passes at least 2 of 3, and `-k fired` now passes.
- [ ] **Step 3:** Open the draft PR (`Closes #52`). After the user merges: migrate staging,
  deploy, and set a "every day at <now+2min>" reminder. Check it fires two days running, then
  "stop".
