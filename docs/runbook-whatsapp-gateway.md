# Runbook — WhatsApp through the GOWA gateway

The `gowa` channel is WhatsApp through a linked device: a GOWA container holds a WhatsApp Web
session for a **dedicated SIM** and relays its messages to us. Why, and what it risks, is
docs/adr/0004. It runs beside Meta's channel or instead of it; each is switched on alone.

It is done on staging when **an invited number that messages the bot's number gets the
acknowledgement once, and any other number gets the invite-only line once a day**.

| Phase | Needs | Proves |
|---|---|---|
| A. Local, simulated | nothing | signature → inbox → queue → worker → send attempt |
| B. Local, a real phone (optional) | the SIM's phone | QR linking, a real round trip |
| C. Staging | the SIM's phone, the Railway dashboard | the deployed path end to end |

---

## The variables

On our **api and worker** (set the same set on both):

| Variable | Value |
|---|---|
| `GOWA__ENABLED` | `true` |
| `GOWA__BASE_URL` | where the worker reaches the gateway: `http://gowa.railway.internal:3000`, or its public `https://` domain (see C5) |
| `GOWA__BASIC_AUTH_USER` | the user in the gateway's `APP_BASIC_AUTH` |
| `GOWA__BASIC_AUTH_PASSWORD` | its password: `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GOWA__WEBHOOK_SECRET` | the gateway's `WHATSAPP_WEBHOOK_SECRET`: `uv run python -c "import secrets; print(secrets.token_hex(32))"` |
| `GOWA__DEVICE_ID` | leave unset (one device) |
| `GOWA__TYPING_DELAY_S` | default `3`; between `0` and `10` |
| `GOWA__TYPING_JITTER_S` | default `2`; between `0` and `5` — a random extra on each pause |
| `WHATSAPP__ALLOWED_PHONES` | the invite list, for every channel |

With `ENABLED=true` a service refuses to boot without the auth pair and the secret, and
names every missing one. The secret must be at least 32 characters: the gateway's default is
the literal `secret`. When deployed, `GOWA__BASE_URL` must be `https://` or a
`*.railway.internal` host, because the basic-auth password goes with every send.

On the **gateway**:

| Variable | Value | Why |
|---|---|---|
| `APP_PORT` | `3000` | |
| `APP_BASIC_AUTH` | `<user>:<password>`, the pair above | its API can send as the bot's number |
| `WHATSAPP_WEBHOOK` | `https://<api's public domain>/webhooks/gowa` | where messages go |
| `WHATSAPP_WEBHOOK_SECRET` | the secret above | signs every webhook |
| `WHATSAPP_WEBHOOK_EVENTS` | `message,message.ack` | nothing else is used |
| `WHATSAPP_WEBHOOK_IGNORE_JIDS` | `@g.us,@newsletter,status@broadcast` | groups, channels, statuses |
| `WHATSAPP_AUTO_DOWNLOAD_MEDIA` | `false` | on by default: would keep every photo on its volume |
| `MCP_ENABLED` | `false` | on by default: an MCP server that can send as the number |

Leave `WHATSAPP_AUTO_REPLY` unset: the gateway would answer everyone itself.

---

## Phase A — local, simulated

```sh
# .env: GOWA__ENABLED=true plus the three credentials (any values; the secret >= 32 chars)
uv run uvicorn personal_organizer.api.main:app --reload --no-access-log
uv run po-worker
uv run po-gowa simulate --url http://localhost:8000/webhooks/gowa --from +31612345678 --replay 10
```

Expect ten `-> 200`, one `channel_inbox` row with `channel = 'gowa'` and one job. With no
gateway running, the worker logs `gowa.unreachable` and the outbox row goes back to `pending`
to be retried, which is correct: nothing was sent.

## Phase B — local, a real phone (optional)

1. `docker compose --profile gateway up -d gowa`. Its settings are in `docker-compose.yml`;
   copy the three `GOWA__*` values from its comment into `.env` and restart the api and
   worker.
2. Open <http://localhost:3000>, log in as `po`, and start a login. On the SIM's phone:
   WhatsApp → Settings → Linked devices → Link a device, then scan the QR code.
3. `uv run po-gowa status` should print `connected=True logged_in=True`.
4. From an invited phone, message the SIM's number. You get the acknowledgement once.

`./.gowa` now holds a live login to that account. `docker compose --profile gateway down`
stops it; deleting `./.gowa` (or unlinking the device on the phone) ends the session.

## Phase C — staging

1. **Create the service.** In the Railway project, staging environment: New → Docker image
   → `aldinokemal2104/go-whatsapp-web-multidevice:v9.6.0`. Name it `gowa`. Set its start
   command to `rest`.
2. **Give it a volume** mounted at `/app/storages`. Without one, every redeploy logs the
   device out and needs a new QR scan.
3. **Set its variables** from the gateway table above.
4. **Link the phone.** Generate a public domain for `gowa` (Settings → Networking), open it,
   log in with the basic-auth pair, and scan the QR code from the SIM's phone as in B2. The
   UI should report the device as logged in.
5. **Point our services at it.** On `personal-organizer` (api) and `worker`, set the `GOWA__*`
   variables, with `GOWA__BASE_URL=http://gowa.railway.internal:3000`. Redeploy both. If
   the worker logs `gowa.unreachable` on a send, the private network is not reaching the
   gateway: use its public domain instead, `GOWA__BASE_URL=https://<gowa domain>`, and keep
   that domain. Otherwise you may remove it once linked.
6. **Turn the emulator off.** Staging's `WHATSAPP__*` was pointed at an emulator for Iteration
   02 (memory: whatsapp-emulator-testing). Set `WHATSAPP__ENABLED=false` unless Meta is
   live, and remove `WHATSAPP__GRAPH_BASE_URL`.
7. **Prove it.** Message the SIM's number from an invited phone: one acknowledgement. From a
   number that is not invited: the invite-only line, and silence for a second message that
   day. In the api's logs: `ingress.recorded` with `channel: gowa`; in the worker's:
   `inbound.handled`.
8. **Prove the alarm.** Every five minutes the worker asks the gateway what `po-gowa status`
   asks (`system:gowa_health`). While the answer is bad it logs `gowa.unhealthy` at error
   level and sends Sentry one event; all of them group into a single issue named
   `gowa.unhealthy`, tagged with the `reason`. To see it work, set the worker's
   `GOWA__BASE_URL` to `http://gowa.railway.internal:3999` (a port nothing listens on) and
   redeploy the worker. Within five minutes Sentry shows `gowa.unhealthy` with
   `reason: unreachable`. Put the URL back, redeploy, and **resolve the issue** in Sentry. A
   resolved issue reopens (a regression) the next time, and alerts again. An unresolved one
   just collects events. Check that the Sentry project's alert rules email you on a new issue
   and on a regression.

Production waits until staging has run cleanly for a while, ideally with Meta's verification
outcome known.

---

## Voice notes (Iteration 06)

A voice note is downloaded from the gateway, transcribed with Soniox, and handled as text.
Only the worker does this.

1. **Set the worker's variables.** `VOICE__ENABLED=true`, `VOICE__API_KEY` (a Soniox API key
   from console.soniox.com) and `MODELS__TRANSCRIPTION_MODEL=stt-async-v5`. Set them on the
   **worker** only. Soniox keeps nothing: each upload and transcription is deleted once read.
2. **Leave the gateway's auto-download off.** `WHATSAPP_AUTO_DOWNLOAD_MEDIA` stays `false`.
   The worker asks the gateway for each file when it needs it (`GET /message/{id}/download`).
3. **Watch the gateway's volume.** The gateway keeps each downloaded file under its statics
   directory, on the volume that already holds the WhatsApp login. After a week, check
   whether it prunes them: `du -sh` on the volume through `railway ssh`. If it does not,
   add a cleanup.
4. **Staging check.** Send one voice note in Hebrew and one in English. Each should get a
   proper reply. In the worker's logs, the `voice.heard` lines show `chars`, never the text.
5. **Save a real fixture.** Copy one real webhook body from the gateway, with the phone
   numbers replaced, as `tests/fixtures/gowa/audio.json`.

If a voice note gets the fallback line ("Sorry, I couldn't quite catch that voice note"),
the transcription failed or came back empty, the file was over 20 MB, voice is off, or the
message came through the Meta channel. Check the worker's
logs for the reason. A re-run of the same message never sends a second reply.

## When replies stop

| Symptom | Likely cause | Fix |
|---|---|---|
| Sentry issue `gowa.unhealthy`, `reason: not_logged_in` | the linked device was logged out | re-link (below), then resolve the issue |
| `gowa.unhealthy`, `reason: unreachable` | the gateway is down, or not reachable at `GOWA__BASE_URL` | check the `gowa` service; see C5 |
| `gowa.unhealthy`, `reason: bad_response`, `status_code: 401` | the worker's `GOWA__BASIC_AUTH_*` differs from the gateway's `APP_BASIC_AUTH` | make them equal |
| `gowa.unhealthy`, `reason: not_connected`, once | the gateway was reconnecting to WhatsApp when it was asked | nothing, if it does not recur; recurring means the gateway's network |
| api logs `gowa.signature_rejected` | `GOWA__WEBHOOK_SECRET` ≠ the gateway's `WHATSAPP_WEBHOOK_SECRET` | make them equal |
| nothing in the api's logs | the gateway is not posting: wrong `WHATSAPP_WEBHOOK`, or logged out | check the gateway's logs and UI |
| worker logs `gowa.error` with `status_code: 401` | the device is logged out, or the basic-auth pair differs | check the UI; re-link (C4), or fix the pair |
| worker logs `gowa.unreachable` | the gateway is down, or not reachable at `GOWA__BASE_URL` | see C5 |
| outbox rows `failed` with `error_code: 463` | WhatsApp's reach-out timelock: too many new chats too fast | slow down; it lifts by itself |
| outbox rows `unknown` | sends timing out (504) | raise `GOWA__SEND_TIMEOUT_S`; never add retries (ADR 0003) |
| the phone shows the device gone, or the number is banned | WhatsApp unlinked or banned it | re-link; if banned, a new SIM; consider Meta (ADR 0004) |

To find one person's log lines without logging their number: `uv run po-gowa hash +316…`
with the target's `LOGGING__PII_PEPPER`.

## Re-linking

The session survives redeploys while the volume does. It ends if the phone unlinks the
device, if the phone stays offline for about two weeks, or if WhatsApp logs it out. To
re-link: open the gateway's UI (C4), log out the stale device if it is listed, and scan a
new QR code. No variable changes.

Then resolve the `gowa.unhealthy` issue in Sentry, so the next logout raises it again.
