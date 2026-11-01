# #63: Change and delete calendar events — design

Status: draft for review, 2026-10-29. Issue: #63. Depends on #50 (native calendar, PR #70) and
#53 (repeating events, PR #71), both merged.

## Why

Today Remi can only add events. The prompt says "You can't change or delete events yet", so a
person whose plans change has to open their calendar app, and a repeating event made by
mistake has to be removed by hand with the runbook's SQL. The user's bar: **whatever the person
wants to do to an event, they can do it in plain words**, e.g. "my Sunday class is cancelled
next week, remove it just for then", or "move it".

## What a person can do

- **One event:** move it (time or day), rename it, change or clear its place, or delete it.
- **A repeating event**, three ways:
  - **just one date**: "class is cancelled next Sunday" removes that Sunday only; "next
    Sunday's class is at 18:00" moves that one only;
  - **from a date on**: "from now on the class ends at 19:00" leaves past weeks alone and
    changes this one and the ones after; "I'm stopping the class after December" ends it there;
  - **the whole series**: "delete the weekly class", "the class is called Pilates now".
- Remi infers which of the three from the words. It asks one short question only when it truly
  can't tell (e.g. "cancel class" with no date, for a weekly class). A date named ("next
  Sunday", "on the 12th") means just that date; "from now on" / "from next week" means from that
  date on; naming the event with no date and "delete"/"cancel" means the whole series only when
  the words say so ("the weekly class", "all of them", "for good").

## How it feels (product voice, CLAUDE.md)

- Remi looks the event up first, with `list_events` in the same turn, and never guesses which
  one. If several match ("the meeting" on a day with three), it asks which one in plain words,
  by title and time, never by number.
- **Confirmation stays**, because it writes somewhere real. It works exactly as adding does
  today: the model's short reply, then a code-written summary of the change and one question,
  and the answer is read from natural language by `confirm.decide`. Examples:
  - "Dentist — Thursday 29 Oct, 10:00–11:00 (was 09:00–10:00)" + "Make this change?"
  - "Pilates — only Sunday 1 Nov" + "Remove it from your calendar?"
  - "Pilates — every Sunday from 1 Nov: 18:00–19:00 (was 18:00–19:30)" + "Make this change?"
  - Hebrew written as Hebrew ("לשנות?", "להסיר מהיומן?"), not translated.
- **Someone else's invite** (Google event whose organizer is not them): Remi says it's someone
  else's event, and the confirm question says so too, so a yes is a knowing yes: "This is an
  invite from someone else. Remove it from your calendar anyway?" Removing it only takes it off
  *their* calendar. Moving or renaming someone else's invite is not offered: Remi says only the
  person who sent it can change it, and offers to remove it instead (Google doesn't let a guest
  move the organizer's event).
- Nobody else is notified. Changes in Google are made with no update emails to guests
  (`sendUpdates: none`).
- Fixed replies after the write, like `event_added` today: what changed, in a line. A partial
  failure (Google down) says plainly that nothing changed.

## Scope and non-goals

In:
- Both stores: Remi's own `events` (native, #50/#53) and Google via Composio (the change lands
  in Google, then the stored copy is refreshed, as `TenantCalendar.add` does).
- One proposed change per message. (Several at once, like a timetable, is not needed: "cancel
  Sunday and Tuesday" can be two turns or the agent picks the series.)
- Deleting the runbook's "Removing a repeating event" SQL section, replaced by a sentence.

Out:
- Notifying guests, or editing guests, descriptions, reminders or colours.
- Turning a timed event into an all-day one or back (a change keeps the event's `all_day`).
- Group outings: once in someone's calendar they are ordinary events and can be changed like
  any other. Nothing tells the group. (Builder's/my call; the issue said "out of scope unless
  trivial", and it is trivial this way.)
- Undo.

Anti-goals (failure even if it "works"):
- The model picking an event by an id it invented, or changing a different event than the one
  shown in the confirm.
- A "yes" to one change applying another (stale or replaced proposal).
- Removing a whole series when the person meant one date.
- A menu ("reply 1 for this one, 2 for all").

## How it is built (decided)

### Model tools

- `list_events` numbers what it returns (`"n": 1, …`), like `list_reminders`. Each shown event
  also says whether it repeats (`"repeats": "every Sunday"`, from `describe_rule` /
  Google's series) and, for Google, `"invite_from_someone_else": true` when it is. The list of
  `(n → event)` is kept for the turn only; numbers are never shown to the person.
- New tool **`propose_change`**: `n` (from a `list_events` in this turn), `which` (`this` |
  `following` | `all`; only for a repeating event, and required for one), and either
  `delete: true` or any of `title`, `start`, `end`, `location`, `repeat` (local times as in
  `propose_event`; `start` without `end` keeps the length). `repeat` is a new RRULE, checked
  with `check_rule`/`first_at` like `propose_event`'s, and only goes with `following` (a new
  pattern starts from a date; rewriting past weeks is never wanted): "the class is on Tuesdays
  now" is a new pattern, not a shifted start, because a
  `BYDAY=SU` rule ignores a Monday `starts_at` (dateutil keeps producing Sundays). A start that
  moves a weekly series to another weekday without a `repeat` is an error the model reads,
  telling it to send the new `repeat`. (My call: the user's bar is "whatever they want".) Like `propose_event` it writes
  nothing: code builds the summary + question under the reply and stores a pending action of a
  new kind, `change_event`. An `n` not listed this turn is an error the model reads.
- The prompt line "You can't change or delete events yet" becomes rules for the above: find it
  with `list_events` first, infer `which` from the words, ask one short question only when it
  can't be inferred, and never write the summary or the question.

### Pending and confirm

- Kind `change_event`; payload: the target (store, `external_id`, series id, the occurrence's
  original start, `which`), the event as it was (for the summary and a staleness check), and the
  new values, in local wall time (D6), plus `proposed_by`.
- `confirm.decide`'s prompt is worded per kind ("whether to make this change / remove this").
  Its answers stay ADD (= go ahead) / CANCEL / OTHER, so `_confirmed` and the claim-before-write
  rule (ADR 0003) are unchanged: claim, commit, then write once, never retried.
- Staleness: on ADD, if the event is no longer there as it was (deleted or moved meanwhile, e.g.
  by an hourly sync), nothing is written and a fixed line says it changed in the meantime.

### Native store (`events`)

Migration 0013 adds to `events`:
- `series_id text null`: on a `remi` row, the series row's `external_id` when the row is one
  date of that series that was changed on its own; on a `google` row, Google's
  `recurringEventId`.
- `original_start timestamptz null`: on a changed single date of a `remi` series, which date of
  the series it replaces.
- `skipped timestamptz[] not null default '{}'`: on a `remi` series row, the occurrence starts
  that no longer happen (EXDATE).
- `invited boolean not null default false`: a `google` row whose organizer is someone else
  (`organizer.self` not true). No organizer name or address is stored (D6).

Operations:
- **one event** (no rule): update or delete the row.
- **series, this date**: delete → add its start to `skipped`. Change → add to `skipped` and
  insert a one-off row with `series_id` / `original_start`.
- **series, from a date on**: set the series row's `UNTIL` to just before that occurrence (or
  delete the row if it is the first); for a change, insert a new series row from that occurrence
  with the new values and the old `UNTIL`. Skips and single-date changes on or after the split
  are dropped (the new rule replaces them). (My call.)
- **series, all**: update the series row (a time change shifts every occurrence by the same
  amount, keeping single-date changes as they are), or delete it and every row with its
  `series_id`.
- `events_between` / `occurrences` skip `skipped` starts; a changed single date shows as its own
  row. Its `external_id` stays its row id, its occurrences keep the `{id}:{YYYYMMDDTHHMM}` form
  #53 uses.

### Google store (Composio)

- `list_events` (`singleEvents=True`) also keeps each item's `recurringEventId` →
  `series_id` and `organizer.self` → `invited`. Still no description, attendees or organizer
  identity. Google rows keep no rule: the model sees `"repeats": true` for a Google instance,
  and the master's `recurrence` is fetched only inside `propose_change`, for the summary
  ("every Sunday" via `describe_rule` when it parses, plain "repeating" when it doesn't).
  (My call: no extra Composio calls on every sync.)
- A rule outside our subset (COUNT, "first Monday", EXDATE lines from Google's own app) can
  still be ended "from a date on": only its `UNTIL` is rewritten (COUNT dropped), every other
  part and line kept as Google sent it. *Changing* such a series from a date on needs its
  pattern copied into the new series, which we can't do faithfully, so `propose_change` refuses
  it unless a new `repeat` is given, and the model offers that date or all of them instead.
- **one event / series, this date**: patch or delete the instance's own id.
- **series, all**: patch or delete the `recurringEventId` (the master).
- **series, from a date on**: read the master (`recurrence`), patch its RRULE's `UNTIL` to
  before the occurrence; for a change, create a new series from that occurrence with the change
  and the old end. Two writes: if the second fails, the reply says the old series now ends
  there and the new one wasn't added (nothing is retried).
- Deleting an invite as a guest removes it from their calendar only (Google's behaviour).
  Whether Google shows the organizer a decline when a guest deletes through the API is to be
  checked while building and said in the PR. For a repeating invite only `this` and `all` are
  offered: a guest can't end the organizer's series early, so "I'm not going from next week"
  gets "I can remove just next week, or the whole series from your calendar" in plain words.
  (My call.)
- Partial updates only, through `GOOGLECALENDAR_PATCH_EVENT` (checked against the pinned
  `20261010_00` on 2026-10-29: `calendar_id`, `event_id`, `summary`, `location`, `start_time`,
  `end_time` (RFC 3339, or `YYYY-MM-DD` for all-day, exclusive end), `timezone`, `recurrence`
  (replaces the lines), `send_updates` (`all`/`externalOnly`/`none`)). Delete is
  `GOOGLECALENDAR_DELETE_EVENT` (`calendar_id`, `event_id`, `send_updates`); the master is read
  with `GOOGLECALENDAR_EVENTS_GET` (`calendar_id`, `event_id`). `CREATE_EVENT` takes
  `recurrence` and `send_updates` at the pinned version too (closes #53's open check).
- **Google no longer connected** (account revoked, Composio off) while `google` rows are still
  stored: nothing is written; the reply is the fixed reconnect line with a link, as
  `calendar_disconnected` on add. Never edit the local copy only. (My call.)
- After any write, `TenantCalendar` refreshes the affected window so Remi's copy matches at once.
- Every write sends `send_updates: "none"`.
- The model never chooses the account, calendar or id: as in D2, arguments are built in
  `composio.py` from typed parameters.

## Constraints

- Keep the layers: `CalendarProvider` (interface) gains `update_event` / `delete_event`;
  `Calendar` / `TenantCalendar` / `InMemoryTenantCalendar` gain `change` / `remove`;
  `messaging/agent.py` owns tools and prompt; voice lives in `onboarding_text.py` /
  `confirm.py` / the agent prompt.
- Tenant isolation: everything through `tenant_session` + `scoped`, RLS on the new columns comes
  with the table.
- Evals: new cases (EN + HE) for each of the six (one/series × this/following/all, change and
  delete), the "someone else's invite" path, and an ambiguous "cancel class" that must ask.
  Judged per case, 2 of 3 (the `run-evals` job is red whenever any case fails).
- Tests: repository tests on a real DB; Composio argument tests with recorded fixtures; the
  pending flow end to end with `InMemoryTenantCalendar`.

## Left to the builder

- Exact tool wording, exact summary layout for "was … / now …", fixed reply copy (within the
  voice rules), how `n` is shown, how the pending payload is laid out.
- Whether a time change to "all" on a native series needs anything beyond shifting
  `starts_at`/`ends_at` and the `skipped` list by the same delta.
- How a stale proposal is detected (compare start+title, or re-read by id).
- Migration details (indexes, whether `series_id` needs one).
