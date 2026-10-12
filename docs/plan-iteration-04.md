# Iteration 04 plan — the agent: one LLM, and Google Calendar through Composio

**Source:** the 2026-10-07 review's "Agent core-lite" (one model, read/propose tools, `po-chat`,
Hebrew and English scenarios, calendar read with time zones) plus its "confirm → execute", cut
to calendar only. Reminders, briefings, voice and memory stay in later iterations.

**Size:** two PRs. PR 1 answers and reads the calendar. PR 2 adds events, behind a confirm.

## Objective and Done-When

> An onboarded person writes "מה יש לי מחר?" or "add dinner with Noa Thursday 8pm" on WhatsApp.
> The bot answers from their own Google Calendar in their language and time zone, and adds the
> event only after they reply 1.

| Done-When | Where it is proved |
|---|---|
| An active tenant gets an LLM answer, not the fixed ack | `tests/db/test_agent_turn.py` with a fake `LLMProvider`. Staging: phone → GOWA → reply |
| "What's on tomorrow" lists the right day's events in the tenant's zone | `tests/unit/test_agent_tools.py` (zone maths, fake calendar). Staging, against a real calendar |
| A proposed event is created once on "1", never on retry, never without it | `tests/db/test_pending_action.py` (PR 2) |
| Hebrew and English scenarios pass on the chosen model | `pytest -m eval` (real model, fake calendar), run before merging each PR and on every model change |
| A tenant's calls can only reach that tenant's Google account | `tests/unit/test_composio_calendar.py`: every `tools.execute` carries the bound `connected_account_id` and the tenant's `user_id`, and the model's arguments can't override either |

## Where we start

The pieces are already in place. Nothing calls them yet:

- `interfaces/llm.py`: `LLMProvider.complete(messages, tools=)` and `Message`/`Completion`.
  `providers/llm/` is empty. `anthropic`, `openai`, `litellm` and `langfuse` are installed;
  `init_langfuse`/`get_langfuse` exist (EU host).
- `interfaces/calendar.py`: `CalendarProvider.list_events/create_event`. It has no
  implementation. `ComposioConnector` (`providers/calendar/composio.py`) does the connect flow
  only. Its `_call` already has the deadline, the shared thread limiter, telemetry off and
  error translation without chaining.
- `calendar_connections` (RLS) holds `composio_user_id` (the tenant uuid) and
  `connected_account_id`. Read it with `active_connection(session, tenant_id)`. The Google app
  has the full `auth/calendar` scope, so writes need no re-consent.
- `messaging/inbound.py`: the tenant gate calls `on_allowed(row, channel)` for an `active`
  tenant, then `_finish_known`, which records the inbound message under RLS in the row's
  finishing transaction (D7). Today `on_allowed` is `acknowledge`, the fixed ack.
- `send_once` (at most once per `inbox_id` + `kind`), `split_text`, `choices.py` (numbered
  replies on every channel), `tenants.timezone` and `tenants.language`.
- `ModelSettings`: `MODELS__CHAT_MODEL=gpt-5-mini`, `MODELS__FALLBACK_MODEL=claude-haiku-4-5`
  (still Active per Anthropic's model list on 2026-10-06; v7's Oct 15 was a no-earlier-than
  date), `request_timeout_s`, `max_retries`.
- Composio SDK 0.24.0: `composio.tools.execute(slug, arguments, *, connected_account_id,
  user_id, version)`, plus `toolkit_versions` on the client to pin the Google Calendar toolkit.

## Decisions

**D1. One provider, called through its own SDK. Recommended: Claude via `anthropic`.**
`MODELS__CHAT_MODEL` names the model. Pick it with the eval run, not from a table: run the
evals on `claude-haiku-5-5` and `claude-sonnet-5-5`, and ship the cheaper one that passes. The fallback
model is not wired: we add a fallback once an outage actually bites. LiteLLM goes from
`pyproject.toml`, because one provider doesn't need a router. If you'd rather keep
`gpt-5-mini`, the plan is the same with an `openai` adapter; only the adapter file and the
key's name change. The earlier plan had GPT first and Claude as fallback. Keeping both means a
tool-format mapping for each and twice the evals, so the plan picks one. *Confirm before PR 1.*

**D2. The model sees our tools, not Composio's.** We write two small schemas,
`list_events(start, end)` and (PR 2) `propose_event(title, start, end, location?)`. The
calendar adapter then calls `tools.execute` with the slug, the `connected_account_id` from the
tenant's active `calendar_connections` row, `user_id=str(tenant_id)` and a pinned toolkit
version. There is no tool router session, no `tools.get()` provider schemas and no workbench or
meta tools. So the model can't choose an account, a user or a toolkit, and the tenant binding
comes from an RLS row, never from model output. The schemas also stay small, and the core stays
provider-neutral. This also closes the Iteration 03 follow-up "name the bound
`connected_account_id`". Orphaned Composio accounts then can't be reached by any call, so
disabling them can wait.

This replaces the 2026-10-07 research's recommendation of Composio *session mode*
(`composio.create(user_id=…, toolkits=["googlecalendar"], manage_connections=False)` with
`session.tools()`). That mode hands the model meta tools (`COMPOSIO_SEARCH_TOOLS`,
`COMPOSIO_MULTI_EXECUTE_TOOL`, `COMPOSIO_REMOTE_WORKBENCH`), so it needs a lockdown we'd have
to verify: the Python spelling for turning the workbench off was never confirmed. It also
can't name a `connected_account_id`, and it costs more tokens. We need two actions, not tool
discovery. If the agent ever needs open-ended Google access, revisit session mode then.

**D3. The agent turn runs inside the existing `channel:handle_inbound` job, as `on_allowed`.**
It needs no new task or queue, and the sender lock already orders a person's messages. A
transient LLM or Composio error (`LLMUnavailableError`, `CalendarProviderUnavailableError`)
joins `INBOUND_RETRY`. A read-only turn is safe to re-run, because `send_once` keeps it to one
reply. On the last attempt (`pass_context=True`, `attempts == max_attempts - 1`) the turn sends
a fixed "I couldn't answer just now, try again" instead of raising, so the person never gets
silence.

**D4. History is text, from `messages`.** The prompt carries the last 20 `messages` rows (in
and out) plus the current body. The reply is recorded as `direction="out"` in `_finish_known`.
That's the same transaction as the inbound copy, so D7 still holds: `on_allowed` returns an
`Advance` with `reply=` set. Both rows share `now()`, so history orders by
`(created_at, direction = 'out')`. *shortcut: tool calls and results aren't stored, so the
model re-reads the calendar each turn. Store them if the evals show it misses context.*

**D5. Writes need a confirm (PR 2).** `propose_event` writes nothing to Google. It stores one
`pending_action` (a jsonb column on `tenants`: id, kind, payload, expires_at, one at a time)
and the reply ends with a `Choice` ("1 add / 2 cancel"). When the next message resolves to that
choice, it is handled without the LLM. A claim (`UPDATE tenants SET pending_action = NULL WHERE
id = :t AND pending_action->>'id' = :id RETURNING …`) commits first, then `create_event` runs.
So a retry or a double "1" can't create the event twice; this is at most once, as in ADR 0003.
Any other message drops the proposal, and the model sees it in history if the person is
amending it. A proposal expires after an hour.

**D6. Time and language come from the tenant row.** The system prompt gives the current local
date and time in `tenants.timezone`, the weekday, a Sunday-to-Thursday work week and
`tenants.language`, and asks for a reply in the person's language. Tool arguments are naive
local times; the adapter attaches the zone and sends RFC 3339. Events come back converted to
the zone, with **title, start, end and location only**. Descriptions and attendee lists never
reach the prompt, because anyone can send an invite into your calendar (prompt injection).
`TenantState` gains `timezone`; the column already exists.

**D7. Bounds per turn:** at most 4 tool rounds, `max_tokens` 1024, `request_timeout_s` from
`ModelSettings`. A long reply goes through `split_text`. There is no per-tenant daily cap for a
circle; a Sentry alert and a monthly bill check cover it.

**D8. Observability:** one Langfuse trace per turn (model, tokens, tool names, latency). It
holds message content, so it's EU-hosted and counts as personal data. The LLM SDK's errors are
translated without chaining, as `ComposioConnector` already does, so prompts never reach
Sentry. Logs carry `inbox_id`, tool names and token counts, never bodies.

**D9. No calendar, no guessing.** No active connection, or Composio reports the account
`EXPIRED`/`REVOKED`/401: the tool returns `calendar_not_connected`, and the reply says so with a
fresh connect link (`create_link`, as onboarding issues it). The model is told not to invent
events.

## PR 1 — the agent answers and reads the calendar

1. **Spike (half a day, staging account).** The slugs `GOOGLECALENDAR_EVENTS_LIST` and
   `GOOGLECALENDAR_CREATE_EVENT` are listed in Composio's toolkit docs (checked 2026-10-10).
   Confirm their argument names, that `tools.execute` honours `connected_account_id`, the
   current toolkit version to pin, and the response shapes. Save one redacted response per
   slug as a test fixture. Check the current model ids and the deprecations page, and set D1.
2. **`interfaces/llm.py`.** Add `ToolCall(id, name, arguments)`, then
   `Message.tool_calls: tuple[ToolCall, ...] = ()` and `Message.tool_call_id: str | None`, and
   `Completion.tool_calls`. Tools are neutral `{"name", "description", "input_schema"}` dicts.
   Drop `embed`: nothing calls it, and the memory iteration brings its own.
3. **`providers/llm/anthropic.py`.** `AnthropicLLM.from_settings` maps neutral messages and
   tools onto the Messages API (system prompt cached), maps errors onto
   `LLMUnavailableError`/`LLMRejectedError` in `core/errors.py`, and has a Langfuse span.
   Test with a stubbed HTTP transport, the way `test_composio_connector.py` does.
4. **`providers/calendar/composio.py`.** `list_events` (and in PR 2 `create_event`) go on
   `ComposioConnector`, reusing `_call`. `CalendarProvider` takes
   `(account_id, user_id, *, start, end)`, because the caller already holds the connection row.
   Pin the toolkit with `toolkit_versions` in `from_settings`.
5. **`messaging/agent.py`.**
   - `respond(history, text, tenant, *, llm, calendar, now) -> str` is pure: the prompt, the
     tool loop (D7), the zone maths (D6). `po-chat` and the evals call this.
   - `agent_turn(row, channel, tenant, *, db, …) -> Advance` is the `on_allowed` wrapper:
     load the history and connection, call `respond`, then `send_once(kind=AGENT_REPLY)`.
6. **`messaging/inbound.py`.** `OnAllowed` gets the `TenantState` and returns `Advance | None`.
   `Advance.reply` makes `_finish_known` call a new `record_outbound`. `acknowledge` keeps the
   allowlist gate, where Composio is off, unchanged.
7. **Settings and wiring.** `AgentSettings(enabled=False, api_key)` (`AGENT__*`). `Settings`
   requires `composio.enabled` when it's on, and names missing keys the same way it does for
   Composio. `worker/tasks/channel.py` picks `agent_turn` when it's enabled and `acknowledge`
   otherwise.
8. **`po-chat --tenant <uuid>`.** A terminal loop over `respond`, with the real LLM and the real
   or `--fake-calendar` provider, plus history in memory. It goes in `[project.scripts]`.
9. **Evals: `tests/evals/`, `-m eval`, excluded from the default run.** About 12 scenarios,
   half in Hebrew ("מחר בשש", "ביום חמישי הבא", "השבוע"), each asserting the tool call and
   its date range, not the wording. Also: an event title that carries an instruction, no
   connection (D9), and a question that needs no tool.

## PR 2 — events, behind a confirm

1. Migration: `tenants.pending_action jsonb NULL`. It isn't a new table, and RLS already
   covers it.
2. The `propose_event` tool, the `Choice` on the reply, and the claim-then-create path (D5) in
   `agent_turn`, ahead of the LLM.
3. `create_event` on the adapter. The created event's link goes in the confirmation.
4. Tests: confirm creates once, a retry after the claim creates nothing, expiry, another
   message drops the proposal, "2" cancels. Add three eval scenarios.

## Prerequisites (outside the code)

- An Anthropic (or OpenAI, per D1) API key on the staging **worker** as `AGENT__API_KEY`. The
  api doesn't need it. Set a monthly spend limit in the provider console.
- Staging has `COMPOSIO__ENABLED=true` and your own calendar connected. That's done if the
  Iteration 03 runbook's day-1 check passed.
- Update `MODELS__CHAT_MODEL` on staging and production after the evals pick a model.

## Out of scope

Memory, reminders and briefings (`scheduled_jobs`), voice notes, groups, Google Tasks, photo →
event, a fallback provider, cost dashboards, Composio triggers (calendar-change webhooks),
editing or deleting events, and disabling orphaned Composio accounts (harmless after D2).
The groups plan (`do-3-but-with-snoopy-lagoon`) later adds `calendar_id` and `free_busy()`
(`GOOGLECALENDAR_FREE_BUSY_QUERY`) to `CalendarProvider`. Those arrive with groups, not here.

## Risks and open questions

- **Prompt injection through the calendar.** D6 leaves out descriptions, and D5 means nothing
  is written without the person's "1". The worst case left is a misleading summary.
- **Hebrew relative dates and the Sunday-start week.** That is why the evals assert date
  ranges. If the chosen model fails them, try the next model up before adding parsing code.
- **Composio's response shape or slug drift.** The toolkit version is pinned, and the fixtures
  from the spike catch a change at upgrade time.
- **Personal data at the LLM provider and Langfuse.** That's acceptable for a circle, but say it
  in the invite message. Check the provider's retention and training defaults for API traffic.
- **Turn latency inside the sender lock.** A 10–20 s turn holds the person's next message. That
  keeps the order right; watch it in Langfuse.
