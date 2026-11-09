# ADR 0003 — A reply whose delivery is uncertain is not sent again

**Status:** accepted (Iteration 02)

## Context

A job can run more than once. Procrastinate retries a failed attempt, the stalled-job task
re-queues a job whose worker died mid-run, and every merge to `main` redeploys staging and
kills whatever was running. Any job that sends a message therefore has to decide what to do
when it finds that an earlier attempt *might* have sent it.

WhatsApp's Cloud API gives no help: there is no idempotency key, so the same request sent
twice is two messages on the user's phone. And some failures cannot tell you whether the
message went out — a read timeout, a connection reset after the request was written, a
worker killed between the HTTP call returning and the result being recorded.

## Decision

**When delivery is uncertain, do not resend.** Each reply is claimed in `channel_outbox`
under `UNIQUE (inbox_id, kind)` before it is sent, and moves through
`pending → sending → accepted | failed | unknown`:

- **Definitely not sent** — connect error, pool timeout, HTTP 429, 5xx, a throttling code —
  puts the row back to `pending` and raises `TransientChannelError`, which is the only
  exception the task's retry strategy retries.
- **Refused** — any other 4xx — records `failed` and the Graph error code. Not retried: it
  would be refused the same way.
- **Uncertain** — a timeout after the request left, or a retry that finds the row still
  `sending` because the previous attempt died holding it — records `unknown` and stops.

The trade is deliberate: an occasional missing "Got it" is invisible next to a duplicated
one, and far better than a duplicated reminder or a doubled calendar confirmation later on.

## Consequences

- `messaging.outbox.send_once` is the only way a reply is sent, and it is safe to call any
  number of times for the same `(inbox_id, kind)`.
- `unknown` rows are the signal to watch. A burst of them means Graph latency is near
  `WHATSAPP__SEND_TIMEOUT_S`; raise the timeout rather than adding retries.
- The outbox stores delivery state and Meta's numeric error code only — never message text,
  and never Graph's error message, which can quote the request back.
- `tests/db/test_handle_inbound.py` pins each path: transient-then-success sends once, an
  ambiguous failure is not retried, a row left in `sending` becomes `unknown` without a send.
- Iteration 04 splits long agent replies into several messages (`messaging.text.split_text`).
  Giving each chunk its own outbox row — a distinct `kind` per chunk, since the claim is
  unique on `(inbox_id, kind)` — lets a partly delivered reply resume at the first unsent
  chunk rather than starting over.
- From Iteration 03 a send that answers no inbound message claims on
  `channel_outbox.idempotency_key` instead (D6). The first is "You're all set" under
  `connected:<connection_id>`, and reminders will follow. The states and the rule are the same.
  The key names the event, is unique across kinds and never carries content.
  `tests/db/test_outbox.py` pins the same paths for it.
- An attachment is rebuilt by the job's re-run and never stored: the outbox row keeps only the
  delivery state, so a retry that finds the row claimed sends nothing and loses no file.
