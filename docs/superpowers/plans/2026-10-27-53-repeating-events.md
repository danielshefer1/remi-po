# #53 Repeating Calendar Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "gym every Monday and Wednesday at 7" and a whole weekly timetable land in the
calendar as repeating events, confirmed with one natural "yes".

**Architecture:** `CalendarEvent` gains `rule` (the same RRULE subset as reminders, checked by
`messaging/recurrence.check_rule` from #52). Native events store the rule in `events.rule`, and
`events_between` expands them in the tenant's zone. Google events are created with
`recurrence`, and the import already arrives as single instances. `propose_event` takes
`repeat`, and a message may hold up to `MAX_PROPOSALS` proposals, kept as one pending action
and confirmed together.

**Tech Stack:** as #50 and #52; `dateutil.rrule` for expansion.

**Spec:** GitHub issue #53, minus editing and deleting single occurrences (moved to a new
issue, "change and delete events"), plus several proposals per message (roadmap, suggestion 3).

## Global Constraints

- The rule subset, the `COUNT`→`UNTIL` normalisation and the local wall-time stepping are #52's
  `messaging/recurrence.py`, reused unchanged except for Hebrew in `describe_rule`.
- `MAX_PROPOSALS = 8` per message. One "yes" adds all of them, and one "no" drops all of them.
  The pending action's TTL (1 h) is unchanged.
- A pending payload has the shape `{"events": [...], "proposed_by": ...}`. A legacy
  single-event payload (written before deploy, alive for up to 1 h) is still read.
- The confirm summary is fixed copy built by code, with the repeat in plain words in the
  tenant's language ("every Monday and Wednesday" / "כל יום שני ורביעי").
- Group free/busy, details, and the briefing see the expanded occurrences with no code of their
  own: they all go through `TenantCalendar.events`.
- The agent prompt keeps "You can't change or delete events yet".

## Review Focus

1. **A weekly class across the DST change** (2026-10-25 in Israel). Expect 18:00 both weeks.
   Pinned in Task 1 (`test_native_series_keeps_wall_time_across_dst`).
2. **A series with no end, read for a 31-day window.** Expect only the occurrences in the
   window. Pinned in Task 1 (`test_open_series_expands_only_window`).
3. **A pending single-event payload from before the deploy, then "yes".** Expect it added.
   Pinned in Task 3 (`test_legacy_payload_still_confirms`).
4. **9 classes in one timetable.** Expect the first 8 proposed, and the model told the rest
   didn't fit, so it says so. Pinned in Task 3 (`test_ninth_proposal_refused`).
5. **Google rejects the recurrence.** Expect today's "not added" line, not a half-added
   timetable claimed as done. Pinned in Task 2 (`test_create_recurring_rejected`).

---

### Task 1: Rules on native events

**Files:**
- Modify: `src/personal_organizer/interfaces/calendar.py` (`CalendarEvent.rule: str | None =
  None`), `src/personal_organizer/db/repositories/events.py` (`add_event` stores `rule`;
  `events_between(..., zone: ZoneInfo)` expands), `src/personal_organizer/messaging/calendar.py`
  (passes the tenant's zone), `src/personal_organizer/messaging/recurrence.py`
  (`describe_rule(rule, language: str = "en")` with Hebrew)
- Test: `tests/db/test_events_repository.py`, `tests/unit/test_recurrence.py`

**Interfaces:**
- Produces: the expanded occurrences as `CalendarEvent`s, each with `external_id =
  f"{row_id}:{start:%Y%m%dT%H%M}"`, `rule` set, and the series' duration.

- [ ] **Step 1: Write failing tests:** `test_native_series_expands`,
  `test_native_series_keeps_wall_time_across_dst`, `test_open_series_expands_only_window`,
  `test_series_until_respected`, `test_describe_hebrew` (`describe_rule("FREQ=WEEKLY;BYDAY=MO,WE",
  "he") == "כל יום שני ורביעי"`).
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. Query the rule rows with
  `starts_at < end` and no lower bound, then expand with `rrule.between` on naive local times.
  **Step 4:** Run them. Expect PASS.
- [ ] **Step 5: Commit** `feat(calendar): repeating events in Remi's calendar`.

### Task 2: Google creates with recurrence

**Files:**
- Modify: `src/personal_organizer/providers/calendar/composio.py` (`create_event`)
- Test: `tests/unit/test_composio_calendar.py`

- [ ] **Step 0: Verify the argument name.** Check `GOOGLECALENDAR_CREATE_EVENT`'s `recurrence`
  parameter at the pinned toolkit version (context7 or Composio docs). Record the finding in
  the commit message. If the action has no recurrence, stop and report to the controller:
  the fallback, `GOOGLECALENDAR_EVENTS_INSERT` or similar, is a decision.
- [ ] **Step 1: Write failing tests:** `test_create_recurring_passes_rrule` (arguments
  include `recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]`; Google wants `UNTIL` in UTC
  `Z` form, so convert it), and `test_create_recurring_rejected`.
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `feat(composio): create a repeating Google event`.

### Task 3: `propose_event` with `repeat`, several per message

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py` (`TOOLS.propose_event`: `repeat`, and the
  description now says "up to 8 per message; for a timetable, one per class"; `check_event`
  validates `repeat` with `check_rule`; `found` is capped at `MAX_PROPOSALS`; `_payload` and
  `_stored` handle the list and the legacy shape; `_confirmed` adds each event and joins the
  lines; `event_summary` adds the repeat line), `src/personal_organizer/messaging/onboarding_text.py`
  (plural `confirm_question` / `event_added` if the copy needs it)
- Test: `tests/unit/test_agent_tools.py`, `tests/db/test_pending_flow.py`

**Interfaces:**
- Produces: `_payload(events: Sequence[CalendarEvent], inbox_id) -> dict` and
  `_stored(payload, zone) -> list[CalendarEvent]`.

- [ ] **Step 1: Write failing tests:** `test_propose_repeating`, `test_bad_repeat_is_error`,
  `test_timetable_five_proposals_one_confirm`, `test_ninth_proposal_refused`,
  `test_legacy_payload_still_confirms`, `test_no_drops_all`.
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), along with the full suite.
- [ ] **Step 5: Commit** `feat(agent): propose repeating events, a whole timetable at once`.

### Task 4: Prompt, evals, PR, staging

**Files:** the `_PROMPT` rule on adding events in `messaging/agent.py`,
`tests/evals/test_agent_evals.py`

- [ ] **Step 1: Prompt:** "For something that repeats, give propose_event a repeat. For a
  timetable, propose each class, all in this one message, and say in a few words what you
  understood ('Your 5 classes, every week until the end of January?')."
- [ ] **Step 2: Evals:** `test_gym_mon_wed` (one proposal, `FREQ=WEEKLY;BYDAY=MO,WE`),
  `test_hebrew_timetable_all_classes` (the 2026-10-26 timetable gives one proposal per class,
  then "כן" adds all), `test_until_end_of_semester` (an `UNTIL` is set).
- [ ] **Step 3: Run** them 3 times. Each passes at least 2 of 3.
- [ ] **Step 4:** Draft PR (`Closes #53`). Staging: send the timetable to a tenant without
  Google and to one with Google. Check the briefing and "what do I have Wednesday?" next week,
  and on the phone that Google shows the series.
- [ ] **Step 5:** File the new issue "change and delete events (single and series)", with what
  this PR leaves out.
