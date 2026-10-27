# #51 Okaying Calendar Sharing in the Group Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** someone asks the group "when is everyone free Friday?". Remi answers for everyone who
has okayed sharing, and asks the others, by name, to okay it right there. When one of them says
"I approve" (or "מאשרת", or "ok go ahead"), Remi turns sharing on for that person and answers
the question again, now including them.

**Architecture:** `who_is_free` marks the members it couldn't read as *asked*
(`group_members.asked_at`). Remi reads every group message. An unaddressed message from a
member with an open ask goes to a small yes/no classifier, the same kind as `confirm.decide`. A
yes sets sharing and then runs the normal addressed group turn on that message, with a prompt
line saying the person just okayed. The model re-asks `who_is_free` from the group context.
Approval is stored per person and per group, and only from the person's own message, by code.

**Tech Stack:** Python 3.14, SQLAlchemy async, Alembic, Anthropic (Haiku 5.5) via `LLMProvider`.

**Spec:** GitHub issue #51; current behaviour in
`docs/superpowers/specs/2026-10-21-iteration-07-family-groups-design.md` ("Okaying a group",
"Asking who's free").

## Global Constraints

- `ASK_TTL = timedelta(hours=48)`. An ask older than that is closed, and their messages are no
  longer classified.
- Only the sender's own row is ever changed (`turn.tenant_id`). "Dana approves" from someone
  else changes nothing.
- An unaddressed message costs one classifier call at most, and only when its sender has an open
  ask. If the sender also has an open outing, the approval check runs first, and a "no" falls
  through to `read_answer`.
- Non-members are never asked: they are not in `group_tenants`.
- Product voice: Remi asks in plain words ("Eden, Noa: I need your okay before I look at your
  calendars. Just say so here."). Never "reply 'I approve'" as a required keyword, and no menus.
  Copy goes in `messaging/groups/prompt.py`, in Hebrew and English.
- Migration revision `0011_group_asks`, `down_revision = "0010_reminder_series"`. The worktree
  starts from Track A's Task 2 commit.

## Review Focus

1. **Someone approves on behalf of another person** ("Mom says ok"). Expect only the sender's
   own sharing to change, and only if the sender has an open ask. Pinned in Task 3
   (`test_approval_changes_only_sender`).
2. **A member chats normally while asked** ("lol", "I'll bring cake"). Expect one classifier
   call, a "no", and no reply. Pinned in Task 3 (`test_non_approval_is_silent`).
3. **An approval after the ask expired.** Expect no classifier call and nothing to happen.
   Pinned in Task 2 (`test_ask_expires`).
4. **The classifier's model is down.** Expect a retry, and silence on the last attempt, like
   `read_answer`. Pinned in Task 3 (`test_approval_llm_down_last_attempt_is_silent`).
5. **"Stop sharing my calendar here" after approving.** Expect it to still work through
   `share_my_calendar`, and to clear `asked_at`. Pinned in Task 2
   (`test_set_sharing_clears_ask`).

---

### Task 1: Migration 0011, model, repository

**Files:**
- Create: `alembic/versions/0011_group_asks.py`
- Modify: `src/personal_organizer/db/models/tenant.py` (`GroupMember.asked_at`),
  `src/personal_organizer/db/repositories/groups.py`
- Test: `tests/db/test_groups_repository.py`

**Interfaces:**
- Produces: `GroupMember.asked_at: datetime | None`.
  `ask(session, tenant_id, group_id, now) -> None` sets `asked_at = now` on a non-sharing
  membership. `open_ask(session, tenant_id, group_id, now) -> bool` is true when the person is
  not sharing and `asked_at > now - ASK_TTL`. `set_sharing` also sets `asked_at = NULL`.
  `ASK_TTL` lives in `groups.py`.

- [ ] **Step 1: Write failing tests:** `test_ask_opens`, `test_ask_expires` (49 h later it is
  closed), `test_set_sharing_clears_ask`, `test_ask_ignores_sharing_member`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the migration (one column, `timestamptz NULL`), the model and the
  repository.
- [ ] **Step 4: Run** the tests and `test_models_match_migrations.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(groups): remember who Remi asked to okay sharing`.

### Task 2: `who_is_free` asks the members it can't see

**Files:**
- Modify: `src/personal_organizer/messaging/groups/turn.py` (`free_busy`)
- Test: `tests/db/test_group_turn.py`

**Interfaces:**
- Consumes: `ask` (Task 1).
- Produces: `free_busy(db, group_id, start, end, *, calendar, now: datetime) -> dict`, with the
  same shape as before. Every name in `not_shared` has now been asked.

- [ ] **Step 1: Write the failing test** `test_free_busy_asks_not_shared`: after `free_busy`,
  `open_ask` is true for the non-sharer and false for the sharer.
- [ ] **Step 2: Run it.** Expect FAIL.
- [ ] **Step 3: Implement.** Call `ask` in the non-sharer's own `tenant_session`, the one
  `free_busy` already opens for their name. Pass `turn.now` from `group_turn`.
- [ ] **Step 4: Run it.** Expect PASS.
- [ ] **Step 5: Commit** `feat(groups): asking who's free asks the others to okay sharing`.

### Task 3: Reading an approval said in the group

**Files:**
- Create: `src/personal_organizer/messaging/groups/approval.py`
- Modify: `src/personal_organizer/messaging/groups/turn.py` (split the addressed part of
  `group_turn` into `_answer`; the unaddressed branch), `src/personal_organizer/messaging/groups/prompt.py`
  (`group_prompt(..., approved: bool = False)`)
- Test: `tests/unit/test_group_approval.py`, `tests/db/test_group_turn.py`

**Interfaces:**
- Consumes: `open_ask`, `set_sharing`.
- Produces: `approves(text: str, *, llm: LLMProvider) -> bool`. Its system prompt asks whether
  the writer okays Remi showing **their own** calendar's busy times in this group, and the
  model answers `YES` or `NO`. `max_tokens` and the `finish_reason == "max_tokens"` handling
  are as in `confirm.decide`, and anything other than `YES` is `False`.
  `_answer(row, channel, turn, *, db, llm, calendar, last_attempt, approved: bool) -> None` is
  the body of today's addressed path.

- [ ] **Step 1: Write failing tests.** Unit, with a scripted LLM: `test_yes_is_true`,
  `test_anything_else_is_false`, `test_truncated_is_false`. DB:
  `test_approval_turns_sharing_on_and_answers` (an unaddressed "מאשרת" from an asked member
  sets sharing, and a `GROUP_REPLY` is sent), `test_non_approval_is_silent`,
  `test_not_asked_costs_no_call` (the LLM is not called when there is no open ask and no open
  outing), `test_approval_changes_only_sender`, `test_approval_llm_down_last_attempt_is_silent`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.** In the `not turn.addressed` branch: when `turn.tenant_id` is set
  and `open_ask`, call `approves`. On `True`, call `set_sharing(..., True)` in the sender's
  session, then `_answer(..., approved=True)`, which appends the `group_shared_on` line as the
  tool path does. Otherwise call `read_answer` as today. `LLMUnavailableError` is re-raised
  unless `last_attempt`.
- [ ] **Step 4: Run** the tests plus `tests/db/test_group_turn.py` and `test_group_gate.py`.
  Expect PASS.
- [ ] **Step 5: Commit** `feat(groups): an okay said in the group turns sharing on and answers again`.

### Task 4: Prompt copy and evals

**Files:**
- Modify: `src/personal_organizer/messaging/groups/prompt.py`
- Test: `tests/evals/test_group_evals.py`

- [ ] **Step 1: Prompt.** Replace the not_shared bullets under "Who is free" with: the people in
  not_shared have just been asked. Tell the asker in a few words that Remi needs their okay
  first, then ask those people by name to just say so in the group. Never require exact words.
  Answer with who you can see, now. With `approved`, add: "{asker} just okayed sharing here,
  after you asked. Thank them in a word and answer the group's last who's-free question again,
  with everyone who has okayed so far." Remove the line "If nobody in the group shares yet …
  anyone set up with you can tell you …", which the new flow covers.
- [ ] **Step 2: Evals:** `test_free_with_non_sharer_asks_them_by_name` (English and Hebrew;
  the reply names the non-sharer and contains no "reply"/"השב"/"1."),
  `test_hebrew_approval_answers_again` ("מאשרת" leads to a reply that mentions her busy
  times), `test_ok_go_ahead_counts`.
- [ ] **Step 3: Run** `uv run pytest -m eval tests/evals/test_group_evals.py` 3 times. Each
  passes at least 2 of 3.
- [ ] **Step 4:** Draft PR (`Closes #51`), rebased on main after Track A merges. Staging: check
  that `WHATSAPP_WEBHOOK_IGNORE_JIDS` doesn't drop `@g.us`. In a test group, a non-sharing
  member asks "Remi when is everyone free Friday?", replies "ok go ahead", and Remi re-answers.
