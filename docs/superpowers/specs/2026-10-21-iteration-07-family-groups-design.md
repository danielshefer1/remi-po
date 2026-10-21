# Iteration 07 design — Remi in the family group

**Source:** the 2026-10-07 re-plan, item 5 ("group @mentions"), chosen by the user on
2026-10-21 as the first thing to build after Iteration 06.

## Why

The user's family wants one place to see what everyone is doing and to plan outings
together, without a shared Google calendar. They already talk in a family WhatsApp group.
Remi joins that group. Mom asks "Remi, what's everyone doing this weekend?" and gets an
answer there. Someone asks "Remi, is everyone free on Friday?", the family settles on a
time, Remi sends a poll, and everyone who votes "in" gets the outing in their own calendar.

## Done-When

> In the family group, Mom writes "רמי, מה כולם עושים בשבת?". Remi answers in the group with
> who is busy when, and no event names. Daniel has let Mom see his details, so Mom also gets
> a private message with Daniel's events. Daniel writes "Remi, is everyone free Friday
> evening?", Remi answers with the free window, Daniel says "let's do dinner at 7, set it
> up", and Remi posts a poll. Noa votes "in" and "Family dinner" appears in her Google
> Calendar at 7. Eden doesn't vote and says nothing, so each day Remi privately asks her,
> warmly, whether she's coming, until she votes, gives a reason, or Friday 7pm arrives.

## Decided with the user (2026-10-21)

- **Where people look:** in the group, by asking Remi. No shared Google calendar.
- **Privacy in the group:** busy/free only, never what an event is.
- **Details:** each person can let specific people see their event details. Those details
  are sent to that person **privately**, never in the group.
- **Consent per group:** a person's busy/free shows only in groups where they have told
  Remi it's okay. Being in a group with Remi is not consent.
- **Confirming an outing:** by WhatsApp poll. Each person who votes "in" gets the event
  added to their own calendar.
- **People who haven't answered:** Remi keeps messaging them until they vote or give a
  reason they can't come.
- **Remi reads every message in the group,** not only those addressed to it, so it notices
  "can't make it Friday, I'm working" said to the family. It still only *speaks* when
  addressed.

## What the 2026-10-21 poll test showed

On staging (GOWA v9.6.0), a poll sent to the user's number rendered, and the vote came back
to `/webhooks/gowa` as a normal `message` event. The parser kept it as `message_type: poll`
and the worker handled it (`inbound.handled`, 12:15:42 UTC). Not yet confirmed: that the
payload holds `poll.type: "vote"` and `selected_options`, as GOWA's
`docs/webhook-payload.md` at v9.6.0 says. The stored copy can't show it: handling a message
nulls `raw` (`messaging/inbound.py:172`, `_PURGED`). The test is re-run as the first
build step, with the parser logging the poll's `type`, `resolution_status` and option
names (no phone numbers), and once more with the poll sent to a test group.

**If votes don't carry the option, or polls fail in groups** (my call, offered to the user
on 2026-10-21 without objection): Remi posts the plan as a plain message and each person
says in their own words whether they're in, read by the model. Everything else in this
design stays the same.

## Goals

1. Remi works in WhatsApp groups on the GOWA channel when someone calls it by name ("Remi",
   "רמי") or @-tags it, and stays silent otherwise.
2. Answers who is busy when, for the people in that group who okayed it, in the group.
3. Sends event details privately to an asker who has been granted them.
4. Turns an agreed plan into a group poll; a "yes" vote adds the event to the voter's
   calendar.
5. Follows up privately, once a day, with members who haven't answered.

## Non-goals (v1)

- Groups on the Meta channel. GOWA only.
- A poll that chooses between several times or days. v1 polls are "in" / "can't make it"
  for one plan.
- Removing an event when someone changes their vote from "in" to "out" (the calendar
  provider has no delete). Remi tells them privately it stayed in their calendar.
- Messaging people who are in the group but are not Remi members. They can ask questions in
  the group, but Remi never messages them privately.
- Summarising the group's conversation, or acting on it unasked (Remi reads it, see
  "Reading the group", but never speaks unaddressed).

## Anti-goals (failure even if it "works")

- An event title, location or note shown in the group, from anyone, in any wording. This
  must be impossible because of the code, not just the prompt: titles never reach the model
  in a group turn.
- Remi chiming in on messages that weren't addressed to it.
- Group chatter kept in our database beyond the short context window below.
- Follow-ups that feel like a bot nagging: canned text, several per day, or sent in the
  group where everyone sees who hasn't answered.
- Anything that reads like a menu, a command or a form (CLAUDE.md "Product voice").

## Constraints from the codebase

- **Every personal table is per tenant with FORCE RLS** (`db/repositories/scope.py`,
  `tenant_session`). A group turn needs several people's calendars. Read each person's
  calendar in that person's own `tenant_session`, and only for people who okayed this group.
  Tenants never read each other's rows directly. The new cross-tenant lookup (which tenants
  okayed group G) is the security-sensitive part and gets its own isolation tests, like
  `tests/db/test_isolation.py`.
- **The gateway filters groups today:** `WHATSAPP_WEBHOOK_IGNORE_JIDS=@g.us,…` on the staging
  `gowa` service, and `providers/channel/gowa/parser.py:45` skips `@g.us` chats. Both
  change. Changing the gateway variable restarts the gateway; its session lives on the
  volume.
- **Calendar port** (`interfaces/calendar.py`): `list_events` and `create_event` only. Free
  and busy times come from `list_events`, with titles dropped before anything else sees
  them.
- **Outbound** (`interfaces/channel.py`): `send_text(OutboundMessage(recipient, body))`.
  Polls need a GOWA-only `send_poll`. GOWA v9.6.0 `POST /send/poll`
  (`phone`, `question`, `options`, `max_answer`) works for a direct chat; a group JID is
  expected to work the same way, which is to be checked.
- **Scheduling:** `schedule:tick` runs every minute (Iteration 05). Follow-ups ride it.
- **Voice:** Hebrew and English, written natively, in `messaging/onboarding_text.py`-style
  text modules and the agent prompt (CLAUDE.md).

## The behaviour

### Joining a group

A member adds Remi's number to a group. Remi says nothing until it is addressed. The first
time it's addressed in a group where nobody has okayed sharing, it answers the question as
best it can (nobody's calendar), and says in passing that each person can tell it if it's
okay to share their schedule here.

### Reading the group

Remi receives every message in groups it serves (see "Which groups"). For each one:

- **Addressed to Remi** (see "Addressed"): a group turn, answered in the group.
- **Not addressed, from someone with an open, unanswered poll in this group:** a cheap
  model check reads it against the open poll(s). If it's a reason they can't come, or a
  plain "I'm in", it's stored as their answer and follow-ups stop. Remi says nothing.
- **Anything else:** no model call.

Every group message, addressed or not, joins a short rolling context for that group (my
call: the last 20 messages from the last 6 hours). Older ones are deleted, not just hidden.
This is what lets "let's do dinner at 7, set it up" make sense to Remi. After handling, an
inbox row's content is purged as today (`_PURGED`).

### Okaying a group (consent)

A member says, in that group, in their own words: "Remi, you can share my calendar here" /
"רמי, אפשר לשתף פה את היומן שלי". Remi confirms in one short line. "Stop sharing my
calendar here" undoes it. The okay is per person, per group. Only Remi members can give it,
since only they have a connected calendar.

### Asking who's free

Anyone in the group may ask, member or not. Remi reads the calendars of the people who
okayed this group, for the window asked about, and answers in the group with names and busy
times only ("Friday from 6 works for everyone. Noa's busy until 5:30."). People who haven't
okayed are named as "I can't see X's calendar" only if the asker named them. Otherwise
they're left out.

### Details, privately

A member grants details in a private chat with Remi: "Mom can see what my events are". The
grant is per person, from owner to viewer, and holds in every group. "Stop showing Mom my
details" revokes it. When someone with grants asks in the group, the group answer stays
busy/free. Remi also sends the asker a private message with the granted people's events in
that window. If the asker isn't a Remi member, there's nobody to message, so no details go
out.

The viewer is identified by their own Remi identity (phone). A name typed by the owner
("Mom") is resolved against the group's members or the owner's notes, and Remi confirms who
it understood ("Got it, Rina (Mom) can now see your event details").

### Setting up an outing

When the group agrees on a plan and someone asks Remi to set it up, Remi posts one poll:
the question is the plan in plain words ("Family dinner, Friday at 7?"), and the options are
"I'm in" / "Can't make it" (Hebrew: "אני בפנים" / "לא מסתדר לי"). The poll is stored with
its title, start, end, the group and who asked.

### Votes

- **"In" from a member with a connected calendar:** the event is created in their
  calendar. Remi reacts or answers in one short private line. It never posts "added" in
  the group.
- **"In" from someone without a calendar:** counted, nothing written, no message.
- **"Can't make it":** counted as an answer. No follow-ups.
- **Changed from "in" to "out" after the event was added:** a private line saying it
  stayed in their calendar (non-goal: delete).
- A vote is the confirmation. No second "are you sure" (CLAUDE.md: confirmation where an
  action writes somewhere real, read from the person's own action).

### Follow-ups

Who gets them: Remi members in the group who okayed sharing and haven't voted on an open
poll. Once a day, at a sensible local time, Remi messages them privately. Each message is
different and warm ("Hey, are you joining Friday's dinner? No pressure, just let me know").
It stops when they vote, when they give any reason in their own words (in the group or
privately, read by the model, not a keyword), or when the event starts. A reason is stored
with their vote, so later "who's coming?" questions can say "Eden can't, she's at work".

## Data (sketch; names are the builder's)

- **group:** network + JID. No personal content. Not tenant-scoped.
- **group share (consent):** group + tenant. The only path from a group to tenants.
- **detail grant:** owner tenant + viewer tenant.
- **outing poll:** group, poll message id, option names (or hashes), title, start, end,
  created by, status.
- **poll answer:** poll + tenant, answer (in / out / reason text), the created event's
  external id, last follow-up time.

## Risks to check on day 1

1. **The vote payload carries the selected option** (see "What the poll test showed").
2. **Polls sent to a group JID** render and their votes arrive with the voter identified.
3. **LID-only senders in groups.** WhatsApp often identifies group participants by LID, not
   phone. Our tenants are keyed by phone (`tenant_identities`). If group senders arrive as
   LIDs, map them to tenants, for example by reading the group's participant list from
   GOWA and linking the LID as a second identity (`messaging/tenancy.py:_link`). Without
   this, Remi can't tell who asked or who voted.
4. **How mentions arrive:** whether GOWA's payload lists mentioned JIDs, or only the text
   "@972…".

## Left to the builder

PR split, table and column names, the group toolset's shape (a separate small tool set
for group turns is expected: free/busy, set up outing, give/revoke okay, nothing personal),
the exact size of the rolling group context, the follow-up hour, and whether a vote is acknowledged by a reaction or a line.

## Calls I made (not discussed with the user)

- **Addressed** means: the text names Remi ("Remi", "רמי", any case), the message @-tags
  Remi's number, or it quote-replies a message Remi sent, including the poll, or it comes
  from the person Remi just answered, within a few minutes, and reads as a follow-up (the
  model decides, with the group context). In the Done-When, Daniel's "let's do dinner at 7,
  set it up" is that last case.
- **Which groups:** Remi answers only in groups that have at least one Remi member as a
  participant (checked against the group's participant list from GOWA). Elsewhere it stays
  silent and spends no model call. No cap on non-member questions, given a circle of ~50.
- **Details follow both rules:** Mom gets Daniel's details privately only if Daniel okayed
  *this* group and granted Mom. A grant alone never opens details in a group Daniel hasn't
  okayed.
- **Names:** the WhatsApp profile name GOWA sends (the parser keeps it), until the person
  says what to call them ("call me Dani"), which is stored on their tenant. A grant's "Mom"
  is resolved through the owner's notes or by asking once ("Who's Mom here, Rina?").
- **"Stored" in the anti-goals means our database.** The gateway already keeps WhatsApp Web's
  own chat store for direct chats; groups are no different.
- Anyone in the group can ask. Only members can okay, be nudged, or get details.
- Details grants hold across all groups. The okay to show busy/free is per group.
- v1 polls are yes/no for one plan; no delete on vote change.
- Follow-ups go to members only, once a day, privately, until vote/reason/event start.
- Groups only on GOWA.
