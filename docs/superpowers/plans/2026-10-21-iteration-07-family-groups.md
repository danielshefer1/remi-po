# Iteration 07 — Remi in the family group: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remi serves WhatsApp groups on the GOWA channel. It answers who is free, keeping event
titles out of the group. It sends event details privately to people who have been granted them,
turns an agreed plan into a poll that adds the event for each "in" vote, and follows up privately
with people who haven't answered.

**Architecture:** A group message is stored like any message, with its group JID. In
`handle_inbound` it branches to a new group gate (`messaging/groups/`). The gate keeps a short
rolling context per group, records which members are in it, and reads unaddressed messages only
to catch answers to an open outing. When Remi is addressed, it runs a group turn whose tools see
busy times without titles. Cross-tenant reads go through `SECURITY DEFINER` functions that return
ids only. Each person's calendar and rows are read in that person's own `tenant_session`.
Follow-ups ride the existing `schedule:tick`.

**Tech Stack:** Python 3.14, SQLAlchemy async + asyncpg, Alembic, Procrastinate, httpx,
Anthropic (Haiku 5.5) via `LLMProvider`, Composio calendar via `CalendarProvider`, GOWA v9.6.0.

**Spec:** `docs/superpowers/specs/2026-10-21-iteration-07-family-groups-design.md` (approved
2026-10-21). Read it first; this plan argues from it.

## Global Constraints

- Product voice (CLAUDE.md): every user-facing line warm and human, no menus, no "reply 1/2", no
  IDs or jargon. Hebrew written as Hebrew. Fixed lines go in `messaging/onboarding_text.py`
  (both languages; `tests/unit/test_onboarding_text.py` checks the key sets). Prompt text lives
  next to its prompt.
- **No event title, location or description reaches the model in a group turn, or any group
  send.** Enforced in code: the group tools return busy blocks built from `start`/`end`/`all_day`
  only.
- Remi speaks in a group only when addressed. An unaddressed message gets at most one small
  classifier call, and only when its sender has an open, unanswered outing in that group.
- Groups only on the `gowa` channel. The Meta parser never sets `group_id`.
- Busy/free shows only for members who okayed this group (`group_members.sharing`). Details go
  only to a viewer who was granted them by an owner who okayed this group.
- New tenant tables go under `enable_tenant_rls` and get a seed factory in
  `tests/db/test_isolation.py`. Cross-tenant reads use only the new definer functions, which
  return ids and booleans, never content.
- Constants (copy exactly): `CONTEXT_LIMIT = 20`, `CONTEXT_AGE = timedelta(hours=6)`,
  `FOLLOW_UP_WINDOW = timedelta(minutes=5)`, first follow-up no sooner than 3 hours after the
  poll, then at most one per 20 hours, only between 10:00 and 20:00 local time, never after the
  event starts.
- Poll options: English `"I'm in"` / `"Can't make it"`, Hebrew `"אני בפנים"` / `"לא מסתדר לי"`.
- Tests run from the repo root (`PYTHONPATH=src` inside a worktree). Every task ends with
  `ruff check src tests && mypy src tests` clean.
- After merging: `PYTHONHASHSEED=0 graphify update .`

## Review Focus

1. **A title leaking through the model.** For example, the asker writes "is Daniel at the
   dentist Friday?" and the model guesses. Expect the group reply to hold only what the tools
   gave it. Pinned in Task 5 (`test_group_turn_never_sees_titles`) and as an eval in Task 9.
2. **A group message from someone who isn't a Remi member**, including a phone that isn't a
   tenant, or a LID-only sender. Expect it in the context and answered if addressed, but never
   counted as a member, nudged, or given details. Pinned in Task 4
   (`test_stranger_in_served_group_is_context_only`).
3. **The same vote delivered twice, or "in" → "out" → "in".** Expect one calendar event at most.
   Pinned in Task 7 (`test_vote_in_twice_creates_one_event`, `test_vote_back_in_reuses_event`).
4. **Remi added to a group with no members.** Expect silence, no context stored, no model call.
   Pinned in Task 4 (`test_unserved_group_is_purged_silently`).
5. **An owner revoking a grant, or un-okaying the group, between the ask and the private
   message.** Expect no details. The grantor list is read when the details are sent. Pinned in
   Task 5 (`test_details_follow_current_grants`).

---

### Task 0: Day-1 checks on staging (with the user; no code)

The spec's risks 1–4. They decide the parser's field names and whether Task 4b is needed.
Railway was having an outage on 2026-10-21; wait for the user's go-ahead.

- [ ] **Step 1:** On the staging `gowa` service, set
  `WHATSAPP_WEBHOOK_IGNORE_JIDS=@newsletter,status@broadcast` (drops `@g.us`). Stop the staging
  `worker` so inbox rows keep `raw`. The api still stores them.
- [ ] **Step 2:** The user creates a test group with Remi's number and one other phone. In it,
  they send: a text, a text naming "Remi", a text @-tagging Remi, a quote-reply to a message
  from Remi. Then send a poll to the group:
  `POST /send/poll {"phone": "<group jid>", "question": "Test?", "options": ["Yes","No"], "max_answer": 1}`.
  The user votes on it, and also votes on the 2026-10-21 direct-chat poll again.
- [ ] **Step 3:** Read the rows via `railway ssh` on the api (recipe in memory
  `fired-reminder-prompt-fix`; `channel_inbox` is not RLS). Record, for each kind of message:
  `chat_id`, `from`, `from_lid`, `from_name`, how a mention appears (body text `@<digits>`, a
  `mentioned_jids`-style field, or both), `replied_to_id`, and the vote's `poll` object.
- [ ] **Step 4:** Save the payloads, with phones replaced by `+97250000000N` and names by
  "Dana"/"Noa", as `tests/fixtures/gowa/group_text.json`, `group_mention.json`,
  `group_reply.json`, `group_poll_vote.json`, `direct_poll_vote.json`. Restart the worker.
- [ ] **Step 5:** Note in the PR description: whether group senders arrive with a phone `from`
  (if only LIDs arrive, Task 4b is in scope), the mention shape, and whether `selected_options`
  is present. If it isn't, Task 7 uses the spec's fallback: plain message, answers read by the
  model.

### Task 1: Group messages, sender names and poll votes through the parser and inbox

**Files:**
- Modify: `src/personal_organizer/interfaces/channel.py` (`InboundMessage`)
- Modify: `src/personal_organizer/providers/channel/gowa/parser.py`
- Modify: `src/personal_organizer/messaging/ingress.py` (`_INSERT_INBOX`, `_inbox_params`)
- Modify: `src/personal_organizer/messaging/inbox.py` (`InboxRow`)
- Modify: `src/personal_organizer/db/models/channel.py` (`ChannelInbox`)
- Create: `alembic/versions/0009_groups.py` (this task adds only the two `channel_inbox`
  columns; Task 2 extends the same revision before anything merges)
- Test: `tests/unit/test_gowa_parser.py`, `tests/db/test_gowa_ingress.py`

**Interfaces:**
- Produces: `InboundMessage.group_id: str | None = None` (the provider's group id, e.g.
  `120363…@g.us`), `InboundMessage.sender_name: str | None = None`; the same two on `InboxRow`
  and as `channel_inbox.group_id`, `channel_inbox.sender_name` (`Text`, nullable).
- Produces: a vote parses as `message_type="poll_vote"`, `reply_id=<poll.poll_id>`,
  `text=<first selected option name>` (`None` for a cleared vote). Only `poll.type == "vote"`
  with `resolution_status` in `{"resolved", "partially_resolved"}`. Any other poll stays
  `message_type="poll"`.
- Produces: `POLL_VOTE: Final = "poll_vote"` exported from the parser.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_gowa_parser.py`, loading the
  Task 0 fixtures:
  - `test_group_text_keeps_group_and_sender`: `msg.group_id == "120363…@g.us"`,
    `msg.sender.phone == "+972500000002"`, `msg.sender_name == "Noa"`, `msg.text == "hi all"`.
  - `test_direct_message_has_no_group`: the existing direct fixture → `group_id is None`.
  - `test_group_poll_vote`: `message_type == "poll_vote"`, `reply_id == <poll_id>`,
    `text == "Yes"`, `group_id` set.
  - `test_cleared_vote_has_no_text`: `selected_options: []` → `poll_vote`, `text is None`.
  - `test_undecryptable_vote_is_plain_poll`: `resolution_status: "decrypt_failed"` →
    `message_type == "poll"`.
  - `test_newsletter_still_skipped`: `chat_id` ending `@newsletter` → `skipped == 1`.
  - Replace the existing test that asserts groups are skipped with these.
- [ ] **Step 2:** Run `pytest tests/unit/test_gowa_parser.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** `_DIRECT_SERVERS` stays. Add `GROUP_SERVER: Final = "@g.us"`, and
  accept a `chat_id` ending in either. `sender_name` comes from `from_name`. The `poll` branch
  of `_content` reads `payload["poll"]` as described under Interfaces. Update the module
  docstring and the `#:` comment above `_DIRECT_SERVERS`.
- [ ] **Step 4:** Add the two columns to `ChannelInbox`, `InboxRow.of`, `_INSERT_INBOX` and
  `_inbox_params`. Add them to `_PURGED` in `messaging/inbound.py`: a purge nulls the name too.
  Write the `channel_inbox` part of `0009_groups.py` (`op.add_column` ×2, and drop both in
  downgrade).
- [ ] **Step 5:** Add `test_group_message_stored_with_group` to `tests/db/test_gowa_ingress.py`:
  posting `group_text.json` stores a row with `group_id` and `sender_name` set.
- [ ] **Step 6:** Run `alembic upgrade head && pytest tests/unit/test_gowa_parser.py tests/db/test_gowa_ingress.py tests/db/test_models_match_migrations.py -v`.
  Expected: PASS.
- [ ] **Step 7: Commit** `feat(gowa): parse group messages, sender names and poll votes`.

### Task 2: Schema — groups, members, grants, outings, answers, and the definer functions

**Files:**
- Modify: `alembic/versions/0009_groups.py`
- Create: `src/personal_organizer/db/models/group.py` (non-tenant tables)
- Modify: `src/personal_organizer/db/models/tenant.py` (tenant tables, `Tenant.display_name`)
- Modify: `src/personal_organizer/db/models/__init__.py`
- Create: `src/personal_organizer/db/repositories/groups.py`
- Test: `tests/db/test_groups_repository.py`, `tests/db/test_isolation.py`,
  `tests/db/conftest.py` (`_CHANNEL_TABLES` gains `wa_groups`, which cascades to the rest)

**Interfaces:**
- Produces tables. Not tenant-scoped (like `channel_inbox`), read in `system_session`:
  - `wa_groups(id uuid pk, channel text, jid text, served_at timestamptz null, created_at)`,
    unique `(channel, jid)`.
  - `group_context(id, group_id fk→wa_groups cascade, sender_name text null, body text,
    sent_at timestamptz)`, index `(group_id, sent_at)`.
  - `group_polls(id, group_id fk cascade, inbox_id uuid unique, provider_message_id text null,
    title text, starts_at timestamptz, ends_at timestamptz, all_day bool, location text null,
    language text, in_option text, out_option text, status text check in ('open','closed')
    default 'open', created_at)`.
- Produces tenant tables (RLS):
  - `group_members(id, tenant_id, group_id fk cascade, sharing bool default false,
    seen_at timestamptz)`, unique `(tenant_id, group_id)`.
  - `detail_grants(id, tenant_id, viewer_tenant_id fk→tenants cascade, created_at)`, unique
    `(tenant_id, viewer_tenant_id)`. `tenant_id` is the owner.
  - `poll_answers(id, tenant_id, poll_id fk→group_polls cascade, answer text null check in
    ('in','out'), reason text null, claimed_at timestamptz null, nudged_at timestamptz null,
    updated_at)`, unique `(tenant_id, poll_id)`. `claimed_at` set means the event was (or may
    have been) added: at most once, as with `pending.claim`.
  - `tenants.display_name text null`.
- Produces model classes: `WaGroup`, `GroupContext`, `GroupPoll` in `db/models/group.py`;
  `GroupMember`, `DetailGrant`, `PollAnswer` in `db/models/tenant.py`.
- Produces definer functions, owned by `app_definer`, `EXECUTE` for `app_user` only, the same
  DDL order as 0007:
  - `group_tenants(p_group uuid) RETURNS TABLE(tenant_id uuid, sharing boolean)`: members of
    the group whose tenant is `active`.
  - `peers_of(p_tenant uuid) RETURNS TABLE(tenant_id uuid)`: active tenants other than
    `p_tenant` who are members of any group `p_tenant` is a member of.
  - `detail_grantors(p_viewer uuid, p_group uuid) RETURNS TABLE(tenant_id uuid)`: owners with a
    grant to `p_viewer` who are members of `p_group` with `sharing`, and active.
  - `due_nudges(p_now timestamptz) RETURNS TABLE(poll_id uuid, tenant_id uuid)`, `LIMIT 500`:
    open polls with `starts_at > p_now` and `created_at <= p_now - interval '3 hours'`, and
    their group's sharing, active members whose `timezone` is set, local time
    `>= '10:00' and < '20:00'`, with no answer row whose `answer` or `reason` is set, and whose
    answer row's `nudged_at` is null or older than 20 hours.
- Produces in `db/repositories/groups.py` (signatures exact):
  - `async def ensure_group(session, *, channel: str, jid: str) -> WaGroup` (system)
  - `async def mark_served(session, group_id: UUID, now: datetime) -> None` (system; sets
    `served_at` only if null)
  - `async def add_context(session, group_id: UUID, *, sender_name: str | None, body: str,
    sent_at: datetime) -> None` (system; then deletes rows older than `CONTEXT_AGE` and beyond
    the newest `CONTEXT_LIMIT`)
  - `async def recent_context(session, group_id: UUID) -> list[GroupContext]` (oldest first)
  - `async def touch_member(session, tenant_id: UUID, group_id: UUID, now: datetime) -> None`
    (tenant; upsert `seen_at`)
  - `async def set_sharing(session, tenant_id: UUID, group_id: UUID, sharing: bool) -> None`
    (tenant)
  - `async def group_tenants(session, group_id: UUID) -> list[tuple[UUID, bool]]` (system)
  - `async def peers_of(session, tenant_id: UUID) -> list[UUID]` (system)
  - `async def detail_grantors(session, viewer: UUID, group_id: UUID) -> list[UUID]` (system)
  - `async def grant(session, owner: UUID, viewer: UUID) -> bool`, `async def revoke(session,
    owner: UUID, viewer: UUID) -> bool` (tenant; `False` when there was nothing to change)
  - `async def due_nudges(session, now: datetime) -> list[tuple[UUID, UUID]]` (system)
  - `async def set_display_name(session, tenant_id: UUID, name: str) -> None` (tenant)
  - `CONTEXT_LIMIT`, `CONTEXT_AGE` live here.
- Outing and answer repositories are in Task 7 (`db/repositories/outings.py`).

- [ ] **Step 1: Write the failing tests** in `tests/db/test_groups_repository.py`:
  - `test_context_keeps_newest_twenty`: 25 rows → `recent_context` returns the last 20, oldest
    first.
  - `test_context_drops_older_than_six_hours`.
  - `test_group_tenants_lists_active_members_with_sharing`: A sharing, B not, C suspended →
    `[(A, True), (B, False)]`.
  - `test_peers_of_only_shared_groups`: A and B in G1, C in G2 → `peers_of(A) == [B]`.
  - `test_detail_grantors_needs_grant_and_sharing`: B granted A, B sharing in G → `[B]`. B
    granted A, not sharing → `[]`. C sharing, no grant → `[]`.
  - `test_due_nudges_window`: one poll, cases at 09:59 and 10:00 local, 2h59m and 3h after
    creation, answered, nudged 19h and 21h ago, event started. Expect only the eligible rows.
- [ ] **Step 2:** Add seed factories for `group_members`, `detail_grants` and `poll_answers` to
  `SEEDS` in `tests/db/test_isolation.py`. They need a `wa_groups` row and a `group_polls` row
  seeded by the owner connection first. Add the four functions to the "definer functions return
  ids only" test.
- [ ] **Step 3:** Run `pytest tests/db/test_groups_repository.py tests/db/test_isolation.py -v`.
  Expected: FAIL.
- [ ] **Step 4: Implement** the migration, models and repository. Model the functions on
  `alembic/versions/0007_reminders.py`. `due_nudges` reads `poll_answers`, `group_members`,
  `group_polls` and `tenants`, so grant `SELECT` on each to the definer role.
- [ ] **Step 5:** Run `alembic downgrade -1 && alembic upgrade head && pytest tests/db -v`.
  Expected: PASS.
- [ ] **Step 6: Commit** `feat(db): groups, members, detail grants, outings and answers`.

### Task 3: Sending to a group, and sending polls

**Files:**
- Modify: `src/personal_organizer/interfaces/channel.py` (`OutboundMessage`)
- Modify: `src/personal_organizer/providers/channel/gowa/outbound.py`
- Modify: `src/personal_organizer/providers/channel/whatsapp/outbound.py`
- Modify: `src/personal_organizer/messaging/outbox.py` (`send_once`)
- Test: `tests/unit/test_gowa_outbound.py`, `tests/db/test_outbox.py`,
  `tests/fixtures/channels.py` (`FakeOutbound` records `poll_options`)

**Interfaces:**
- Produces: `OutboundMessage.poll_options: tuple[str, ...] = ()`.
- Produces: `GowaOutbound.send_text` sends to `recipient` as-is when it ends with `@g.us`, and
  otherwise normalises the phone as today. With `poll_options` it posts
  `POLL_PATH = "send/poll"` with `{"phone": jid, "question": body, "options": [...],
  "max_answer": 1}` and returns `results.message_id`, with the same error handling as a text.
- Produces: the Meta outbound raises `RejectedChannelError(None)` for a group recipient or
  `poll_options`.
- Produces: `send_once(..., poll_options: tuple[str, ...] = ())`, passed into the
  `OutboundMessage`.
- Produces: `async def sent_message_id(db: Database, inbox_id: UUID, kind: str) -> str | None`
  in `messaging/outbox.py`: the `provider_message_id` of that send, if accepted.

- [ ] **Step 1: Write the failing tests:**
  - `test_send_to_group_jid_posts_jid_unchanged`: recipient `"120363…@g.us"` → request JSON
    `phone == "120363…@g.us"`.
  - `test_send_poll_posts_to_send_poll`: the path is `send/poll`, the JSON has `question`,
    `options` and `max_answer: 1`, and the returned id comes from `results.message_id`.
  - `test_poll_without_id_is_ambiguous`: a 200 with no id → `AmbiguousDeliveryError`.
  - In `tests/db/test_outbox.py`: `test_sent_message_id_after_accept`.
- [ ] **Step 2:** Run `pytest tests/unit/test_gowa_outbound.py tests/db/test_outbox.py -v`.
  Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces. The typing indicator is skipped for polls.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gowa): send to groups and send polls`.

### Task 4: The group gate

**Files:**
- Create: `src/personal_organizer/messaging/groups/__init__.py`
- Create: `src/personal_organizer/messaging/groups/gate.py`
- Create: `src/personal_organizer/messaging/groups/addressing.py`
- Modify: `src/personal_organizer/messaging/inbound.py` (`handle_inbound` branches on
  `row.group_id`)
- Modify: `src/personal_organizer/messaging/inbox.py`: move `_PURGED`, `_finish` and
  `_is_stale` here from `inbound.py` as `PURGED`, `finish_row` and `is_stale`, so the gate can
  use them without importing `inbound` (which imports the gate). `inbound.py` imports them back.
- Modify: `src/personal_organizer/worker/tasks/channel.py` (wires `on_group`)
- Test: `tests/unit/test_addressing.py`, `tests/db/test_group_gate.py`

**Interfaces:**
- Consumes: Task 1's `InboxRow.group_id`, `.sender_name`. Task 2's `ensure_group`,
  `mark_served`, `add_context`, `touch_member`.
- Produces in `addressing.py`:
  - `def names_remi(text: str | None, *, bot_phone: str | None) -> bool`: true when the text
    contains "Remi" or "רמי" as a word, case-insensitive, allowing one Hebrew prefix letter from
    `ו ש ה ל ב` (so "ורמי" counts), or `@<bot phone digits without +>`. Add the mention shape
    found in Task 0 if it differs.
  - `async def is_addressed(db, row: InboxRow, *, group_jid: str, bot_phone: str | None, now:
    datetime) -> bool`: `names_remi`, or `row.context_message_id` is the
    `provider_message_id` of an outbox row whose `recipient_key == f"group:{group_jid}"`, or the
    newest outbox row for that recipient key was created within `FOLLOW_UP_WINDOW` as the reply
    to an inbox row from the same `sender_key`.
- Produces in `gate.py`:
  - `GROUP_REPLY: Final = "group:reply"` (outbox kind)
  - `OnGroup = Callable[[InboxRow, OutboundChannel, GroupTurn], Awaitable[None]]`, where
    `GroupTurn` is a frozen dataclass: `group_id: UUID`, `jid: str`, `tenant_id: UUID | None`
    (the sender, if an active tenant), `addressed: bool`, `now: datetime`.
  - `async def handle_group(db, channel, row, current, *, on_group: OnGroup, bot_phone: str |
    None) -> str`. The disposition is one of `group_unserved`, `group_stale`, `group`.
- Behaviour, in order:
  1. Look up the group (`ensure_group`). Resolve the sender (`resolve_sender`). If the sender is
     an active tenant, `mark_served` and `touch_member`. The member's `sender_name` becomes
     their `display_name` if they have none yet.
  2. If the group has `served_at IS NULL` after step 1: purge with `finish_row(...,
     purge=True)` and return `group_unserved`. No context is written and `on_group` is not
     called. (Plan call: "a member is in the group" is proven by a member having written there.
     The spec's participant-list check is replaced by this, so no gateway call is needed.)
  3. Stale (`is_stale`): purge, `group_stale`.
  4. `add_context` with `sender_name`, and `body` or `[<message_type>]`. A `poll_vote` is not
     added to the context.
  5. Call `on_group` with `addressed` from `is_addressed`. `on_group` decides everything else:
     votes, classification, turns.
  6. Finish with `finish_row(..., "group", purge=True)`. Group messages are never copied to a
     person's `messages` history.
- `handle_inbound`: before the gates, `if row.group_id is not None` → `handle_group`. If
  Composio is off or `on_group` is `None`: purge with disposition `group_off` (the allowlist
  gate never sees groups).

- [ ] **Step 1: Write the failing tests.** In `tests/unit/test_addressing.py`:
  `test_names_remi` (parametrised): `"Remi, free friday?"` → True, `"remind me"` → False,
  `"רמי מה קורה"` → True, `"ורמי"` → True, `"רמיקס"` → False, `"@972552640741 hi"` → True,
  `None` → False.
  In `tests/db/test_group_gate.py`, with `Spy`-style `on_group`:
  - `test_unserved_group_is_purged_silently`: a stranger in a new group → `group_unserved`,
    `on_group` not called, no `group_context` rows, `raw is None`.
  - `test_member_message_serves_group_and_calls_turn`: an active tenant → `on_group` called
    once, `group_members` has them, and `served_at` is set.
  - `test_stranger_in_served_group_is_context_only`: context written, `on_group` called with
    `tenant_id is None`, and no `group_members` row is created for them.
  - `test_group_message_not_in_personal_history`: `messages_of(tenant)` stays empty.
  - `test_quote_reply_to_remi_is_addressed`, and
    `test_follow_up_within_five_minutes_is_addressed` / `..._after_six_minutes_is_not`.
- [ ] **Step 2:** Run `pytest tests/unit/test_addressing.py tests/db/test_group_gate.py -v`.
  Expected: FAIL.
- [ ] **Step 3: Implement** `addressing.py`, `gate.py` and the branch in `handle_inbound`
  (new keyword `on_group: OnGroup | None = None`, `bot_phone` from
  `settings.onboarding.bot_phone`). The worker passes `on_group=None` until Task 5.
- [ ] **Step 4:** Run the two files plus `tests/db/test_handle_inbound.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(groups): the group gate and who Remi is talking to`.

### Task 4b (only if Task 0 found LID-only group senders): link a member's LID

- [ ] Add `async def participants(self, group_jid: str) -> list[tuple[str | None, str | None]]`
  (phone, lid) to `GowaOutbound`, using the gateway's group-info endpoint found in Task 0. In
  `handle_group`, when `resolve_sender` misses on a LID-only sender, fetch the participants
  once per job. Then link the LID to the tenant with that phone as a second identity
  (`messaging/tenancy.py:_link`). Test: `test_lid_sender_linked_from_participants` with a fake
  outbound.

### Task 5: The group turn — who's free, okaying the group, names, private details

**Files:**
- Create: `src/personal_organizer/messaging/groups/turn.py`
- Create: `src/personal_organizer/messaging/groups/prompt.py`
- Modify: `src/personal_organizer/messaging/onboarding_text.py` (new keys below)
- Modify: `src/personal_organizer/worker/tasks/channel.py` (`on_group=partial(group_turn, …)`)
- Test: `tests/unit/test_group_tools.py`, `tests/db/test_group_turn.py`

**Interfaces:**
- Consumes: Task 2's `group_tenants`, `detail_grantors`, `set_sharing`, `set_display_name`,
  `recent_context`. Task 4's `GroupTurn`, `GROUP_REPLY`. `agent.read_calendar`'s window rules
  (`_local`, `MAX_WINDOW`), `converse`, `send_once`, `proactive_target`.
- Produces:
  - `GROUP_TOOLS` with these tool names: `who_is_free(start, end)`,
    `share_my_calendar(on: bool)`, `call_me(name)`. Task 7 adds `set_up_outing`.
  - `def busy_blocks(events: Sequence[CalendarEvent], zone: ZoneInfo) -> list[dict[str,
    object]]`: `{"start", "end", "all_day"}` only, merged where they overlap or touch.
  - `async def free_busy(db, group_id: UUID, start: datetime, end: datetime, *, calendar) ->
    dict[str, object]`. Returns
    `{"people": [{"name": str, "busy": [...]}], "not_shared": [names of members who aren't
    sharing]}`. A sharer whose calendar is not connected appears as
    `{"name", "busy": null, "note": "calendar not connected"}`.
  - `async def group_turn(row: InboxRow, channel: OutboundChannel, turn: GroupTurn, *, db,
    llm, calendar, last_attempt: bool) -> None`.
  - `DETAILS_KIND: Final = "group:details"`.
- Behaviour:
  - Not addressed: return (Task 7 adds outing answers here).
  - Addressed: the system prompt is `prompt.group_prompt(language, now, asker_name,
    asker_is_member)`. The language comes from `detect_language(row.body)`. The zone is the
    asker's if they're a tenant, else the group's first sharer's, else `Asia/Jerusalem`. The
    history is `recent_context` rendered as `"{sender_name}: {body}"` user messages, with
    Remi's own lines (stored under `sender_name="Remi"` when sent) as assistant messages. The
    model may reply `""` to stay silent (the follow-up rule). Send with `send_once(kind=
    GROUP_REPLY, recipient_key=f"group:{jid}", to=jid)`, then `add_context(sender_name="Remi")`.
  - `share_my_calendar` and `call_me` need `turn.tenant_id`. Otherwise they return
    `"error: only people set up with Remi can do that"`.
  - After a turn in which `who_is_free` ran and the asker is a tenant: `detail_grantors(asker,
    group)` is read at that moment. For each grantor, their events in the same windows are read
    in their own session and sent to the asker privately (one `send_once`,
    `idempotency_key=f"details:{row.id}"`, `kind=DETAILS_KIND`). Text is `t("group_details",
    …)`, followed by `"{name}: {title}, {day} {start}–{end}"` lines built by code. Nothing is
    sent when there are no grantors or no events.
  - A fallback on the last attempt sends `t("agent_unavailable")` in the group, as the agent
    does.
- New `onboarding_text.py` keys (both languages): `group_details` ("Here's what's on, just for
  you:"), `group_shared_on`, `group_shared_off`, `outing_kept`, `outing_nudge_fallback`
  (`{summary}`). Write the Hebrew as Hebrew.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/test_group_tools.py`:
  - `test_busy_blocks_have_no_title_or_location`: events with title "Dentist" and a location →
    the JSON dump contains neither.
  - `test_busy_blocks_merge_overlaps`.
  In `tests/db/test_group_turn.py`, with `ScriptedLLM`, the in-memory calendar and
  `FakeOutbound(name="gowa")`:
  - `test_group_turn_never_sees_titles`: two sharers with titled events. Every message the
    `ScriptedLLM` received is free of both titles. The group send is too.
  - `test_not_shared_member_is_not_read`: a member with `sharing=False` → their calendar is
    never called, and they're listed in `not_shared`.
  - `test_share_my_calendar_sets_sharing`, `test_stranger_cannot_share`.
  - `test_details_sent_privately_to_grantee`: B grants A, B shares in G, A asks → one group
    send without titles, and one private send to A containing B's title.
  - `test_details_follow_current_grants`: the grant is revoked before the turn → no private
    send.
  - `test_unaddressed_message_calls_no_model`: `NoLLM` and `addressed=False` → no sends.
- [ ] **Step 2:** Run `pytest tests/unit/test_group_tools.py tests/db/test_group_turn.py -v`.
  Expected: FAIL.
- [ ] **Step 3: Implement** `turn.py` and `prompt.py`. The prompt follows the product voice. It
  speaks to a family, never names event contents (it has none), says plainly who it can't see,
  and invites people to okay sharing only when nobody in the group shares.
- [ ] **Step 4:** Run the two files and `tests/unit/test_onboarding_text.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(groups): who's free, okaying a group, details in private`.

### Task 6: Granting details from a private chat

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py` (tools, `run_tool`, prompt line)
- Create: `src/personal_organizer/messaging/groups/grants.py`
- Test: `tests/unit/test_agent_tools.py`, `tests/db/test_grants.py`

**Interfaces:**
- Consumes: Task 2's `peers_of`, `grant`, `revoke`.
- Produces: agent tools `let_see_details(name)` and `stop_details(name)`. Both live in
  `grants.py`: `async def resolve_peer(db, owner: UUID, name: str) -> UUID | list[str]`, which
  matches `name` against peers' `display_name`s, case-insensitively, by whole name or first
  word. It returns the tenant id, or the list of peer names when there is none or more than
  one match. The tool result for a list is
  `"error: who do they mean? people you share a group with: …"`, so the model asks once. A
  match returns `"ok: {name} can now see your event details"`, and the model says who it
  understood.
- Prompt line (in `_PROMPT`): the person can let someone they share a group with see what
  their events are. Use their notes to work out who "Mom" is. Details are only ever sent to
  that person privately.

- [ ] **Step 1: Write the failing tests:** `test_resolve_peer_by_first_name`,
  `test_resolve_peer_ambiguous_lists_names`, `test_resolve_peer_never_outside_shared_groups`
  (C, who isn't in a shared group, is never matched even by exact name),
  `test_let_see_details_then_stop`.
- [ ] **Step 2:** Run `pytest tests/db/test_grants.py tests/unit/test_agent_tools.py -v`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** `respond` gets `grants: Grants | None = None`, a small port like
  `Notes` (`TenantGrants(db, tenant_id)` in the worker, `None` in `po-chat`).
- [ ] **Step 4:** Run the same. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): let someone see your event details`.

### Task 7: Outings — the poll, votes, and answers read from the chat

**Files:**
- Create: `src/personal_organizer/db/repositories/outings.py`
- Create: `src/personal_organizer/messaging/groups/outings.py`
- Modify: `src/personal_organizer/messaging/groups/turn.py` (`set_up_outing` tool; votes and
  unaddressed answers before the turn)
- Test: `tests/db/test_outings.py`, `tests/unit/test_outing_classifier.py`

**Interfaces:**
- Consumes: Task 3's `send_once(poll_options=…)`, `sent_message_id`. Task 2's tables.
  `agent.check_event`, `agent.add_event`, `agent.event_summary`.
- Produces in `db/repositories/outings.py`: `create_poll(session, *, group_id, inbox_id, event:
  CalendarEvent, language, in_option, out_option) -> GroupPoll`, `set_poll_message(session,
  poll_id, provider_message_id)`, `poll_by_message(session, group_id, provider_message_id) ->
  GroupPoll | None`, `open_polls(session, group_id, now) -> list[GroupPoll]` (system), and
  tenant-side `answer_of(session, tenant_id, poll_id) -> PollAnswer | None`,
  `set_answer(session, tenant_id, poll_id, *, answer: str | None, reason: str | None)`,
  `claim_event(session, tenant_id, poll_id, now) -> bool` (sets `claimed_at` only if null, in
  its own committed transaction before the create), `mark_nudged(session, tenant_id, poll_id,
  now)`.
- Produces in `messaging/groups/outings.py`:
  - `OUTING_OPTIONS: Final = {"en": ("I'm in", "Can't make it"), "he": ("אני בפנים", "לא מסתדר לי")}`
  - `POLL_KIND: Final = "group:poll"`, `VOTE_KIND: Final = "group:vote"`
  - `async def set_up_outing(arguments, *, db, channel, row, turn, zone, language) -> str`. The
    group tool: `check_event`, then one poll per turn, sent with `send_once(kind=POLL_KIND,
    poll_options=OUTING_OPTIONS[language])`, whose question is
    `event_summary(..)` as one line. Store the poll and its `provider_message_id`. The tool
    returns `"ok: the poll is posted"`.
  - `async def handle_vote(row, channel, turn, *, db, calendar) -> None`. The voter must be a
    tenant (otherwise ignore). The poll comes from `poll_by_message(row.reply_id)`.
    - In: `set_answer("in")`. If `claim_event`: `add_event` in the voter's calendar (its
      returned line is the private message), sent with `send_once(kind=VOTE_KIND,
      inbox_id=row.id)` to the voter's phone.
    - Out: `set_answer("out")`. If `claimed_at` is set, send `t("outing_kept")` privately.
    - Cleared: `set_answer(None)`.
  - `async def read_answer(row, turn, *, db, llm) -> None`. For an unaddressed text from a
    tenant with open polls in this group and no answer: one call to `classify(polls, text,
    llm)` returns `("in", None)`, `("out", reason)` or `None`. Store it, then nothing else: no
    send, no event. An "in" here does not write to the calendar, since only a vote confirms.
  - `async def classify(polls: Sequence[GroupPoll], text: str, *, llm) -> tuple[UUID, str,
    str | None] | None`. Shaped like `confirm.decide`: a short system prompt listing the open
    outings by number, and an answer of `IN n`, `OUT n: <reason>` or `OTHER`.
- **If Task 0 found votes without `selected_options`:** skip `handle_vote` and the poll send.
  `set_up_outing` posts the plan as a plain group message (`send_once(kind=POLL_KIND)`), and
  `read_answer` also runs for addressed messages and quote-replies to it. In that path an "in"
  from `classify` does add the event, through the same `claim_event`.
- `group_turn` order: `poll_vote` → `handle_vote`, return. Not addressed → `read_answer`,
  return. Addressed → the turn, with `set_up_outing` in `GROUP_TOOLS`.

- [ ] **Step 1: Write the failing tests** in `tests/db/test_outings.py`:
  - `test_set_up_outing_posts_one_poll`: the outbound got `poll_options == ("I'm in", "Can't
    make it")`, and the `group_polls` row has `provider_message_id`.
  - `test_vote_in_creates_event_in_voters_calendar` (in-memory calendar of the voter only).
  - `test_vote_in_twice_creates_one_event`.
  - `test_vote_back_in_reuses_event`: in → out (`outing_kept` sent) → in, one event.
  - `test_vote_from_stranger_is_ignored`.
  - `test_unaddressed_reason_stops_follow_ups`: a `ScriptedLLM` answering `OUT 1: working` →
    `poll_answers.reason == "working"`, and `due_nudges` no longer lists them.
  - `test_unaddressed_from_answered_member_calls_no_model` (`NoLLM`).
  In `tests/unit/test_outing_classifier.py`: parsing `IN 1`, `OUT 2: at work`, `OTHER`,
  garbage → `None`, and `n` out of range → `None`.
- [ ] **Step 2:** Run `pytest tests/db/test_outings.py tests/unit/test_outing_classifier.py -v`.
  Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same, plus `tests/db/test_group_turn.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(groups): outings by poll, votes add the event`.

### Task 8: Follow-ups

**Files:**
- Create: `src/personal_organizer/messaging/groups/nudge.py`
- Modify: `src/personal_organizer/worker/tasks/schedule.py` (`NUDGE_TASK`, `run_tick` scans
  `due_nudges`)
- Modify: `src/personal_organizer/messaging/agent.py` (open outings in the prompt; tool
  `answer_outing`)
- Test: `tests/db/test_nudge.py`, `tests/db/test_schedule_tick.py`

**Interfaces:**
- Consumes: Task 2's `due_nudges`, Task 7's repositories and `classify`'s answer values.
- Produces:
  - `NUDGE_TASK: Final = "schedule:nudge"`, `def nudge_key(poll_id: UUID, tenant_id: UUID, day:
    date) -> str` = `f"nudge:{poll_id}:{tenant_id}:{day}"`.
  - `async def send_nudge(poll_id, tenant_id, *, db, channels, llm, now, last_attempt) -> str |
    None`: reload the poll and answer, and return `None` if the poll is closed, started, or
    answered. Write one warm line with the model: a short prompt with the outing's summary,
    the person's name, how many nudges came before (`nudged_at` set or not), their language,
    and the product voice rules. If the model is unavailable on the last attempt, use
    `t("outing_nudge_fallback", summary=…)`. Send it with `send_once(idempotency_key=
    nudge_key(...))`, then call `mark_nudged` and `record_outbound` in the tenant's session, so
    the private reply has the context.
  - Agent: `respond` gets `outings: Outings | None` (a port like `Notes`). The prompt lists
    their open, unanswered outings, numbered. Tool `answer_outing(n, coming: bool, reason)`
    stores the answer. `coming=True` does not add the event, and the prompt says the vote in
    the group adds it.
- `run_tick` defers nudges after briefings, with the same `_defer` and lock pattern.

- [ ] **Step 1: Write the failing tests:** `test_nudge_sent_once_per_day`,
  `test_nudge_skipped_after_answer`, `test_nudge_skipped_after_event_start`,
  `test_nudge_fallback_on_last_attempt`, `test_tick_defers_due_nudges`,
  `test_answer_outing_privately_stops_nudges`.
- [ ] **Step 2:** Run `pytest tests/db/test_nudge.py tests/db/test_schedule_tick.py -v`.
  Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same. Expected: PASS.
- [ ] **Step 5: Commit** `feat(groups): warm daily follow-ups until they answer`.

### Task 9: Evals, runbook, staging

**Files:**
- Modify: `tests/evals/test_agent_evals.py` (or create `tests/evals/test_group_evals.py`)
- Modify: `docs/runbook-whatsapp-gateway.md` (gateway variable, group test steps)
- Modify: `po-chat` only if `admin/chat.py` needs `grants=None`/`outings=None` to keep working

- [ ] **Step 1:** Live evals with Haiku 5.5, in Hebrew and English, from the Done-When:
  - "who's free Friday evening" → names and times, no titles.
  - "is Daniel at the dentist Friday?" → no confirmation or denial of the title.
  - "let's do dinner at 7, set it up" as a follow-up → `set_up_outing` called.
  - An unaddressed chat message → silence (`""`) when reached through the follow-up rule.
  - "can't make it, working" → `OUT n: …` from `classify`.
  - "Mom can see my events", with the note "Mother: Rina" → `let_see_details("Rina")`.
  The user runs them (the worktree guard blocks commands containing "eval").
- [ ] **Step 2:** Full suite: `pytest && ruff check src tests && mypy src tests`. Expected:
  everything passes.
- [ ] **Step 3:** Runbook: the `WHATSAPP_WEBHOOK_IGNORE_JIDS` change, `alembic upgrade head`
  (0009), and the Done-When as a staging script with the family group.
- [ ] **Step 4: Commit** `docs(groups): runbook and evals`. Open the PR(s): Tasks 1–6 are PR A
  (groups, free/busy, details) and Tasks 7–9 are PR B (outings, follow-ups), stacked.
