# Open Issues Roadmap (#50–#53, #55, #56)

> **For agentic workers:** this is the index. Each issue has its own plan, linked below. Execute
> them with superpowers:subagent-driven-development, one track per worktree, in the waves below.

**Goal:** close every open issue on `danielshefer1/remi-po` with as little wall-clock time and
as few subagent runs as possible, without breaking main or staging.

## Decisions taken with the user (2026-10-27)

- **#50 native calendar:** Remi keeps its own `events` table.
  - **Default: one-way import.** Connecting Google imports the next 90 days, and an hourly
    `calendar:sync` job keeps the copy fresh. Reads use the stored copy.
  - **Opt-in per member: live reading ("merge on read").** Off by default. When on, every read
    of a window first refreshes that window from Google, and falls back to the stored copy if
    Google is down. A member turns it on or off by asking Remi in plain words. Remi makes the
    option known: the "you're connected" message mentions it, and the agent offers it when a
    recent Google change is missing.
  - There is no two-way sync. While Google is connected, new events go to Google and their copy
    is stored. Without Google they are stored natively.
- **#53:** create repeating events only. Editing or deleting events (single or a series) is a
  new issue, done after this roadmap.
- **#55:** the cheap model decides when to hand off. It gets a `hand_off` tool. It can call it
  only before any write, and then the turn re-runs on the strong model. There is no up-front
  routing and no retry after a failure.

## Waves and tracks

| Wave | Track | Plan | Base | Migration | Runs alongside |
|---|---|---|---|---|---|
| 0 | CI evals workflow (new, optional) | below | main | none | Wave 1 |
| 1 | A: #52 repeating reminders | [52](2026-10-27-52-repeating-reminders.md) | main | `0010_reminder_series` | B, C |
| 1 | B: #51 approval in the group | [51](2026-10-27-51-group-approval.md) | A's Task 2 commit | `0011_group_asks` | A, C |
| 1 | C: #55 hand-off to a stronger model | [55](2026-10-27-55-model-handoff.md) | main | none | A, B |
| 2 | D: #50 native calendar | [50](2026-10-27-50-native-calendar.md) | main after Wave 1 merged | `0012_events` | E |
| 2 | E: #56 short, split replies | [56](2026-10-27-56-short-split-replies.md) | main after Wave 1 merged | none | D |
| 3 | F: #53 repeating events | [53](2026-10-27-53-repeating-events.md) | main after D merged | none (`events.rule` is in 0012) | none |

Why this order:
- **Migrations form one chain.** Each number is reserved here, and PRs merge in number order
  (A, B, then D). Track B starts from A's migration commit, so its `down_revision =
  "0010_reminder_series"` resolves in its tests. B rebases onto main once A merges.
- **#53 needs both #50 (the table) and #52 (`messaging/recurrence.py`).** It goes last.
- **Shared files.** Wave 1 tracks barely touch the same files. A edits the reminder tools in
  `agent.py`, C edits `respond()`'s tool loop and the end of `TOOLS`, and B stays in
  `messaging/groups/`. In Wave 2, D and E both edit `agent.py` and `groups/turn.py`, but
  different parts: D the calendar plumbing, E the send path and the "How you sound" prompt
  block. The controller merges E first, then rebases D, which is the larger diff.
- C (#55) ships in Wave 1 because it is small and independent. Its evals use the bulk-reminder
  case that A (#52) makes possible, so run C's evals after A merges.

## Execution protocol (every track)

1. **Worktree + DB.** Create the worktree with superpowers:using-git-worktrees, reset to the
   base in the table, and give the track its own test database (`po_t<n>` on the 5433 server,
   copied the way `po_cal` was copied from `po_g7`). Export **only** the `DATABASE__*`
   variables, pointed at it: exporting the whole `.env` breaks the env-reading tests. Inside a
   worktree, run tests with `PYTHONPATH=src`.
2. **Per task:** one fresh implementer subagent, then one fresh reviewer that checks the spec
   and code quality together. Fix and re-review until it is clean. Use Sonnet 5.5 implementers
   for repository, migration and plumbing tasks, and Opus 5.5 for tasks that write prompts or
   user-facing copy (CLAUDE.md product voice).
3. **Task gate:** `uv run pytest tests/unit tests/db -q && uv run ruff check src tests && uv run
   mypy src tests`, all clean.
4. **Track end:** one whole-branch review (Opus 5.5). Then the track's live evals:
   `AGENT__API_KEY=… uv run pytest -m eval tests/evals -k <track filter>`, 3 runs, each must
   pass at least 2 of 3. Then a draft PR. The user merges.
5. **After merge:** migrate staging before deploying (this repo's rule since iteration 03),
   run the plan's staging check, then `PYTHONHASHSEED=0 graphify update .`.
6. The controller keeps a ledger at `.superpowers/sdd/2026-10-27-<track>/progress.md` in each
   worktree (as in iteration 07).

## Wave 0 (suggested, not an issue yet): live evals in CI

Every track's merge gate is a live eval run, and today those run by hand. Add
`.github/workflows/evals.yml`, triggered by `workflow_dispatch` and by a PR label `run-evals`.
It runs `uv run pytest -m eval tests/evals` with the repo secret `AGENT__API_KEY`. The user sets
that secret. It costs one small task. Skip it if the user prefers running evals locally.

## Suggested additions and replacements

1. **New issue: change and delete events**, for single events and a whole series. It is split
   out of #53 and comes after this roadmap. Today the agent can't change or delete any event.
2. **Fired-reminder fix, folded into #52 Task 4.** `list_reminders` also returns the last 5
   reminders that already went off. This was agreed on 2026-10-26 but held back. #52 rewrites
   `list_reminders` anyway, and its issue requires that the fired-reminder dedupe keeps
   working. The user confirms before Track A starts.
3. **#53 needs several proposals in one message**, confirmed together. A weekly timetable is 5
   or 6 different repeating events, and `propose_event` allows one per message today. This is
   added to the #53 plan, though the issue doesn't say it.
4. **#51 needs group traffic to reach Remi on staging.** Staging GOWA had `@g.us` in
   `WHATSAPP_WEBHOOK_IGNORE_JIDS`. Check that before the staging step.
5. **Remi to Lena rename** (scoped earlier, not started). If it is happening, do it before Wave
   2, because #56 rewrites the same copy. Otherwise do it after Wave 3.
6. **Housekeeping before starting:** 10 stale `.claude/worktrees/agent-*` worktrees, about 20
   merged local branches, and the test databases `po_t1`–`po_t6`, `po_g7` and `po_cal`.

## Done when

All six issues are closed by merged PRs, each PR's live evals pass, each staging check passed,
and the "change and delete events" issue is filed.
