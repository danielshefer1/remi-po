# #50 Native Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every member has a calendar in Remi, with or without Google. Connecting Google
imports their Google events into it, and an hourly sync keeps the copy fresh. A member who
wants Remi to check Google live on every read can turn that on by just asking, and Remi tells
them this option exists.

**Architecture:** A tenant-scoped `events` table holds native events (`source = 'remi'`) and
imported Google events (`source = 'google'`, with Google's id). One per-tenant facade,
`TenantCalendar`, replaces every caller's `(CalendarProvider, CalendarConnection | None)` pair.
- **By default (import),** `events(start, end)` reads the stored rows only. Google is copied
  in on connect, and then again by a `calendar:sync` job about every hour for each connected
  member.
- **With live reading on** (`tenants.live_calendar`, off by default), `events(start, end)`
  first refreshes that window's imported rows from Google, then reads. If Google is down, it
  serves the stored copy.

`add(event)` writes to Google when connected and stores the copy. Otherwise it stores natively.

**Tech Stack:** Python 3.14, SQLAlchemy async, Alembic (tenant RLS, a `SECURITY DEFINER` scan
like `due_reminders`), Procrastinate, Composio via `CalendarProvider`.

**Spec:** GitHub issue #50, plus the user's decision on 2026-10-27, recorded in
`docs/superpowers/plans/2026-10-27-open-issues-roadmap.md`. One-way import is the default, and
"merge on read" (live reading) is an opt-in feature each member can turn on and off in chat.
There is no two-way sync.

## Global Constraints

- `events` is a `TenantMixin` table under `enable_tenant_rls`, with a seed factory in
  `tests/db/test_isolation.py`. Columns: `id`, `title`, `starts_at`, `ends_at`, `all_day`,
  `location`, `rule text NULL` (unused until #53), `source` ∈ (`remi`, `google`),
  `external_id text NULL`, `created_at`. Unique on `(tenant_id, external_id)`.
- `tenants.live_calendar boolean NOT NULL DEFAULT false`, and
  `calendar_connections.synced_at timestamptz NULL`.
- Only the title, the times and the location are stored, as `CalendarEvent` carries today. No
  description and no attendees.
- `IMPORT_AHEAD = timedelta(days=90)` and `SYNC_EVERY = timedelta(hours=1)`. A sync, an import
  or a live refresh replaces exactly the `google` rows that overlap the window it read. `remi`
  rows are never touched by them.
- **The option is made known, in plain words, and only where it fits (product voice):**
  1. The `all_set` line sent after connecting Google says, in both languages: their events are
     in, Remi syncs about every hour, and if they'd rather Remi check Google every time, they
     can just say so.
  2. The agent prompt tells the model the option exists and what it is now set to. It offers
     it when the person notices a Google change Remi doesn't have yet ("I just moved that in
     Google"), and turns it on or off when they ask.
  No menus and no setting names: "I'll check Google live every time from now on", not
  "live_calendar enabled".
- No caller says "your calendar isn't connected" anymore when it lists or proposes. The
  `connect_calendar` tool stays, as "pull in your Google calendar too".
- Briefings no longer depend on `COMPOSIO__ENABLED`. With Composio off, `provider` is `None`,
  everything is native, and no sync runs.
- Migration revision `0012_events`, `down_revision = "0011_group_asks"`.

## Review Focus

1. **An event deleted in Google.** By default, expect it gone after the next hourly sync. With
   live reading on, expect it gone on the next read of that window. Pinned in Task 2
   (`test_live_refresh_drops_deleted_google_event`) and Task 5
   (`test_sync_drops_deleted_google_event`).
2. **Google down** during a sync, the briefing, or a live read. Expect the stored copy served,
   the sync retried next hour, and no retry storm. Pinned in Task 2
   (`test_live_google_down_serves_stored`) and Task 5 (`test_sync_failure_keeps_rows`).
3. **An event created through Remi while connected.** Expect it in Google, and stored once (no
   duplicate after the next sync or refresh). Pinned in Task 2
   (`test_add_while_connected_is_not_duplicated_by_refresh`).
4. **Another tenant's events, or the sync scan leaking content.** Expect RLS enforced, and the
   definer scan returning tenant ids only. Pinned in Task 1 by the isolation seed and
   `test_due_syncs_returns_ids_only`.
5. **A member who reconnects a different Google account.** Expect the old account's imported
   rows replaced by the import on connect, and native rows kept. Pinned in Task 5
   (`test_new_account_import_replaces_imported_rows`).

---

### Task 1: Migration 0012, models, repository

**Files:**
- Create: `alembic/versions/0012_events.py`, `src/personal_organizer/db/repositories/events.py`
- Modify: `src/personal_organizer/db/models/tenant.py` (`Event`, `EVENT_SOURCES`,
  `Tenant.live_calendar`, `CalendarConnection.synced_at`),
  `src/personal_organizer/db/repositories/tenants.py` (`set_live_calendar`),
  `tests/db/test_isolation.py`
- Test: `tests/db/test_events_repository.py`

**Interfaces:**
- Produces:
  - `add_event(session, tenant_id, event: CalendarEvent, *, source: str, external_id: str |
    None = None) -> CalendarEvent`. Its `external_id` is the given one, or `str(row id)`.
  - `events_between(session, tenant_id, start, end) -> list[CalendarEvent]`, overlapping,
    sorted by start.
  - `replace_imported(session, tenant_id, start, end, events: Sequence[CalendarEvent]) -> None`
    deletes the `google` rows overlapping the window, then inserts `events` (`ON CONFLICT
    (tenant_id, external_id) DO UPDATE`).
  - `mark_synced(session, tenant_id, now) -> None`, on the active connection.
  - `set_live_calendar(session, tenant_id, on: bool) -> None`.
  - `due_syncs(session, now) -> list[UUID]`, through a `SECURITY DEFINER` function `due_syncs`
    in `system_session`. It returns the tenant ids of active tenants with an active connection
    whose `synced_at` is null or older than `SYNC_EVERY`, limited to 200, with the same DDL
    order and grants as 0007's `due_reminders`.

- [ ] **Step 1: Write failing tests:** `test_add_and_read_native`,
  `test_replace_imported_keeps_native`, `test_replace_imported_upserts_same_id`,
  `test_events_between_overlap_edges` (an event ending exactly at `start` is excluded),
  `test_due_syncs_returns_ids_only` (a fresh `synced_at` isn't due, and one 61 min old is),
  `test_live_calendar_defaults_off`.
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. **Step 4:** Run them, plus
  `test_rls.py`, `test_isolation.py`, `test_models_match_migrations.py` and `test_migrations.py`.
  Expect PASS.
- [ ] **Step 5: Commit** `feat(calendar): Remi's own events table, sync bookkeeping, live option`.

### Task 2: `TenantCalendar`

**Files:**
- Create: `src/personal_organizer/messaging/calendar.py`
- Test: `tests/db/test_tenant_calendar.py`, using `InMemoryCalendar`, which gains a
  `fail: Exception | None` attribute

**Interfaces:**
- Consumes: Task 1's repository.
- Produces:
  - `class TenantCalendar(db: Database, tenant_id: UUID, *, provider: CalendarProvider | None,
    account: CalendarConnection | None, live: bool = False)`, with `connected: bool` and `live:
    bool`.
  - `async refresh(start, end) -> bool` pulls the window from Google into the stored copy.
    It returns `False` when there is no Google, or Google failed (then it logs
    `calendar.refresh_failed`). It is shared by the live read, the import and the sync.
  - `async events(start: datetime, end: datetime) -> Sequence[CalendarEvent]` calls `refresh`
    first only when `live and connected`, then reads the stored rows. It never raises a
    provider error.
  - `async add(event: CalendarEvent) -> CalendarEvent` re-raises
    `CalendarProviderUnavailableError` and `CalendarProviderRejectedError`, so callers keep
    today's "not added" line.

- [ ] **Step 1: Write failing tests:** `test_native_only_when_not_connected`,
  `test_default_reads_stored_without_calling_google` (`InMemoryCalendar.calls` stays empty),
  `test_live_refresh_drops_deleted_google_event`, `test_live_google_down_serves_stored`,
  `test_add_without_account_is_native`,
  `test_add_while_connected_is_not_duplicated_by_refresh`.
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. **Step 4:** Run them. Expect
  PASS.
- [ ] **Step 5: Commit** `feat(calendar): one calendar per member, with optional live reading of Google`.

### Task 3: The agent uses `TenantCalendar`, and can turn live reading on and off

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py`:
  - `read_calendar`, `add_event`, `_confirmed`, `respond` and `agent_turn`;
  - a new `live_calendar` tool;
  - `agent_prompt(..., live: bool)`'s `{calendar}` line, and the "Connecting their calendar is
    optional" bullet;
  - the `list_events`/`propose_event`/`connect_calendar` descriptions, with "Google Calendar"
    changed to "their calendar".

  Also modify `src/personal_organizer/messaging/schedule.py` (`Schedule.set_live_calendar(on:
  bool)`, plus `InMemorySchedule`), `src/personal_organizer/admin/chat.py` and
  `src/personal_organizer/worker/tasks/channel.py` (`provider = agent_calendar() if
  settings.composio.enabled else None`; `live=tenant.live_calendar`).
- Test: `tests/unit/test_agent_tools.py`, `tests/db/test_agent_turn.py`, `tests/db/test_pending_flow.py`

**Interfaces:**
- Consumes: `TenantCalendar` (Task 2) and `set_live_calendar` (Task 1).
- Produces:
  - `respond(..., calendar: TenantCalendar, ...)`, with no `account` parameter;
  - `read_calendar(arguments, *, calendar: TenantCalendar, zone) -> str`;
  - the tool `live_calendar` with `{"on": boolean, required}`. Its result is `ok: …` or
    `error: they haven't connected Google; offer connect_calendar`.

- [ ] **Step 1: Update the tests first.** `test_list_events_without_google_reads_native`,
  `test_propose_without_google_is_allowed`, `test_confirm_without_google_stores_natively`,
  `test_connect_calendar_still_sends_link`, `test_live_calendar_on_and_off`,
  `test_live_calendar_without_google_is_error`. Delete the tests asserting `NOT_CONNECTED` on
  list/propose.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.** The prompt's `{calendar}` line has three cases:
  - not connected: "You keep their calendar. They haven't connected Google; offer to pull it in
    only when it would help, and call connect_calendar if they want it."
  - connected, live off: "You keep their calendar, with their Google events copied in about
    every hour, so a change they just made in Google may not show yet. If that matters to
    them (they say something's missing or out of date), tell them in a few words you can check
    Google live every time instead, and call live_calendar if they want it."
  - connected, live on: "You keep their calendar and check their Google Calendar live every
    time. If they want it back to hourly, call live_calendar with off."
- [ ] **Step 4: Run them.** Expect PASS.
- [ ] **Step 5: Commit** `feat(agent): Remi's calendar by default; live Google reading when they ask`.

### Task 4: Groups, outings and the briefing use `TenantCalendar`

**Files:**
- Modify: `src/personal_organizer/messaging/groups/turn.py` (`_events`, `free_busy`,
  `_send_details`), `src/personal_organizer/messaging/groups/outings.py` (the calendar add on
  an "in" vote), `src/personal_organizer/messaging/briefing.py` (`_today`, and `send_briefing`
  skips only when there are no events and no reminders),
  `src/personal_organizer/worker/tasks/schedule.py` (`briefings=True`)
- Test: `tests/db/test_group_turn.py`, `test_outings.py`, `test_briefing.py`, `test_schedule_tick.py`

**Interfaces:**
- Consumes: `TenantCalendar(db, tenant_id, provider=..., account=active_connection(...),
  live=tenant.live_calendar)`, built per member inside the loops that already open each
  member's session.

- [ ] **Step 1: Update and write tests:** `test_free_busy_reads_native_events`,
  `test_free_busy_live_member_refreshes`, `test_vote_in_adds_native_event_without_google`,
  `test_briefing_without_google_lists_native_events`, `test_tick_defers_briefings_without_composio`.
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. **Step 4:** Run the full suite.
  Expect PASS.
- [ ] **Step 5: Commit** `feat(calendar): groups, outings and the briefing read Remi's calendar`.

### Task 5: Import on connect, hourly sync

**Files:**
- Create: `src/personal_organizer/messaging/calendar_sync.py`, with `sync_calendar(tenant_id, *,
  db, provider, now) -> bool`. It runs `refresh(now - 1 day, now + IMPORT_AHEAD)` and, on
  success, `mark_synced`.
- Modify: `src/personal_organizer/onboarding/connected.py` (call `sync_calendar` before the
  keyed `all_set` send, so the message is true; a failure is only logged), and
  `src/personal_organizer/worker/tasks/schedule.py`:
  - a `SYNC_TASK = "calendar:sync"` task;
  - `run_tick` defers one per `due_syncs` id, locked `sync:<tenant_id>`, and only when Composio
    is enabled;
  - it now returns a 4-tuple.
- Test: `tests/db/test_calendar_sync.py`, `tests/db/test_onboarding_connected.py`,
  `tests/db/test_schedule_tick.py`

- [ ] **Step 1: Write failing tests:** `test_connect_imports_next_90_days`,
  `test_import_failure_still_sends_all_set`, `test_new_account_import_replaces_imported_rows`,
  `test_sync_drops_deleted_google_event`, `test_sync_failure_keeps_rows` (`synced_at` unchanged,
  so it is due again), `test_tick_defers_due_syncs_once`.
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. Add the comment
  `# shortcut: one Google page (250 events) per read; follow nextPageToken if a 90-day import
  is ever cut`. **Step 4:** Run them. Expect PASS.
- [ ] **Step 5: Commit** `feat(calendar): import Google on connect and sync it hourly`.

### Task 6: Copy that makes the option known, evals, PR, staging

**Files:** `src/personal_organizer/messaging/onboarding_text.py` (`all_set` in both languages;
retire or reword `calendar_not_connected` and `calendar_disconnected` where they no longer
apply; the key sets stay equal), `tests/evals/test_agent_evals.py`

- [ ] **Step 1: Write the `all_set` copy** (Opus implementer; Hebrew written as Hebrew, not
  translated). Example English: "You're connected. Your Google events are in, and I'll keep
  them synced about every hour. If you'd rather I check Google live every time, just tell me."
- [ ] **Step 2: Evals,** English and Hebrew each:
  - `test_no_google_add_then_list`: "dentist tomorrow at 10", then "yes", then "what do I have
    tomorrow?" lists it.
  - `test_no_google_no_nagging`: a plain question doesn't push connecting Google.
  - `test_stale_google_offers_live`: connected with live off, "I just moved my meeting in
    Google, why don't you see it?" mentions checking Google live, and calls no `live_calendar`
    until they agree.
  - `test_asks_for_live_turns_it_on`: "always check my Google calendar live" calls
    `live_calendar` with on, and the reply says so in plain words.
  - Update the old "calendar isn't connected" evals.
- [ ] **Step 3: Run** `-k "calendar or event or connect or live"` 3 times. Each passes at least
  2 of 3.
- [ ] **Step 4:** Draft PR (`Closes #50`). The PR description has a short **"Live Google
  reading (optional)"** section: what it does, that it's off by default, how a member turns it
  on ("just ask Remi"), and its cost (one Composio call per read).
- [ ] **Step 5: Staging,** after merge: migrate to 0012, deploy. Connect a test tenant to
  Google, and check that the `all_set` message mentions the live option. Delete an event in
  Google, ask Remi, and expect it still shown until the next sync, after which it's gone. Say
  "check Google live every time", delete another event, ask again, and expect it gone right
  away. With a tenant without Google, add an event and list it.
