# Iteration 05 design — reminders and the morning briefing

**Source:** the 2026-10-07 re-plan, item 3. Iteration 04 shipped its "confirm → execute" for
calendar events. This iteration ships the rest: reminders and a morning briefing. Voice notes
and memory (item 4) come after.

**Decided with the user, 2026-10-15:**
- Both features in this iteration, in two PRs.
- The briefing is on by default, at 07:30 local time. "You're all set" says so, and plain
  words change or stop it.
- A briefing goes out on empty days too, as one short line.
- No dedicated snooze. A natural "remind me again in an hour" after a reminder already works,
  because the reminder is in the history. No prompt line or eval is added for it.

## Objective and Done-When

> A member writes "תזכיר לי מחר בשש להתקשר לאמא" or "remind me at 6 to call mom". The bot
> answers like a person ("Sure, I'll remind you tomorrow at 6 to call mom"), and at 6 their
> time the reminder arrives. "Cancel that" and "what reminders do I have?" work. Every morning
> at 07:30 their time they get a short, warm look at today's events and reminders. "Make it 8"
> or "stop the morning message" changes it.

| Done-When | Where it is proved |
|---|---|
| A reminder set in chat is sent once, at its time, in the tenant's zone | `tests/db/test_reminders.py` (due query, send, re-run sends nothing new). Staging: set one two minutes out |
| List and cancel work from natural language | `tests/unit/test_reminder_tools.py`, plus evals in Hebrew and English |
| A reminder is in the history, so "done, thanks" reads right | `tests/db/test_reminders.py` asserts the outbound `messages` row |
| A briefing goes out once per local day, at or after `briefing_at`, never more than 2 h late | `tests/db/test_briefing.py` (zones, the window, `briefed_on`, re-run) |
| The briefing survives a model or calendar outage | `tests/db/test_briefing.py` with a failing fake LLM and calendar: the fixed fallback is sent |
| Tenants never see each other's reminders | `tests/db/test_isolation.py` gains `reminders` |

## Approach

The source of truth is a table plus a once-a-minute tick, not one Procrastinate job per
reminder.
- A per-reminder job would need the table anyway, to list and cancel.
- A daily briefing would become a chain of jobs that each schedule the next, which breaks
  when one link fails.
- With the tick, a deploy or a dropped job loses nothing: the next minute picks it up.
- Timing is accurate to about a minute, which is enough.

## Data (migration 0007)

**`reminders`** (`TenantMixin`, under RLS like every tenant table):

| column | type | notes |
|---|---|---|
| `id` | uuid | |
| `text` | text | what to remind, at most `MAX_FIELD` (200) characters |
| `due_at` | timestamptz | UTC |
| `status` | text | `pending`, `sent` or `cancelled` |
| `created_at` | timestamptz | |

The index is on `(status, due_at)`.

**`tenants`** gains two columns:
- `briefing_at time` (local time). It defaults to `'07:30'`, and `NULL` means off. Existing
  active tenants get the default too, which is what the user chose.
- `briefed_on date`: the tenant's last local day a briefing was sent.

**Two `SECURITY DEFINER` functions,** owned by `app_definer` like `resolve_tenant` (D1). The
tick runs as `app_user`, which sees no tenant's rows outside `tenant_session`. These functions
return ids only, never content:
- `due_reminders(now timestamptz) → table(reminder_id uuid, tenant_id uuid)`
  - Matches `status = 'pending'` and `due_at <= now`, for tenants with `status = 'active'`.
- `due_briefings(now timestamptz) → table(tenant_id uuid, local_day date)`
  - Matches active tenants with `briefing_at IS NOT NULL` and a non-null `timezone`, where
    `local = now AT TIME ZONE timezone`.
  - Due when `local::time >= briefing_at` and `local::time < briefing_at + interval '2 hours'`.
    Past the 2 hours, a stale briefing is skipped.
  - Also requires `briefed_on IS DISTINCT FROM local::date`.
  - The window never wraps past midnight, because `set_briefing` accepts only 04:00 to 22:00.

## The agent's new tools

These go in `messaging/agent.py`, next to `list_events` and `propose_event`. None of them asks
for a confirm: they write only our own table, and CLAUDE.md keeps a confirm only for writes
to somewhere real.

- **`set_reminder(text, at)`**
  - `at` is a naive local time, as with the calendar tools (D6).
  - Errors the model reads: a time in the past, a time more than a year out, or empty or
    over-long text.
  - Returns `ok` with the local time it stored, so the model can say it back.
- **`list_reminders`**: the pending reminders, as `{id, text, at}` in local time.
  - The `id` is a short index valid for this turn only, never the UUID.
- **`cancel_reminder(id)`**: takes an index from `list_reminders` in the same turn.
  - The tool description says to list first.
- **`set_briefing(time | "off")`**: takes a local `HH:MM` between 04:00 and 22:00, or `off`.
  - Outside that range, the model reads an error asking for another time.

Prompt change in `_PROMPT`: add a short "Reminders and the morning message" rule. It says the
four tools exist, that a vague time gets a sensible default the reply states ("tonight" →
20:00, "in the morning" → 09:00), and that the model must never say a reminder is set unless
`set_reminder` returned `ok`.

## Sending

All of this lives in `worker/tasks/schedule.py` and `messaging/reminders.py`.

**The tick.** `@app.periodic(cron="* * * * *")`, on `maintenance`, with a `queueing_lock` so
ticks never pile up.
- It calls both definer functions.
- It defers one `schedule:reminder(reminder_id, tenant_id)` or
  `schedule:briefing(tenant_id, local_day)` job each, on `agent`.
- The jobs carry ids only (ADR 0001), with `queueing_lock` set to the send key so a slow
  minute never queues the same send twice.

**`schedule:reminder`**
1. In `tenant_session`, load the reminder. If it is not `pending`, stop.
2. Pick the channel and phone the way `announce_connected` does (`latest_inbound_channel`,
   `phone_of`). With either missing, log it and stop.
3. `send_once(idempotency_key=f"reminder:{id}", kind="reminder")` sends a fixed line, with
   no model call.
   - English: "Reminder: {text}".
   - Hebrew: "תזכורת: {text}".
   - The final wording is reviewed against the voice rules in `onboarding_text.py`.
4. In one transaction, set `status = 'sent'` and record the outbound `messages` row.
   - `record_outbound` takes `inbox_id: UUID | None`. The column is already nullable.

A reminder that fires late (the worker was down) still goes out. A missed reminder is worse
than a late one.

**`schedule:briefing`**
1. Load the tenant. If it is not active or `briefed_on == local_day`, stop.
2. Read `local_day`'s events through `list_events` (00:00 to 00:00 next day), plus that day's
   pending reminders.
3. One model call with no tools. Its own short prompt gives the tone, the language, and the
   events and reminders as JSON data, never instructions. It returns two to four friendly
   lines.
   - On an empty day the model writes one line. No events are invented: the reply is dropped
     if it holds a URL, as in `respond`.
4. Fallbacks:
   - **Model unavailable on the last attempt, or refused:** a fixed list built by code (the
     title and time of each event, then each reminder), under a fixed opening line in each
     language.
   - **Calendar not connected:** reminders only, plus the existing `calendar_disconnected`
     line.
   - **Calendar unavailable:** raise for a retry, then on the last attempt send reminders only.
5. `send_once(idempotency_key=f"briefing:{tenant_id}:{local_day}", kind="briefing")`.
6. Set `briefed_on = local_day` and record the outbound message, in one transaction.

**Onboarding.** `all_set` in both languages gains one sentence: in English, "I'll send you a
quick look at your day every morning at 7:30. Just tell me if you'd like another time, or
none." The Hebrew is written as Hebrew.

## Errors and safety

- **At most once:** every send is keyed (ADR 0003). A re-run after the send sends nothing,
  and the status and `briefed_on` updates are idempotent.
- **No content outside RLS:** the definer functions return ids, and job kwargs are ids. The
  reminder text lives only in `reminders` and `messages`.
- **Prompt injection through event titles:** the briefing call has no tools and sees titles
  only as data, so the worst case is an odd sentence.
- A tenant who is suspended or deleted between the tick and the job is skipped by the job's
  own check.

## Testing

- `tests/db/test_reminders.py`: `due_reminders` across zones and statuses, the send, the
  re-run, the history row, and a cancelled reminder not sent.
- `tests/db/test_briefing.py`: `due_briefings` before, inside and after the window,
  `briefed_on`, `briefing_at` off, the empty day, and the model and calendar fallbacks.
- `tests/unit/test_reminder_tools.py`: argument checking, local-time handling and the
  per-turn index for cancel.
- `tests/db/test_isolation.py`: `reminders` added to the table list.
- `tests/evals/test_agent_evals.py`: set in Hebrew and English with relative times ("מחר
  בשש", "in 20 minutes"), "what reminders do I have", "cancel the one about mom", "make the
  morning message 8", "stop the morning message".

## PRs

1. **Reminders:**
   - Migration 0007: `reminders`, `due_reminders`, and both `tenants` columns, so there is
     one migration.
   - The three reminder tools and the prompt rule.
   - The tick and `schedule:reminder`.
2. **Briefing:**
   - `due_briefings`, the `set_briefing` tool and `schedule:briefing` with its fallbacks.
   - The tick's second half.
   - The `all_set` line.

## Out of scope

- Recurring reminders, skipping weekends or Shabbat, a dedicated snooze, and reminders tied
  to an event ("10 minutes before the meeting").
- Briefing content beyond today's events and reminders (weather, tomorrow).
- Meta Cloud API delivery: its 24-hour window would need templates, and Meta is parked. GOWA
  only.
