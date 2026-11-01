# #63 Change and Delete Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** People change or delete calendar events (one-off, or one date / from a date on / all
of a series) in plain words, behind the same natural-language confirm as adding.

**Architecture:** One value type, `EventChange`, travels the whole way: the model's
`propose_change` builds it from a `list_events` number, it is stored as a `change_event`
pending action, and on "yes" `Calendar.change(change)` applies it — to Remi's own `events` rows
(skips, single-date rows, series splits) or to Google through three new Composio calls, then
refreshes the copy. Confirm, claim-before-write and the fixed replies reuse the add path.

**Tech Stack:** Python 3.14, SQLAlchemy async + Alembic (Postgres, RLS), dateutil rrule,
Composio SDK (toolkit `googlecalendar` pinned `20261010_00`), Anthropic via `LLMProvider`,
pytest (`-m db`, `-m eval`).

**Spec:** `docs/superpowers/specs/2026-10-29-63-change-delete-events-design.md`

## Global Constraints

- Product voice (CLAUDE.md): no menus, numbers or IDs in anything the person reads; Hebrew is
  written as Hebrew. Copy lives in `messaging/onboarding_text.py`, `messaging/confirm.py` and the
  prompt in `messaging/agent.py`.
- The model never sees or sends an event id: only `n` from a `list_events` in the same turn.
- No description, attendees or organizer identity is read into a `CalendarEvent`, the DB, a
  log or a prompt (D6). `invited` is a bool only.
- Every Google write sends `"calendar_id": "primary"` and `"send_updates": "none"`; arguments
  are built in `composio.py` from typed parameters (D2).
- Claim, commit, then write once; a failed write is never retried (ADR 0003).
- Everything DB-side through `Database.tenant_session` + `scoped(...)`.
- Run tests from the worktree with `PYTHONPATH=src` and its own `.env` (DATABASE__* repointed to
  a fresh `po_t63`, plus MODELS__*; without MODELS__* the DB tests silently skip). Build the DB:
  `create database po_t63`, `.venv/bin/python -m personal_organizer.db.cli bootstrap`,
  `.venv/bin/alembic upgrade head`.
- Hebrew text sent to `respond`/`converse` in a unit test needs a Hebrew scripted reply (#62's
  redo guard otherwise pops an empty script).
- Gate per task: `uv run ruff check . && uv run ruff format --check . && uv run mypy src` and
  the task's tests. (Local mypy needs `dateutil` stubs: copy the `dateutil-stubs` dir as
  `dateutil` into a job dir and set `MYPYPATH`; don't re-sync the shared venv.)

## Review Focus

1. A repeating event's first occurrence chosen with `which="following"` — expected to behave
   exactly as `all` (no empty series with `UNTIL` before its start). Test in Task 2 and Task 3.
2. A single-date change, then "delete the whole series" — the moved date goes too. Test in Task 2.
3. A "yes" arriving after the hourly sync moved or removed the event — nothing written, the
   "changed meanwhile" line. Test in Task 3 (native and Google) and Task 5 (flow).
4. DST: a weekly 18:00 series split across the October change keeps 18:00 local on both sides,
   and `UNTIL` on a Google timed series is UTC `Z` one second before the occurrence. Test in
   Task 2 and Task 3.
5. A Google delete of something already gone (404/410) counts as done, not as "nothing
   changed". Test in Task 3.

---

### Task 1: Data shape — migration 0013, `CalendarEvent` fields, `EventChange`, skips in reads

**Files:**
- Create: `alembic/versions/0013_event_changes.py`
- Modify: `src/personal_organizer/db/models/tenant.py` (`Event`)
- Modify: `src/personal_organizer/interfaces/calendar.py`
- Modify: `src/personal_organizer/messaging/recurrence.py` (`occurrences`)
- Modify: `src/personal_organizer/db/repositories/events.py` (`_to_event`, `events_between`, `_upsert_google`)
- Test: `tests/unit/test_recurrence.py`, `tests/db/test_events_repository.py`

**Interfaces:**
- Produces:
  - `CalendarEvent` gains `series_id: str | None = None` and `invited: bool = False` (after `rule`).
  - In `interfaces/calendar.py`:
    ```python
    Which = Literal["this", "following", "all"]

    @dataclass(frozen=True)
    class EventChange:
        target: CalendarEvent   # the occurrence as list_events showed it
        which: Which = "this"   # "this" for an event that doesn't repeat
        delete: bool = False
        title: str | None = None
        start: datetime | None = None
        end: datetime | None = None
        location: str | None = None  # "" clears it
        rule: str | None = None      # a new pattern; only with which="following"
    ```
  - `occurrences(event, start, end, zone, skipped: Collection[datetime] = ())` drops occurrences
    whose aware start equals a skipped instant, and sets `series_id=event.external_id` on each.
  - `Event` gains `series_id: Mapped[str | None]`, `original_start: Mapped[datetime | None]`,
    `skipped: Mapped[list[datetime]]` (`ARRAY(DateTime(timezone=True))`, server default `'{}'`),
    `invited: Mapped[bool]` (server default false). `_to_event` fills `series_id`, `invited`.
  - `_upsert_google` writes and updates `series_id` and `invited`.

- [ ] **Step 1: Failing tests**
  - `test_recurrence.py::test_occurrences_skip_and_series_id`: weekly SU 18:00 Jerusalem series
    `external_id="s1"`, skipped = its second occurrence; over three weeks expect the 1st and 3rd
    only, each with `series_id == "s1"`.
  - `test_events_repository.py::test_google_copy_keeps_series_and_invited`: `replace_imported`
    with one event `series_id="g1"`, `invited=True`; `events_between` returns both fields.
  - `test_events_repository.py::test_skipped_occurrence_not_read`: a `remi` series row whose
    `skipped` (set with a direct `update(Event)`) holds one start; that date is absent.
- [ ] **Step 2:** `PYTHONPATH=src uv run pytest tests/unit/test_recurrence.py tests/db/test_events_repository.py -k "skip or series_and_invited"` — FAIL.
- [ ] **Step 3: Implement.** Migration 0013 (`down_revision = "0012_events"`): `add_column` ×4
  on `events`, downgrade drops them; mirror in the model so
  `tests/db/test_models_match_migrations.py` stays green. `events_between` passes `row.skipped`
  to `occurrences`. Update the `Event` docstring (`rule` is #53's, the new columns are #63's).
- [ ] **Step 4:** the tests above plus `tests/db/test_models_match_migrations.py` and the whole
  `tests/unit/test_recurrence.py` — PASS.
- [ ] **Step 5: Commit** `feat(calendar): series ids, skips and invites on events (#63)`

### Task 2: Native changes — `events.change_event`

**Files:**
- Modify: `src/personal_organizer/db/repositories/events.py`
- Modify: `src/personal_organizer/core/errors.py`
- Modify: `src/personal_organizer/messaging/recurrence.py` (helper)
- Test: `tests/db/test_events_repository.py`, `tests/unit/test_recurrence.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `class EventChangedError(AppError)`, `code = "event_changed"`: the target is no longer
    there as listed.
  - `recurrence.until_before(rule: str, at: datetime) -> str`: `rule` with `UNTIL` set to one
    second before `at`'s local wall time (replacing any `UNTIL`).
  - `async def find_row(session, tenant_id, external_id: str) -> Event | None`: the row for an
    `external_id`, resolving a native occurrence id `<row id>:<YYYYMMDDTHHMM>` to its series row.
  - `async def change_event(session, tenant_id, change: EventChange, zone: tzinfo) -> None`:
    applies `change` to `remi` rows; raises `EventChangedError` if stale.

Behaviour (`change.target.start` is the occurrence's start; for a single-date row it is its
`original_start` that names the series date):
- Stale when: no row; or row title ≠ target title; or, for a series, the target start is not
  an occurrence any more (skipped, past `UNTIL`); or, for a one-off, its start ≠ target start.
- One-off (no rule, no `series_id`): update fields / delete the row.
- Single-date row (`series_id` set): `this` updates/deletes that row; `following`/`all` act on
  its series row with the occurrence = `original_start`.
- Series, `this`: delete → append start to `skipped`; change → append start to `skipped` and
  insert a one-off row (`source="remi"`, `series_id` = series `external_id`, `original_start`).
- Series, `following` (and the target is not the first occurrence; if it is, do `all`):
  series row `rule = until_before(rule, target.start)`; delete skips ≥ target start and
  single-date rows with `original_start ≥` target start; for a change, insert a new series row
  starting at `change.start or target.start` with `change.rule or` the old rule (old `UNTIL`
  kept), the other fields changed or carried over.
- Series, `all`: delete → delete the series row and every row with its `series_id`; change →
  update title/location; a time change shifts `starts_at`, `ends_at` and every `skipped` by
  `change.start - target.start` (end shifts by its own delta when only `end` is given).
- Fields not given carry over; `location=""` stores `NULL`.

- [ ] **Step 1: Failing tests** (`test_events_repository.py`, weekly SU 18:00–19:00 Jerusalem
  series from 2026-11-01 added with `add_event(..., source="remi")`, read back with
  `events_between`):
  - `test_one_off_move_and_delete`
  - `test_series_delete_this_date` — 2026-11-08 gone, 11-01 and 11-15 stay.
  - `test_series_move_this_date` — 11-08 at 20:00, others 18:00; the 20:00 one has `series_id`.
  - `test_series_change_following_splits` — end 19:30 from 11-15: 11-01/11-08 end 19:00, 11-15
    and 11-22 end 19:30; a skip on 11-22 made before is dropped.
  - `test_series_following_on_first_is_all` — one series row left, no empty one.
  - `test_series_following_new_rule` — `rule="FREQ=WEEKLY;BYDAY=TU"` from 11-15: Tuesdays from
    11-17 on, Sundays only before.
  - `test_series_delete_all_takes_moved_dates` — move 11-08, then delete `all`: nothing left.
  - `test_series_shift_all_keeps_skips` — skip 11-08, then `all` start +1h: 19:00 on 11-01,
    11-15, and 11-08 still skipped.
  - `test_stale_target_raises` — delete the row, then `change_event` → `EventChangedError`.
  - `test_split_across_dst` — series DAILY 18:00 from 2026-10-20, change `following` from
    2026-10-28 (after the 2026-10-25 change): both halves read 18:00 local.
  - `test_recurrence.py::test_until_before` — `until_before("FREQ=WEEKLY;BYDAY=SU;UNTIL=20270101T000000", <2026-11-15 18:00 Jerusalem>)` == `"FREQ=WEEKLY;BYDAY=SU;UNTIL=20261115T175959"`.
- [ ] **Step 2:** run them — FAIL.
- [ ] **Step 3: Implement** in `events.py` (row ops) and `recurrence.py` (`until_before`, built
  on `_parse` and `_KEYS` order).
- [ ] **Step 4:** run them — PASS; then `PYTHONPATH=src uv run pytest tests/db/test_events_repository.py tests/unit/test_recurrence.py`.
- [ ] **Step 5: Commit** `feat(calendar): change and delete Remi's own events and series (#63)`

### Task 3: Google — Composio calls and `TenantCalendar.change`

**Files:**
- Modify: `src/personal_organizer/interfaces/calendar.py` (`CalendarProvider`)
- Modify: `src/personal_organizer/providers/calendar/composio.py`
- Modify: `src/personal_organizer/providers/calendar/memory.py`
- Modify: `src/personal_organizer/messaging/recurrence.py`
- Modify: `src/personal_organizer/messaging/calendar.py`
- Modify: `src/personal_organizer/core/errors.py`
- Test: `tests/unit/test_composio_calendar.py`, `tests/unit/test_recurrence.py`, `tests/db/test_tenant_calendar.py`

**Interfaces:**
- Consumes: Tasks 1–2 (`EventChange`, `EventChangedError`, `find_row`, `change_event`).
- Produces:
  - `CalendarProvider` gains:
    - `update_event(account_id, user_id, event_id: str, *, title: str | None = None, start: datetime | None = None, end: datetime | None = None, all_day: bool = False, location: str | None = None, recurrence: Sequence[str] | None = None) -> None`
    - `delete_event(account_id, user_id, event_id: str) -> None` (404/410 = already gone = done)
    - `get_series(account_id, user_id, event_id: str, zone: tzinfo) -> tuple[CalendarEvent, list[str]]` (master, its `recurrence` lines)
  - Slugs: `PATCH_EVENT = "GOOGLECALENDAR_PATCH_EVENT"`, `DELETE_EVENT = "GOOGLECALENDAR_DELETE_EVENT"`,
    `GET_EVENT = "GOOGLECALENDAR_EVENTS_GET"`. Argument names are in the spec's Google section.
    Times: `_rfc3339` for timed; `YYYY-MM-DD` with exclusive end for all-day; `timezone` = the
    `ZoneInfo` key, as `create_event` does. `create_event` also sends `send_updates: "none"`.
  - `_event` sets `series_id = item.get("recurringEventId")` and
    `invited = organizer present and organizer.get("self") is not True`. Nothing else of
    `organizer` is read.
  - `recurrence.local_rule(lines: Sequence[str], zone: tzinfo, all_day: bool) -> str | None`:
    the `RRULE:` line as our checked local rule (`UNTIL` `Z` → local wall; date-only →
    `T235959`), `None` if missing or outside `check_rule`'s subset.
  - `recurrence.end_lines(lines: Sequence[str], before: datetime, all_day: bool) -> list[str]`:
    every line kept, the `RRULE:` line's `COUNT` dropped and `UNTIL` set to one second before
    `before` in UTC `…Z` (timed) or the day before as `YYYYMMDD` (all-day).
  - `class SeriesSplitError(CalendarProviderError)`, `code = "series_split"`: the old series was
    ended but the new one wasn't created.
  - `Calendar` protocol gains `change(self, change: EventChange) -> None` and
    `series_rule(self, event: CalendarEvent) -> str | None` (`event.rule` for native; Google's
    master through `get_series` + `local_rule`; `None` when it doesn't repeat or can't be read).
  - `TenantCalendar.change`: `find_row`; `None` → `EventChangedError`. `remi` row →
    `change_event` in a tenant session with the owner's zone. `google` row with no provider or
    account → `CalendarNotConnectedError`; stale (stored start or title ≠ target) →
    `EventChangedError`; otherwise:
    - `this`, or no series: `delete_event` / `update_event` on the target's own id.
    - `all`: same on `series_id`; a time change shifts the master's start/end (from
      `get_series`) by the delta.
    - `following`: `get_series`; target is the master's first → as `all`; else
      `update_event(master, recurrence=end_lines(...))`; for a change, then `create_event`
      of the new series (`rule = change.rule or local_rule(lines)`); a create failure →
      `SeriesSplitError`.
    - then `refresh(target.start - 1 day, target.start + IMPORT_AHEAD)`; a failed refresh
      leaves the write standing.
  - `InMemoryCalendar` (provider): `update_event`/`delete_event` on its list by id and
    `get_series` returning the stored series event and `[f"RRULE:{rule}"]`; records
    `self.writes: list[tuple[str, str, dict[str, object]]]` (`op`, `event_id`, args).
  - `InMemoryTenantCalendar.change`: applies one-off update/delete and series `all` to its list,
    appends every change to `self.changed`; `fail` raises as in `add`. `series_rule` returns
    `event.rule`. `# shortcut: a series' "this"/"following" is recorded, not applied; apply it if
    po-chat needs it`.

- [ ] **Step 1: Failing tests**
  - `test_composio_calendar.py` (the harness's `schema`/`executed` routes):
    `test_patch_builds_typed_arguments` (exact dict, incl. `send_updates: "none"`, `timezone`,
    no unset keys), `test_patch_all_day_dates`, `test_delete_sends_no_updates`,
    `test_delete_gone_is_done` (status 410 in data → returns), `test_get_series_reads_recurrence`,
    `test_list_keeps_series_and_invited` (an item with `recurringEventId`, `organizer.self:
    false`; one with `self: true` → `invited` False; no organizer → False),
    `test_create_sends_no_updates`.
  - `test_recurrence.py`: `test_local_rule_from_google` (`RRULE:FREQ=WEEKLY;BYDAY=SU;UNTIL=20261231T215959Z`
    in Jerusalem → `...;UNTIL=20261231T235959`), `test_local_rule_outside_subset_is_none`
    (`BYDAY=1MO`), `test_end_lines_timed_utc` (before 2026-11-15 18:00 Jerusalem →
    `UNTIL=20261115T155959Z`, `COUNT` dropped, an `EXDATE` line kept), `test_end_lines_all_day`.
  - `test_tenant_calendar.py` (DB, `InMemoryCalendar` as provider): `test_change_native_routes_to_rows`,
    `test_google_this_patches_instance`, `test_google_all_patches_master`,
    `test_google_following_ends_and_creates`, `test_google_following_create_fails_is_split`,
    `test_google_stale_raises`, `test_google_without_connection_raises_not_connected`,
    `test_change_refreshes_copy`.
- [ ] **Step 2:** run them — FAIL.
- [ ] **Step 3: Implement** in the files above, following `create_event`'s parse/except shape.
  Keep the module docstrings' slug list and "two slugs and nothing else" sentence true.
- [ ] **Step 4:** run them — PASS; then `tests/unit/test_composio_calendar.py tests/unit/test_composio_connector.py tests/db/test_tenant_calendar.py tests/unit/test_recurrence.py`.
- [ ] **Step 5: Commit** `feat(calendar): change and delete Google events through Composio (#63)`

### Task 4: The model's side — numbered `list_events`, `propose_change`, the summary

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py`
- Modify: `src/personal_organizer/messaging/confirm.py`
- Test: `tests/unit/test_agent_tools.py`

**Interfaces:**
- Consumes: Task 1 `EventChange`; Task 3 `Calendar.series_rule`.
- Produces:
  - `@dataclass(frozen=True) class ProposedChange: change: EventChange; summary: str; question: str`
    (`question` is a `confirm.py` key).
  - `read_calendar(arguments, *, calendar, zone, listed: list[CalendarEvent])`: appends what it
    shows to `listed`; each shown event gets `"n"` (1-based across the turn's calls),
    `"repeats"` (`describe_rule(rule)` for native, `true` for a Google instance with
    `series_id`), and `"invite_from_someone_else": true` when `invited`.
  - `async def check_change(arguments, *, listed, calendar, zone) -> EventChange | str` and
    `def change_summary(change: EventChange, rule: str | None, zone, language) -> str`.
  - `respond(..., changed: list[ProposedChange] | None = None)`; tool `propose_change` in
    `TOOLS` and `_WRITE_TOOLS`; `hand_off` clears `changed` like `found`.
  - `confirm.py`: `confirm_prompt(summary, language, *, many=False, question: str | None = None)`;
    keys `confirm_change` ("Make this change?" / "לעדכן ביומן?"), `confirm_remove`
    ("Remove it from your calendar?" / "להסיר מהיומן?"), `confirm_remove_invite`
    ("This is an invite from someone else. Remove it from your calendar anyway?" /
    "זו הזמנה ממישהו אחר. להסיר אותה מהיומן בכל זאת?"). `_DECIDE` worded for adding, changing
    or removing: ADD means "go ahead with it as it is".

`check_change` errors (each a string the model reads, starting `error:`):
- `n` not a listed number → call `list_events` first, use its `n`.
- Repeating target without `which` in (`this`, `following`, `all`) → ask them: just this date,
  from this date on, or all of them — in plain words, one short question.
- Neither `delete` nor any field → say what to change.
- `invited` and not `delete` → it's someone else's invite, only they can change it; offer to
  remove it from their calendar.
- `invited` and `which == "following"` → offer just this date or the whole series.
- `repeat` with `which != "following"` → a new pattern goes with following.
- `repeat` fails `check_rule`/`first_at` → that error.
- A weekly series' start moved to another weekday with `which` in (`following`, `all`) and no
  `repeat` → send `repeat` with the new days.
- Google series, `following`, a change (not delete), no `repeat`, and `series_rule` is `None`
  → its pattern can't be copied; offer this date or all of them.
- `end <= start` → as `check_event`. Only `start` → `end = start + (target.end - target.start)`.
  An all-day target takes dates, like `check_event`.
- A second `propose_change`, or one mixed with `propose_event`, in the same message → one
  thing at a time.

`change_summary` (lines, like `event_summary`): the event as it will be (`event_summary` of the
result), the scope for a series (`Only <day label>` / `Every … from <day label>` /
`Every …` for all; Hebrew `רק ב…` / `… החל מ…`), and for a change a `was …` line with the old
times when they changed (`במקום …`). A delete shows the event as it is plus the scope. Ok
results: `"ok: proposed; the summary and the confirm question go below your reply"`.

- [ ] **Step 1: Failing tests** (`test_agent_tools.py`, `InMemoryTenantCalendar`, scripted
  `respond` runs like the existing `propose_event` ones):
  - `test_list_events_numbers_and_flags` — `n` 1..k across two calls; `repeats`; invite flag.
  - `test_propose_change_needs_listed_n`, `test_propose_change_asks_which_for_series`,
    `test_invite_cannot_be_moved`, `test_invite_following_refused`,
    `test_repeat_only_with_following`, `test_weekday_move_needs_repeat`,
    `test_one_change_per_message`, `test_start_only_keeps_length`.
  - `test_change_summary_move_en` and `_he` — exact strings for the dentist move
    (`was 09:00-10:00`); `test_change_summary_remove_this_date`;
    `test_respond_puts_change_confirm_below` — reply ends with `confirm_prompt(summary, "en",
    question="confirm_change")` and `changed` holds one `ProposedChange`.
  - `test_invite_remove_uses_invite_question`.
- [ ] **Step 2:** run them — FAIL.
- [ ] **Step 3: Implement.** Prompt: replace "You can't change or delete events yet; if asked,
  say so." with rules: to change or remove, find it with `list_events` first and call
  `propose_change` with its `n`; never guess; if several match, ask which by title and time;
  for a repeating one infer `which` (a date named → `this`; "from now on"/"from next week" →
  `following`; "the weekly …", "all of them", "for good" → `all`; a new weekday → `following`
  with `repeat`), ask only if it can't be inferred; like `propose_event` it changes nothing by
  itself, so never say it's done and never write the summary or question. (The prompt is
  English like the rest; replies follow their language.)
- [ ] **Step 4:** run them — PASS; then `tests/unit/test_agent_tools.py tests/unit/test_confirm.py`.
- [ ] **Step 5: Commit** `feat(agent): propose changes to events from list_events (#63)`

### Task 5: Confirm and apply — pending `change_event`, fixed replies, po-chat

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py` (`agent_turn`, `_confirmed`, payload)
- Modify: `src/personal_organizer/messaging/onboarding_text.py`
- Modify: `src/personal_organizer/admin/chat.py`
- Test: `tests/db/test_pending_flow.py`, `tests/unit/test_chat_cli.py`

**Interfaces:**
- Consumes: Task 4 `ProposedChange`, `respond(changed=)`; Task 3 `Calendar.change`, errors.
- Produces:
  - `CHANGE_EVENT: Final = "change_event"`.
  - `_change_payload(proposed: ProposedChange, inbox_id: UUID) -> dict[str, Any]` and
    `_stored_change(payload, zone) -> EventChange`: target and new values in local wall time,
    plus `summary`, `question`, `proposed_by`. `decide` gets `payload["summary"]`.
  - `async def apply_change(change, *, calendar, language) -> str` returning one fixed line.
  - Copy (EN / HE, in `onboarding_text.py`):
    - `event_changed`: "Done, I've updated it:\n{summary}" / "בוצע, עדכנתי ביומן:\n{summary}"
    - `event_removed`: "Done, it's off your calendar." / "בוצע, הסרתי את זה מהיומן."
    - `change_cancelled`: "No problem, I left it as it was." / "אין בעיה, השארתי כמו שהיה."
    - `event_not_changed`: "I couldn't reach your calendar just now, so nothing changed. Try
      me again in a bit." / "לא הצלחתי להגיע ליומן כרגע, אז לא שיניתי כלום. אפשר לנסות שוב
      עוד מעט."
    - `event_changed_meanwhile`: "That event changed in the meantime, so I left it alone.
      Tell me again what you'd like?" / "האירוע הזה השתנה בינתיים, אז לא נגעתי בו. מה לשנות?"
    - `series_split`: "I ended the old series before {day}, but couldn't add the new one. Ask
      me again to add it?" / "סיימתי את הסדרה הישנה לפני {day}, אבל לא הצלחתי להוסיף את
      החדשה. לבקש שוב?"
    - `calendar_disconnected_change`: as `calendar_disconnected`, "I couldn't change it: …" /
      "לא הצלחתי לשנות את זה, …".
    - `proposal_closed` stays (its "didn't add" reads wrong for a change: add
      `change_closed`: "That suggestion is no longer open, so I didn't change anything. Tell me
      again what to change." / "ההצעה הזו כבר לא בתוקף, אז לא שיניתי כלום. מה לשנות?").
- Behaviour: `agent_turn` passes `changed=[]` to `respond`, stores `CHANGE_EVENT` when it holds
  one (instead of `CREATE_EVENT`), and drops it when the question didn't go out, as for
  proposals. `_confirmed` branches on `action.kind`: CANCEL → `change_cancelled`; ADD → claim
  (committed) → `apply_change`. `EventChangedError` → `event_changed_meanwhile`;
  `CalendarNotConnectedError` → `calendar_disconnected_change`; `SeriesSplitError` →
  `series_split`; other provider errors → `event_not_changed` (never retried). po-chat keeps a
  waiting `ProposedChange` like `waiting` events and applies it on ADD through
  `InMemoryTenantCalendar.change`.

- [ ] **Step 1: Failing tests** (`test_pending_flow.py`, scripted LLM, `InMemoryCalendar`
  provider over a real tenant):
  - `test_change_proposed_then_yes_moves_once` — the outbound text ends with the change
    question; "כן" → `event_changed`; a re-run and a second "כן" change nothing more.
  - `test_change_cancel_leaves_it` — `change_cancelled`.
  - `test_change_other_goes_to_agent`.
  - `test_change_after_sync_moved_it` — between propose and yes, the stored copy moves →
    `event_changed_meanwhile`, provider has no writes.
  - `test_change_provider_down_not_retried` — `fail = CalendarProviderUnavailableError` →
    `event_not_changed`, one attempt.
  - `test_remove_invite_flow` — question is `confirm_remove_invite`; yes → `event_removed`.
  - `test_change_question_never_sent_drops_it`.
  - po-chat: `test_chat_applies_confirmed_change`.
- [ ] **Step 2:** run them — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** run them — PASS; then the full suite `PYTHONPATH=src uv run pytest` and the
  gate.
- [ ] **Step 5: Commit** `feat(agent): confirm and apply event changes (#63)`

### Task 6: Evals, runbook, graph

**Files:**
- Modify: `tests/evals/test_agent_evals.py`
- Modify: `docs/runbook-iteration-03.md` ("Removing a repeating event")
- Modify: `graphify-out/` (regenerated)

**Interfaces:**
- Consumes: everything above; the eval harness's `Proposal`-style scenarios.

- [ ] **Step 1: Eval cases** (each asserts the `ProposedChange` the turn produced, never the
  reply's wording, plus "no menu, no number" in the reply), over a seeded calendar: a one-off
  dentist Thu 09:00; a weekly Pilates SU 18:00–19:30; a Google invite "Team sync" with
  `invited=True`, weekly MO 10:00:
  - EN "move the dentist to 10" → dentist, `this`, start 10:00, end 11:00.
  - HE "תבטלי את רופא השיניים" → dentist, `delete`.
  - EN "pilates is cancelled next sunday" → Pilates, `this`, `delete`, next Sunday.
  - HE "פילאטיס ביום ראשון הבא ב-18:30" → `this`, start 18:30.
  - EN "from now on pilates ends at 19:00" → `following`, end 19:00.
  - HE "פילאטיס עובר לימי שלישי מעכשיו" → `following`, `repeat` with `BYDAY=TU`.
  - EN "delete the weekly pilates" → `all`, `delete`.
  - EN "cancel pilates" → no proposal; the reply asks one question.
  - EN "move the team sync to 11" → no proposal; the reply says it's someone else's.
  - HE "תמחקי את הטים סינק של יום שני הקרוב" → `this`, `delete`, and the confirm uses
    `confirm_remove_invite`.
  - `test_decide` gains "yes, change it" / "כן תעדכני" → ADD and "leave it" / "לא, תשאירי" →
    CANCEL against a change summary.
- [ ] **Step 2:** commit, push the branch, open a draft PR, and run
  `gh workflow run evals.yml --ref feat/63-change-events -f filter="change or remove or pilates or dentist or team or decide"`
  three times (or label `run-evals`); each case must pass ≥2 of 3. Run the whole eval suite
  once to catch regressions from the prompt and `_DECIDE` changes; judge per case (#67 fails on
  main too).
- [ ] **Step 3:** runbook: replace the SQL block with "Ask Remi in chat ('delete the weekly
  class'); for Google, it changes Google too." Keep the SQL only as a fallback under a
  sentence saying so.
- [ ] **Step 4:** `PYTHONHASHSEED=0 graphify update .`; commit `docs: #63 runbook and graph`.

---

## After merge (not tasks)

- Staging: `alembic current` shows `0013_event_changes` after the pre-deploy migrate.
- On a Google-connected tenant: move one date of a weekly series, end one from a date, delete
  an invite — and check whether the organizer saw a decline (spec asks to report it).
