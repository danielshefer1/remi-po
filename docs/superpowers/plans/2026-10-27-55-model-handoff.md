# #55 Hand-off to a Stronger Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Haiku keeps every normal turn. When it sees a turn too big or tricky for it (a whole
timetable, many items at once, multi-step planning), it hands the turn to a stronger model.
The person never hears about it.

**Architecture:** When a strong model is configured, the private agent gets one more tool,
`hand_off(reason)`. It is allowed only before any write in the turn. Calling it ends the cheap
model's turn, and `respond` runs the same turn again from the top on the strong `LLMProvider`,
with no `hand_off` tool. Each hand-off is logged and tagged in Sentry with its reason.

**Tech Stack:** Python 3.14, Anthropic SDK via `AnthropicLLM`, pydantic-settings, sentry-sdk.

**Spec:** GitHub issue #55, as narrowed by the user on 2026-10-27: the small model asks first,
with no up-front routing and no retry after a failure.

## Global Constraints

- `MODELS__STRONG_MODEL` is optional (`None` turns the feature off). The value is set per
  environment, as `claude-sonnet-5-5` on staging.
- These are writes: `set_reminder`, `cancel_reminder`, `set_briefing`, `set_timezone`,
  `remember`, `forget`, `let_see_details`, `stop_details`, `stop_sharing_calendar`,
  `answer_outing`. `propose_event` writes nothing until confirmed, so it is not one, but its
  `found` list is reset on the re-run. After any write, `hand_off` returns `error: too late to
  hand off; finish it yourself`.
- A turn hands off at most once. The strong model's run has no `hand_off` tool.
- The private agent only. Group turns, onboarding and the briefing don't change.

## Review Focus

1. **The cheap model writes, then hands off.** Expect a refusal, with no duplicate write.
   Pinned in Task 2 (`test_hand_off_after_write_is_refused`).
2. **The strong model is unavailable.** Expect the same retry and fallback path as today
   (`LLMUnavailableError` reaches `agent_turn`). Pinned in Task 2
   (`test_strong_unavailable_raises_for_retry`).
3. **A proposal made before the hand-off.** Expect only the strong model's proposal under the
   reply. Pinned in Task 2 (`test_proposal_from_cheap_run_is_dropped`).
4. **The feature is off** (`strong_model` unset). Expect no `hand_off` in the tools, and
   behaviour exactly as today. Pinned in Task 2 (`test_no_strong_no_tool`).
5. **Cost visibility.** Every hand-off logs `agent.handoff` with the reason and sets the Sentry
   tag. Pinned in Task 2 (`test_hand_off_logs_reason`).

---

### Task 1: Settings and the strong client

**Files:**
- Modify: `src/personal_organizer/settings.py` (`ModelSettings.strong_model: str | None = None`),
  `src/personal_organizer/providers/llm/anthropic.py` (`from_settings(settings, *, model: str |
  None = None, http_client=None)`), `src/personal_organizer/worker/tasks/channel.py` (`strong_llm()`)
- Test: `tests/unit/test_settings.py`, `tests/unit/test_anthropic_llm.py`

**Interfaces:**
- Produces: `strong_llm() -> AnthropicLLM | None`, cached like `agent_llm`. It is `None` when
  the agent is off or `strong_model` is unset.

- [ ] **Step 1: Write failing tests:** `test_strong_model_defaults_to_none`,
  `test_from_settings_uses_given_model` (the request's `model` is the override).
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. **Step 4:** Run them. Expect
  PASS, then the task gate.
- [ ] **Step 5: Commit** `feat(llm): an optional stronger model setting`.

### Task 2: The `hand_off` tool in `respond`

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py` (`HAND_OFF_TOOL`, `_WRITE_TOOLS`,
  `respond(..., strong: LLMProvider | None = None)`, `agent_turn(..., strong=None)`, one prompt
  line), `src/personal_organizer/worker/tasks/channel.py` (pass `strong_llm()` to
  `agent_turn`), `src/personal_organizer/admin/chat.py` (pass `None`)
- Test: `tests/unit/test_agent_tools.py`

**Interfaces:**
- Consumes: `strong_llm()` (Task 1).
- Produces: a tool named `hand_off`, with `input_schema` `{"reason": string, required}`.
  `respond` with `strong` set adds it to `TOOLS`. Inside `respond`, a private exception
  `_HandOff(reason)` raised from `run_tool` is caught around `converse`. `respond` then calls
  itself with `llm=strong, strong=None` and an emptied `proposed` list.

- [ ] **Step 1: Write failing tests,** using the scripted LLM from `test_agent_tools.py`:
  `test_hand_off_reruns_on_strong` (cheap calls `hand_off`, strong replies "done", and the
  reply is "done"), `test_hand_off_after_write_is_refused`, `test_no_strong_no_tool`,
  `test_proposal_from_cheap_run_is_dropped`, `test_strong_unavailable_raises_for_retry`,
  `test_hand_off_logs_reason` (with `structlog.testing.capture_logs`).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.** Add this prompt line under Rules, shown only when the tool is
  present: "If what they ask is big or tricky (a whole timetable, many items at once, planning
  in several steps), call hand_off first, before any other tool, with a few words on why." Set
  the tag with `sentry_sdk.set_tag("agent.handoff", "yes")`.
- [ ] **Step 4: Run** the tests, plus `tests/db/test_agent_turn.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(agent): the chat model can hand a hard turn to a stronger one`.

### Task 3: Evals, PR, staging

**Files:** `tests/evals/test_agent_evals.py`. The fixture builds `strong` from
`MODELS__STRONG_MODEL`, defaulting to `claude-sonnet-5-5`.

- [ ] **Step 1: Evals:** `test_timetable_hands_off` (the 2026-10-26 Hebrew student timetable
  plus "remind me 20 minutes before every class": `hand_off` is called, then the strong run
  sets the reminders, as repeating ones once #52 is merged), `test_simple_question_stays_cheap`
  ("what do I have tomorrow?" makes no `hand_off`, in both languages).
- [ ] **Step 2: Run** them 3 times after Track A has merged. Each passes at least 2 of 3.
- [ ] **Step 3:** Draft PR (`Closes #55`). After merge, set `MODELS__STRONG_MODEL` on staging
  `worker` and `api`, then resend the timetable and check the log shows `agent.handoff` for that inbox_id
  and no `agent.fallback` or timeout for it.
