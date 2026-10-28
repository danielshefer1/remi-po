# #56 Short Replies, Split Like Texts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remi texts like a person: the answer first in a line or two, details in a second
bubble, and a question last, in both Hebrew and English.

**Architecture:** The model marks a break between bubbles with a line holding only `---`.
`bubbles()` splits on it, at most `MAX_BUBBLES` parts. `send_parts()` sends each part as its
own `ChannelOutbox` row: part 1 keyed `(inbox_id, kind)` as today, part *n* keyed
`(inbox_id, f"{kind}:{n}")`, `PART_GAP_S` apart, in order. A confirm question or connect line
always rides on the last part. The prompts change to put the answer first and drop filler.

**Tech Stack:** Python 3.14, asyncio, the existing outbox (ADR 0003).

**Spec:** GitHub issue #56; CLAUDE.md product voice.

## Global Constraints

- `BREAK = "---"`, `MAX_BUBBLES = 3`, `PART_GAP_S = 1.2`. Parts past the third are joined into
  the third with `"\n\n"`.
- At most once (ADR 0003). A re-run whose first part is already claimed sends nothing, as
  `already_sent` does today. Comment that limit in code:
  `# shortcut: a crash between parts loses the later parts; a re-run would write a different
  reply`.
- A part that is not `accepted` stops the rest.
- The history records the parts joined by `"\n\n"`, so the model never sees `---` echoed back
  as text. The group context records the same.
- No preamble ("Sure! I'd be happy to…"), no recap of their message, no closing "let me know
  if…". Lists only when the content is a list. Hebrew is written as Hebrew, and the gender note
  is respected.

## Review Focus

1. **The model writes `---` inside a list or a quote.** Only a line that is exactly `---` after
   stripping breaks. Pinned in Task 1 (`test_inline_dashes_dont_split`).
2. **Ten bubbles.** Expect 3 parts, nothing lost. Pinned in Task 1 (`test_caps_at_three`).
3. **The confirm question with a split reply.** It is on the last part, whole, and a "yes"
   still confirms. Pinned in Task 2 (`test_confirm_rides_last_part`).
4. **Part 2 refused by the channel.** Expect part 3 not sent, and the reply still recorded.
   Pinned in Task 2 (`test_refused_part_stops_rest`).
5. **A job re-run after part 1 was sent.** Expect no part sent again. Pinned in Task 2
   (`test_rerun_sends_nothing`).

---

### Task 1: `bubbles()`

**Files:**
- Modify: `src/personal_organizer/messaging/text.py`
- Test: `tests/unit/test_split_text.py`

**Interfaces:**
- Produces: `bubbles(reply: str) -> list[str]`, with stripped, non-empty parts (`[""]` for an
  empty reply), plus the `BREAK` and `MAX_BUBBLES` constants.

- [ ] **Step 1: Write failing tests:** `test_splits_on_break_line`,
  `test_inline_dashes_dont_split` (`"a --- b"` and `"----"` don't split), `test_caps_at_three`,
  `test_no_break_is_one_part`, `test_empty_parts_dropped`.
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. **Step 4:** Run them. Expect
  PASS.
- [ ] **Step 5: Commit** `feat(text): split a reply into bubbles`.

### Task 2: Sending parts, private and group

**Files:**
- Modify: `src/personal_organizer/messaging/outbox.py` (`send_parts`),
  `src/personal_organizer/messaging/agent.py` (`respond` puts the confirm or connect suffix on
  the last part; `agent_turn` sends with `send_parts`; `fit_message` per part),
  `src/personal_organizer/messaging/groups/turn.py` (the group reply send)
- Test: `tests/db/test_outbox.py`, `tests/db/test_agent_turn.py`, `tests/db/test_group_turn.py`

**Interfaces:**
- Consumes: `bubbles` (Task 1).
- Produces: `send_parts(db, channel, *, inbox_id: UUID, kind: str, recipient_key: str, to: str,
  parts: Sequence[str]) -> str`. It returns part 1's status and sleeps `PART_GAP_S` between
  parts (patched to 0 in tests). `respond` still returns `str`, and the parts are split in
  `agent_turn`. Because of that, `respond` checks the suffix length against the **last**
  part's length.

- [ ] **Step 1: Write failing tests:** `test_parts_sent_in_order_with_own_keys`,
  `test_rerun_sends_nothing`, `test_refused_part_stops_rest`, `test_confirm_rides_last_part`,
  `test_group_reply_split`.
- [ ] **Step 2: Run them.** Expect FAIL. **Step 3:** Implement. **Step 4:** Run the full suite.
  Expect PASS.
- [ ] **Step 5: Commit** `feat(outbox): send a reply as several texts, in order, at most once each`.

### Task 3: Voice in the prompts

**Files:**
- Modify: `src/personal_organizer/messaging/agent.py` (the "How you sound" block of `_PROMPT`),
  `src/personal_organizer/messaging/groups/prompt.py`, and a pass over
  `messaging/onboarding_prompt.py`, `onboarding_text.py` and `confirm.py` for preambles and
  closing offers

- [ ] **Step 1: Rewrite "How you sound"** (agent; the group prompt mirrors it in the group's
  register):
  - "Lead with the answer, in a line or two. Add details only when they matter or they ask."
  - "No warm-up ('Sure!', 'בשמחה!'), no repeating what they said, no closing offers ('let me
    know if…')."
  - "Lists only when the content is a list, like a day's schedule."
  - "When there's an answer plus more, you can send it as separate texts the way people do:
    put a line with only --- between them. At most three, the question (if any) last. Most
    replies are one text."
  - "Brief message, brief answer."
- [ ] **Step 2: Fixed copy.** Trim any preamble or closing offer in the onboarding and confirm
  texts, in both languages. `tests/unit/test_onboarding_text.py` stays green.
- [ ] **Step 3: Commit** `feat(voice): short answers first, split like texts`.

### Task 4: Evals, PR, staging

**Files:** `tests/evals/test_agent_evals.py`, `tests/evals/test_group_evals.py`

- [ ] **Step 1: Evals,** English and Hebrew each:
  - `test_tomorrow_is_short`: "what do I have tomorrow?" with 2 events; the first bubble is at
    most 160 characters and names the first event.
  - `test_remind_at_5_is_one_line`: one bubble, at most 100 characters.
  - `test_no_filler`: no `Sure|Happy to|let me know|בשמחה|אשמח|תודיע` in 5 typical turns.
  - `test_answer_then_question_splits`: an ambiguous add, "dinner with Dana Thursday",
    becomes 2 bubbles with the question last.
  - The existing evals stay green.
- [ ] **Step 2: Run** `-k "short or filler or split"` and the full eval suite 3 times. Each
  passes at least 2 of 3.
- [ ] **Step 3:** Draft PR (`Closes #56`). Staging: ask something that produces a split reply,
  and check on the phone that the parts arrive in order.
