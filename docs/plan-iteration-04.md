# Iteration 04 plan — the agent: one LLM, onboarding through it, and Google Calendar through Composio

**Source:** the 2026-10-07 review's "Agent core-lite" (one model, read/propose tools, `po-chat`,
Hebrew and English scenarios, calendar read with time zones) plus its "confirm → execute", cut
to calendar only. Added on 2026-10-12: onboarding is talked through by the LLM too, so a person
who goes off the default path still gets replies that make sense. Reminders, briefings, voice
and memory stay in later iterations.

**Size:** three PRs, each usable on its own.

1. The LLM, and onboarding through it.
2. Active tenants get answers and calendar reads.
3. Events, behind a confirm.

## Objective and Done-When

> An invited person can say anything during onboarding — "what is this?", "is it safe?", "I
> use Outlook", "speak English" — and get a sensible answer that still leads them to the time
> zone and the connect link. Once onboarded, they write "מה יש לי מחר?" or "add dinner with Noa
> Thursday 8pm". The bot answers from their own Google Calendar, in their language and time zone,
> and adds the event only after they reply 1.

| Done-When | Where it is proved |
|---|---|
| An off-script onboarding message gets a model-written answer, and the zone choice or the connect link is still under it | `tests/db/test_onboarding.py` with a fake `LLMProvider` (PR 1). Staging: a new invitee asks "what is this?" first |
| The default path still needs no LLM call: "1", a button or an exact city | `tests/db/test_onboarding.py`: the fake LLM fails the test if it is called on those paths |
| With the model off or down, onboarding is exactly today's | the existing onboarding tests, unchanged, with `llm=None`, plus a last-attempt test |
| An active tenant gets an LLM answer, not the fixed ack | `tests/db/test_agent_turn.py` with a fake `LLMProvider` (PR 2). Staging: phone → GOWA → reply |
| "What's on tomorrow" lists the right day's events in the tenant's zone | `tests/unit/test_agent_tools.py` (zone maths, fake calendar). Staging, against a real calendar |
| A proposed event is created once on "1", never on retry, never without it | `tests/db/test_pending_action.py` (PR 3) |
| Hebrew and English scenarios pass on the chosen model | `pytest -m eval` (real model, fake calendar), run before merging each PR and on every model change |
| A tenant's calls can only reach that tenant's Google account | `tests/unit/test_composio_calendar.py`: every `tools.execute` carries the bound `connected_account_id` and the tenant's `user_id`, and the model's arguments can't override either |

## Where we start

The pieces are already in place. Nothing calls them yet:

- `interfaces/llm.py`: `LLMProvider.complete(messages, tools=)` and `Message`/`Completion`.
  `providers/llm/` is empty. `anthropic`, `openai`, `litellm` and `langfuse` are installed;
  `init_langfuse`/`get_langfuse` exist (EU host).
- **Onboarding (`messaging/onboarding.py`) is a fixed state machine:**
  - The language is detected from the first message, at `enrol`.
  - The `zone` step:
    - The first message gets the welcome, plus a Correct/Change choice built from the number's
      guess (or a city prompt when there's no guess).
    - "1", a button or an alias resolves the choice.
    - An exact city resolves through `match_zone`. Anything else gets the fixed "didn't
      recognise that" text.
  - The `connect` step answers *every* message by resending the link.
  - Each step sends through `send_once` and returns an `Advance(timezone, next_step)`.
  - Connecting depends on the tenant's status, not its step. So a link that was sent keeps
    working even if a step change is lost.
- `interfaces/calendar.py`: `CalendarProvider.list_events/create_event`. It has no
  implementation. `ComposioConnector` (`providers/calendar/composio.py`) does the connect flow
  only. Its `_call` already has the deadline, the shared thread limiter, telemetry off and
  error translation without chaining.
- `calendar_connections` (RLS) holds `composio_user_id` (the tenant uuid) and
  `connected_account_id`. Read it with `active_connection(session, tenant_id)`. The Google app
  has the full `auth/calendar` scope, so writes need no re-consent.
- `messaging/inbound.py`:
  - The tenant gate calls `onboarding_step(...)` for an `onboarding` tenant, and
    `on_allowed(row, channel)` for an `active` one.
  - Both are followed by `_finish_known`, which records the inbound message under RLS in the
    row's finishing transaction (D7).
  - Today `on_allowed` is `acknowledge`, the fixed ack. Outbound texts aren't recorded in
    `messages`, and the outbox doesn't store them.
- `send_once` (at most once per `inbox_id` + `kind`), `split_text`, `choices.py` (numbered
  replies on every channel), `tenants.timezone` and `tenants.language`.
- `ModelSettings`: `MODELS__CHAT_MODEL=gpt-5-mini`, `MODELS__FALLBACK_MODEL=claude-haiku-4-5`
  (still Active per Anthropic's model list on 2026-10-06; v7's Oct 15 was a no-earlier-than
  date), `request_timeout_s`, `max_retries`.
- Composio SDK 0.24.0: `composio.tools.execute(slug, arguments, *, connected_account_id,
  user_id, version)`, plus `toolkit_versions` on the client to pin the Google Calendar toolkit.

## Decisions

**D1. One provider, called through its own SDK: Claude Haiku 5.5 (`claude-haiku-5-5`) via
`anthropic`.** Decided 2026-10-12.
- `MODELS__CHAT_MODEL=claude-haiku-5-5`. It costs $0.10 / $0.50 per MTok for prompts of 100K
  tokens or less (ours are about 6K), so roughly $15 a month even with 50 people writing 10
  messages a day.
- **Step-up rule:** if the evals fail on Haiku after prompt fixes, run them on
  `claude-sonnet-5-5`. Switch `MODELS__CHAT_MODEL` only if Sonnet passes where Haiku didn't.
  It's a config change, not code.
- Haiku 5.5 details the adapter must handle:
  - Thinking is adaptive and on by default. Leave `thinking` out and set
    `output_config.effort="low"` for chat (the default is `medium`); raise it only if the evals
    need it.
  - Sampling parameters (`temperature`) and assistant prefill return a 400. Don't send them.
  - `stop_reason == "refusal"` has no server-side fallback on Haiku. Treat it as
    `LLMRejectedError`, so the turn takes the fixed fallback text of D3 and doesn't retry.
  - The tokenizer counts about 30% more tokens than Haiku 4.5's. That's irrelevant at our size.
- The fallback model is not wired: we add a fallback once an outage actually bites.
  `MODELS__FALLBACK_MODEL` stays in settings, unused.
- LiteLLM goes from `pyproject.toml`, because one provider doesn't need a router.
- `openai` stays installed: embeddings and transcription will use it in later iterations.

**D2. The model sees our tools, not Composio's.**
- We write the tool schemas ourselves: `set_timezone` and `set_language` for onboarding,
  `list_events(start, end)` and `propose_event(title, start, end, location?)` for the calendar.
- The calendar adapter calls `tools.execute` with the slug, the `connected_account_id` from the
  tenant's active `calendar_connections` row, `user_id=str(tenant_id)` and a pinned toolkit
  version.
- There is no tool router session, no `tools.get()` provider schemas and no workbench or meta
  tools.

So the model can't choose an account, a user or a toolkit, and the tenant binding comes from an
RLS row, never from model output. The schemas also stay small, and the core stays
provider-neutral. This also closes the Iteration 03 follow-up "name the bound
`connected_account_id`". Orphaned Composio accounts then can't be reached by any call, so
disabling them can wait.

This replaces the 2026-10-07 research's recommendation of Composio *session mode*
(`composio.create(user_id=…, toolkits=["googlecalendar"], manage_connections=False)` with
`session.tools()`):
- That mode hands the model meta tools (`COMPOSIO_SEARCH_TOOLS`,
  `COMPOSIO_MULTI_EXECUTE_TOOL`, `COMPOSIO_REMOTE_WORKBENCH`). So it needs a lockdown we'd have
  to verify, and the Python spelling for turning the workbench off was never confirmed.
- It can't name a `connected_account_id`, and it costs more tokens.
- We need two actions, not tool discovery.

If the agent ever needs open-ended Google access, revisit session mode then.

**D3. Every LLM turn runs inside the existing `channel:handle_inbound` job.** That covers
onboarding and active tenants.
- It needs no new task or queue, and the sender lock already orders a person's messages.
- A transient LLM or Composio error (`LLMUnavailableError`, `CalendarProviderUnavailableError`)
  joins `INBOUND_RETRY`. A re-run is safe because `send_once` keeps it to one reply.
- On the last attempt (`pass_context=True`, `attempts >= max_attempts`: Procrastinate 3.10 runs
  a job `max_attempts + 1` times, 7 with our 6) the turn falls back instead of raising, so the
  person never gets silence.
  - During onboarding it falls back to today's fixed texts.
  - For an active tenant it sends a fixed "I couldn't answer just now, try again".

**D4. History is text, from `messages`.**
- The prompt carries the last 20 `messages` rows (in and out) plus the current body.
- Every reply, onboarding included, is recorded as `direction="out"` in `_finish_known`, in the
  same transaction as the inbound copy, so D7 still holds. To carry it, `Advance` gains
  `reply=`.
- Both rows share `now()`, so history orders by `(created_at, direction = 'out')`.
- Because onboarding replies are recorded, the agent later sees how onboarding went.

*shortcut: tool calls and results aren't stored, so the model re-reads the calendar each turn.
Store them if the evals show it misses context.*

**D5. Writes need a confirm (PR 3).**
- `propose_event` writes nothing to Google. It stores one `pending_action` (a jsonb column on
  `tenants`: id, kind, payload, expires_at, one at a time).
- The reply ends with a `Choice` ("1 add / 2 cancel"). When the next message resolves to that
  choice, it is handled without the LLM.
- A claim (`UPDATE tenants SET pending_action = NULL WHERE id = :t AND pending_action->>'id' =
  :id RETURNING …`) commits first, then `create_event` runs. So a retry or a double "1" can't
  create the event twice; this is at most once, as in ADR 0003.
- The claim leaves `{claimed_by: <inbox id>}` in place of the action (PR 3). A re-run of that
  message then sends nothing, rather than asking the model what a bare "1" means, which could
  propose the same event again.
- Any other message drops the proposal, and the model sees it in history if the person is
  amending it. A proposal expires after an hour.

**D6. Time and language come from the tenant row.**
- The system prompt gives the current local date and time in `tenants.timezone`, the weekday, a
  Sunday-to-Thursday work week and `tenants.language`, and asks for a reply in the person's
  language.
- Tool arguments are naive local times; the adapter attaches the zone and sends RFC 3339.
- Events come back converted to the zone, with **title, start, end and location only**.
  Descriptions and attendee lists never reach the prompt, because anyone can send an invite
  into your calendar (prompt injection).
- `TenantState` gains `timezone`; the column already exists.

**D7. Bounds per turn:**
- At most 4 tool rounds, `max_tokens` 1024, and a 20 s `AGENT__REQUEST_TIMEOUT_S` per call with
  the SDK's own retries off, so `INBOUND_RETRY` is the one retry layer. A reply cut off at
  `max_tokens` takes the fallback.
- A long reply goes through `split_text`.
- There is no per-tenant daily cap for a circle; a Sentry alert and a monthly bill check cover
  it.

**D8. Observability:**
- One Langfuse trace per turn (model, tokens, tool names, latency). It holds message content,
  so it's EU-hosted and counts as personal data.
- The LLM SDK's errors are translated without chaining, as `ComposioConnector` already does, so
  prompts never reach Sentry.
- Logs carry `inbox_id`, tool names and token counts, never bodies.

**D9. No calendar, no guessing.** If there's no active connection, or Composio reports the
account `EXPIRED`/`REVOKED`/401:
- The tool returns `calendar_not_connected`.
- The reply says so, and a fresh connect link is appended by code, the same way as in D10.
- The model is told not to invent events.

**D10. Onboarding is a conversation, but the code keeps the rails.** The model writes the
words; the code still owns every state change and everything that must be exact.

*The default path costs no LLM call*, and it is exactly today's code:
- "1", a button reply or an alias for the open zone choice.
- An exact city or zone name (`match_zone`).
- The Change → "which city?" prompt.
- The confirmed zone → `connect_text` with the link.
- The "you're all set" message from the connect callback.

*The model writes the reply* in three places, which are exactly the branches that send a fixed,
context-free text today:
- The first message: the welcome. It answers whatever they opened with ("can you add my dentist
  tomorrow?" → "I can, once we're set up — two quick steps").
- An unmatched message in the `zone` step, replacing "didn't recognise that".
- Any message in the `connect` step, replacing the bare link resend.

What the model gets:
- The step, what's done and what's missing.
- The zone guess.
- The recorded conversation (D4).
- A short facts sheet:
  - what the bot does today;
  - Google Calendar only;
  - why it needs calendar access;
  - the "Google hasn't verified this app" screen, and why it's safe to continue;
  - invite-only;
  - where the data lives;
  - how to stop: `po-admin suspend`, via you.

The model's onboarding tools are `set_timezone(zone)` and `set_language("he" | "en")`. There are
no calendar tools, because there is no calendar yet.
- `set_timezone` is validated against `zoneinfo`. On success it takes the existing confirm
  path: `Advance(timezone=…, next_step="connect")`, and `connect_text` with the link. For
  example, "I live in Tel Aviv but I'm in New York this month" → the model picks one and says
  which.
- `set_language` sets `Advance(language=…)`, which needs a new `set_language` repository
  function. Detection at `enrol` stays the default.

*The code appends the suffix* under the model's words:
- In the `zone` step, the zone choice (or the city prompt when there's no guess).
- In the `connect` step, the link from `issue_or_reuse_link`.

The model never writes a URL or a numbered option. Its prompt says the suffix is added, and a
check drops any reply text that contains `http` or the `[link]` placeholder recorded in
history. So the link and the choice are always present,
correct and the same on every channel, and "1" keeps meaning Correct (D9 of Iteration 03).

*Re-runs.* The three branches send under one kind, `onboarding:reply`, with the model or
without it (so toggling the agent between a send and its commit can't send twice). A re-run
that finds that reply already sent returns no change without calling the model, because what
it decided now need not be what went out.

*shortcut: a crash between the send and the commit loses what the model decided with that reply
(a `set_timezone`, say), and the sent text isn't recorded in the history. The next message asks
again, and a link already sent keeps working, because connecting depends on status, not step.
Store the decision with the send if that ever shows up in the logs.*

*Off the map entirely* (Outlook, no Google account, "I don't want to connect"): the model says
honestly that Google Calendar is the only option for now, and the person stays in `connect`.
There is no calendar-less mode (see open questions).

## PR 1 — the LLM, and onboarding through it

1. **Model check.** Confirm `claude-haiku-5-5` answers with tools on the staging key (one
   `po-chat` turn).
2. **`interfaces/llm.py`.** Add `ToolCall(id, name, arguments)`, then
   `Message.tool_calls: tuple[ToolCall, ...] = ()` and `Message.tool_call_id: str | None`, and
   `Completion.tool_calls`. Tools are neutral `{"name", "description", "input_schema"}` dicts.
   Drop `embed`: nothing calls it, and the memory iteration brings its own.
3. **`providers/llm/anthropic.py`.** `AnthropicLLM.from_settings` maps neutral messages and
   tools onto the Messages API (system prompt cached), maps errors onto
   `LLMUnavailableError`/`LLMRejectedError` in `core/errors.py`, and has a Langfuse span.
   Test with a stubbed HTTP transport, the way `test_composio_connector.py` does.
4. **`messaging/conversation.py`.** `converse(history, text, *, system, tools, run_tool, llm)
   -> str` is the shared tool loop (D7). Onboarding uses it now and the agent uses it in PR 2.
5. **`messaging/onboarding.py`.**
   - `onboarding_step` gains `llm: LLMProvider | None` and `history`. The three branches in D10
     call `converse` with the onboarding prompt, the facts sheet and the two tools, then append
     the code suffix.
   - `llm=None`, or the last attempt, keeps today's texts.
   - `Advance` gains `language` and `reply`. The prompt and facts sheet live beside
     `onboarding_text.py`, in both languages.
6. **`messaging/inbound.py`.**
   - `_finish_known` writes `Advance.language` and records `Advance.reply` through a new
     `record_outbound`.
   - The tenant gate loads the history once and passes it on.
   - `on_allowed` is untouched until PR 2.
7. **Settings and wiring.**
   - Add `AgentSettings(enabled=False, api_key)` (`AGENT__*`). When it's on, `Settings` requires
     `composio.enabled`, and names missing keys the same way it does for Composio.
   - `worker/tasks/channel.py` builds the LLM when the agent is enabled and passes it through
     `handle_inbound`.
8. **`po-chat --tenant <uuid>`.** A terminal loop over the same functions, with history in
   memory and a print-only channel. It starts the tenant at its stored step. It goes in
   `[project.scripts]`.
9. **Evals: `tests/evals/`, `-m eval`, excluded from the default run.** About 10 onboarding
   scenarios, half in Hebrew. Each asserts the tool call and its argument, the reply's language,
   and that the text has no URL:
   - "what is this?" as the first message;
   - "is this safe?" on the connect step;
   - "אני בתל אביב אבל בניו יורק החודש";
   - "speak English please";
   - "I use Outlook";
   - "the link doesn't work";
   - a calendar request made before connecting;
   - gibberish.

## PR 2 — active tenants: answers and calendar reads

1. **Spike (half a day, staging account).** The slugs `GOOGLECALENDAR_EVENTS_LIST` and
   `GOOGLECALENDAR_CREATE_EVENT` are listed in Composio's toolkit docs (checked 2026-10-10).
   Confirm their argument names, that `tools.execute` honours `connected_account_id`, the
   current toolkit version to pin, and the response shapes. Save one redacted response per
   slug as a test fixture.
2. **`providers/calendar/composio.py`.** `list_events` (and in PR 3 `create_event`) go on
   `ComposioConnector`, reusing `_call`. `CalendarProvider` takes
   `(account_id, user_id, *, start, end)`, because the caller already holds the connection row.
   Pin the toolkit with `toolkit_versions` in `from_settings`.
3. **`messaging/agent.py`.**
   - `respond(history, text, tenant, *, llm, calendar, now) -> str` is the active prompt plus
     `converse` with `list_events`, and the zone maths (D6).
   - `agent_turn(row, channel, tenant, *, db, …) -> Advance` is the `on_allowed` wrapper:
     connection lookup, `respond`, `send_once(kind=AGENT_REPLY)`, D9's link suffix.
4. **`messaging/inbound.py`.** `OnAllowed` gets the `TenantState` and returns `Advance | None`.
   `acknowledge` keeps the allowlist gate, where Composio is off, unchanged.
5. **Wiring.** `worker/tasks/channel.py` picks `agent_turn` when the agent is enabled.
   `po-chat` gains active tenants, with `--fake-calendar`.
6. **Evals.** About 12 calendar scenarios, half in Hebrew ("מחר בשש", "ביום חמישי הבא",
   "השבוע"), each asserting the tool call and its date range, not the wording. Also: an event
   title that carries an instruction, no connection (D9), and a question that needs no tool.

## PR 3 — events, behind a confirm

1. Migration: `tenants.pending_action jsonb NULL`. It isn't a new table, and RLS already
   covers it.
2. The `propose_event` tool, the `Choice` on the reply, and the claim-then-create path (D5) in
   `agent_turn`, ahead of the LLM.
3. `create_event` on the adapter. The created event's link goes in the confirmation.
4. Tests: confirm creates once, a retry after the claim creates nothing, expiry, another
   message drops the proposal, "2" cancels. Add three eval scenarios.

## Prerequisites (outside the code)

- An Anthropic API key on the staging **worker** as `AGENT__API_KEY`. The
  api doesn't need it. Set a monthly spend limit in the provider console.
- Staging has `COMPOSIO__ENABLED=true` and your own calendar connected. That's done if the
  Iteration 03 runbook's day-1 check passed.
- To try PR 1 end to end, have a second number ready to invite, or reset your own tenant to
  `onboarding`.
- Set `MODELS__CHAT_MODEL=claude-haiku-5-5` on the staging worker (and in `.env.example`
  and the runbook, in PR 1).

## Out of scope

- Memory, reminders and briefings (`scheduled_jobs`), voice notes, groups, Google Tasks, and
  photo → event.
- A fallback provider, cost dashboards, and Composio triggers (calendar-change webhooks).
- Editing or deleting events, and disabling orphaned Composio accounts (harmless after D2).
- A calendar-less mode for people who can't connect Google.
- The groups plan (`do-3-but-with-snoopy-lagoon`) later adds `calendar_id` and `free_busy()`
  (`GOOGLECALENDAR_FREE_BUSY_QUERY`) to `CalendarProvider`. Those arrive with groups, not here.

## Risks and open questions

- **Prompt injection through the calendar.** D6 leaves out descriptions, and D5 means nothing
  is written without the person's "1". The worst case left is a misleading summary.
- **The model talking past the rails in onboarding.** The suffix is code, the state changes are
  code, and replies with URLs are dropped. The worst case left is an unhelpful sentence above
  a correct choice or link. The evals cover the off-script cases.
- **The first reply is slower.** The welcome now waits on a model call, a few seconds instead
  of instant. That's acceptable for a one-time message, and the fallback covers an outage.
- **Hebrew relative dates and the Sunday-start week.** That is why the evals assert date
  ranges. If the chosen model fails them, try the next model up before adding parsing code.
- **Composio's response shape or slug drift.** The toolkit version is pinned, and the fixtures
  from the spike catch a change at upgrade time.
- **Personal data at the LLM provider and Langfuse.** Onboarding messages now go to the
  provider as well. That's acceptable for a circle, but say it in the invite message. Check the
  provider's retention and training defaults for API traffic.
- **Turn latency inside the sender lock.** A 10–20 s turn holds the person's next message. That
  keeps the order right; watch it in Langfuse.
- **Open:** should someone who can't connect Google get a calendar-less mode (chat and,
  later, reminders) instead of staying in `connect`? That isn't planned. Decide after the first
  invitees.
