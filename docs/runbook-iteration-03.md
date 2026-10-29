# Runbook — Iteration 03: tenants, onboarding and the Google connect

Iteration 03 is done when **an invited person writes to the bot, confirms their time zone
with a numbered reply, taps one link, connects Google Calendar and is told they are set up,
with their data under RLS from the first message**. Isolation and the choice parsing are
mechanised in the test suite (last section). What remains is outside the code: a Google
OAuth app, a Composio auth config, and one run through on staging from a real phone.

The code merges and deploys **with Composio switched off** (`COMPOSIO__ENABLED` unset or
`false`). In that state `/connect/*` is not mounted, no new variable is required, and invited
senders keep Iteration 02's acknowledgement. Only the migration runs. Nothing here has to
happen before merging.

> **Keep `COMPOSIO__ENABLED=false` on staging until the code that mounts `/connect/*` is
> deployed there** (the connect PR, `/connect/{token}` and `/connect/callback`). The worker
> sends onboarding links as soon as the flag is on, and a link to an api that does not serve
> it answers 404.

| Step | Needs | Proves |
|---|---|---|
| 1. The migration | the api's pre-deploy command | `app_definer`, the tenant tables and RLS on staging |
| 2. Google OAuth app | a Google Cloud project | a consent screen whose refresh tokens last |
| 3. Composio auth config | Composio's staging project | Composio connects with *our* Google client |
| 4. Variables | 1–3 | onboarding switched on |
| 5. End to end | the GOWA SIM linked; your phone | the Done-When |
| 6. Day 1 and day 8 | 5 | what the next iterations build on |

Staging only. Production stays paused until the first invitee (the ops checklist, below).

---

## 1. The migration: bootstrap first

> **`po-db bootstrap` must run before `alembic upgrade head`, on every database.** Migration
> 0004 hands its two `SECURITY DEFINER` functions (`resolve_tenant`, `create_tenant`) to the
> role `app_definer`, and only bootstrap creates that role and grants it to `app_owner`. Run
> the migration on a database last bootstrapped by an older checkout and it stops with
> `role "app_definer" does not exist`, or `must be member of role "app_definer"`.

**On Railway** this is already the order: the api's pre-deploy command is
`sh -c "po-db bootstrap && alembic upgrade head"` (`docs/runbook-iteration-01.md`, section 1).
Before merging, open staging's `personal-organizer` → Settings → Deploy and confirm it still
says exactly that, `sh -c` included. Merge, then read the pre-deploy log: `db.bootstrap.ok`,
then Alembic running `0003_channel_ledgers -> 0004_tenants`.

**Locally**, and anywhere you migrate by hand:

```sh
uv run po-db bootstrap        # creates app_definer (NOLOGIN, BYPASSRLS), grants it to app_owner
uv run alembic upgrade head   # 0004_tenants
uv run po-db check            # app_user is still correctly constrained
```

Bootstrap is idempotent. Run it again whenever you pull a branch that touches
`alembic/bootstrap.sql`.

**Check it on staging.** Run `railway connect Postgres --environment staging`, which connects
as the superuser and bypasses RLS, so read only:

```sql
SELECT rolname, rolcanlogin, rolbypassrls FROM pg_roles WHERE rolname = 'app_definer';
-- app_definer | f | t
SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
WHERE relname IN ('tenants', 'tenant_identities', 'calendar_connections',
                  'onboarding_links', 'messages');
-- five rows, t | t
```

## 2. The Google OAuth app: "In production", unverified

One Google Cloud project and one OAuth client. Staging's Composio project uses it now, and
production's later.

1. console.cloud.google.com → create a project (e.g. `personal-organizer`).
2. APIs & Services → Library → **Google Calendar API** → Enable.
3. Google Auth Platform → **Branding**: app name, your support email, developer contact. Add
   no logo: a logo triggers brand verification.
4. **Audience**: user type **External**, then **Publish app**. The publishing status must read
   **In production**. Do not submit it for verification.
5. **Data Access** → Add or remove scopes → `https://www.googleapis.com/auth/calendar`, and
   nothing else. It is a *sensitive* scope, not a restricted one, which is what makes an
   unverified production app possible.
6. **Clients** → Create client → **Web application**. Authorised redirect URI: Composio's
   callback, exactly as its auth config form shows it (step 3.2). At the time of writing that
   is `https://backend.composio.dev/api/v3/toolkits/auth/callback`. Keep the client id and
   secret for step 3.

**Why not "Testing":** in Testing, Google expires refresh tokens after about **7 days**, so
every user would be silently disconnected once a week. Testing also admits only listed test
users.

**What unverified production costs**, all acceptable for an invite-only circle:

- **Google's warning screen.** Users see "Google hasn't verified this app" and must tap
  **Advanced → Go to <app name> (unsafe)**. Tell invitees before they tap the link.
- **A 100-user cap** over the project's lifetime, not concurrently. That is twice the circle
  this is for. Verification and the cutover campaign were dropped for good on 2026-10-07.

## 3. Composio: a custom auth config with that client

Use Composio's staging project first. Production later gets its own project and its own auth
config with the same Google client.

1. platform.composio.dev → the **staging** project → Settings → API keys. That key is
   staging's `COMPOSIO__API_KEY`, and only staging's.
2. Auth Configs → Create auth config → **Google Calendar** → OAuth2 → **your own credentials**
   (not Composio's managed app):
   - the client id and secret from 2.6;
   - scopes: `https://www.googleapis.com/auth/calendar`. Remove any default scope that Data
     Access (2.5) does not list, or add it there too, so the consent screen asks for exactly
     what the app declares;
   - the **redirect URL** the form shows: put it in the Google client (2.6) if it differs.
3. Save. The auth config id (`ac_…`) is `COMPOSIO__CALENDAR_AUTH_CONFIG_ID`. The setting
   refuses anything else. A connected account id (`ca_…`) pasted by mistake is the usual
   culprit.

Every `calendar_connections` row records the auth config that made it, so changing the
setting later affects new connections only.

## 4. The variables

Set the full Iteration 03 set on staging's **api and worker** (`personal-organizer` and
`worker`), the same values on both. Set `COMPOSIO__ENABLED=true` last, once the code that
mounts `/connect/*` is deployed to staging.

| Variable | Services | Value | If wrong |
|---|---|---|---|
| `COMPOSIO__ENABLED` | api, worker | `true`. Unset or `false` is Iteration 02's behaviour | api: `/connect/*` answers 404. worker: invited senders keep "Got it — I'm not smart yet", because onboarding is gated on this flag |
| `COMPOSIO__API_KEY` | api (the worker only validates it) | the staging project's key (3.1) | boot fails, naming it, when missing. A key from another Composio project: the connect POST answers 503 and the api logs `connect.link_provider_rejected` |
| `COMPOSIO__CALENDAR_AUTH_CONFIG_ID` | api (the worker only validates it) | `ac_…` (3.3) | boot fails unless it starts with `ac_`. An id from a different project than the key: `connect.link_provider_rejected`, or on the callback `connect.callback_refused` with `reason: auth_config_mismatch` |
| `COMPOSIO__REQUEST_TIMEOUT_S` | api | leave unset (`15`) | too small: spurious `connect.*_provider_unavailable` (503 pages) |
| `APP__PUBLIC_BASE_URL` | api, worker | `https://<staging api domain>`: an origin, no path, no trailing route | boot fails with `must be an origin`, or `must be https:// when deployed`. A domain that is not the api's: links in WhatsApp lead nowhere. The wrong origin at Composio: the callback never reaches the api |
| `ONBOARDING__LINK_SECRET` | api, worker | at least 32 characters: `uv run python -c "import secrets; print(secrets.token_hex(32))"` | boot fails when missing, shorter than 32, or `.env.example`'s placeholder when deployed. **Different on api and worker**: every link, even a fresh one tapped at once, shows "This link has expired" (410, api logs `connect.page_refused`) and nothing else fails |
| `ONBOARDING__LINK_TTL_S` | api, worker | leave unset (`900`); 60–3600 | outside the range: boot fails. Shorter than a user takes to switch apps and sign in: links expire on the tap |
| `ONBOARDING__BOT_PHONE` | worker | the number invitees write to, E.164 — today the GOWA SIM | not E.164: boot fails. Unset: `po-admin invite` records the invite but prints no link |

- **`ONBOARDING__LINK_SECRET` must be identical on both services.** The worker signs and the
  api verifies. Use one Railway shared variable, or paste the same value twice. Rotating it
  invalidates every outstanding link and in-flight callback, which costs a user one fresh
  link.
- With `COMPOSIO__ENABLED=true`, a service refuses to boot unless the API key, the auth config
  id, `APP__PUBLIC_BASE_URL` and `ONBOARDING__LINK_SECRET` are all set, and the error
  (`Invalid Composio settings: … required when COMPOSIO__ENABLED is true`) names every missing
  one. That is why the full set goes on both services, even where one does not read a value.
- The staging domain is Railway's generated one; no product domain is needed. Like the
  webhook, it must target port **8080** (`docs/runbook-iteration-01.md`, the PORT trap).

Unchanged by this iteration and still needed (`docs/runbook-whatsapp-gateway.md` has the full
table):

| Variable | Services | If wrong |
|---|---|---|
| `WHATSAPP__ALLOWED_PHONES` | api, worker | the **fallback** invite list, for every channel — your own number at least; invite everyone else with `po-admin` (section "Inviting people"). A number not on it in the form it arrives is told "invite-only". Compare `po-gowa hash +<number>` with `sender_hash` on `inbound.handled`; the hash matches only with staging's `LOGGING__PII_PEPPER`, e.g. `railway run --service worker --environment staging uv run po-gowa hash +<number>` |
| `GOWA__ENABLED`, `GOWA__BASIC_AUTH_USER`, `GOWA__BASIC_AUTH_PASSWORD`, `GOWA__WEBHOOK_SECRET` | api, worker: all of them, same values | boot fails on any service where one is missing; a mismatch with the gateway is `gowa.unhealthy` with status 401, or `gowa.signature_rejected` |
| `GOWA__BASE_URL` | api, worker: same value (a deployed api fails boot on the default `http://localhost:3000`) | a malformed URL (an embedded space or a non-numeric port): `gowa.unhealthy`, `reason: bad_response`, `error_type: InvalidURL`. Plain `http://` to a host that is not `*.railway.internal` fails boot when deployed |
| `GOWA__DEVICE_ID` | api, worker | leave unset with one device. A non-Latin-1 character: `gowa.unhealthy`, `reason: bad_response`, `error_type: UnicodeEncodeError` |

## 5. The end to end, on staging

**Before you start:** the gateway's own UI shows the device logged in, or `po-gowa status`
run inside the staging worker (a laptop cannot reach `*.railway.internal` and would print
`unreachable` while the gateway is fine: `railway ssh --service worker --environment staging`,
then `po-gowa status`; check your Railway CLI's syntax) prints `connected=True logged_in=True`,
and Sentry has no open `gowa.unhealthy` issue. Your number is
on `WHATSAPP__ALLOWED_PHONES`, and you have not written to the bot since the migration (if
you have, see "Starting over" below). Read logs with the CLI, since the dashboard shows JSON
lines blank: `railway logs --service worker --environment staging --json`, and
`--service personal-organizer` for the api.

1. **Write "hi"** to the SIM's number. Expect "Hi! I'm your personal organizer. Two quick
   steps and you're set up." and a numbered choice: "Is your time zone Asia/Jerusalem?", `1.
   Correct`, `2. Change`. That is for a +972 number; a number with no zone guess is asked
   "Which city are you in?". In the worker log: `inbound.handled` with
   `disposition: onboarding`.
2. **Reply `1`.** Expect one message: "Your time zone is set to Asia/Jerusalem." and the link,
   `https://<staging>/connect/<token>`, on a line of its own. From a second invited phone, try
   the other path. Write the first message in Hebrew, then `2`, then a city (`London`, or
   `תל אביב`). That proves the Hebrew strings (D11), the city match, and that both paths reach
   the same link step. A reply that matches no zone logs `onboarding.zone_unmatched` and gets
   "I couldn't match that to a time zone".
3. **Do not tap yet.** WhatsApp has already fetched the link for its preview. The api log has
   an `http.request` with `method: GET` and `route: /connect/{token}`. The link must still
   work, because GET never consumes it (ADR 0005).
4. **Tap the link.** You get a page, "Connect Google Calendar", with one button, "Continue to
   Google". Tap it: the api answers 303 to Composio, which sends you to Google (api log:
   `connect.redirected`).
5. **At Google:** choose the account. At the unverified-app warning, tap **Advanced → Go to …
   (unsafe)**, then allow Calendar access.
6. **Back on our page** (`/connect/callback`), "Calendar connected" says you are connected
   (api log: `connect.connected`). Within seconds WhatsApp says "You're all set! Your Google
   Calendar is connected." In the worker log: the `onboarding:connected` job, then
   `outbox.accepted` with `channel: gowa`. **Write today's date down: the day-8 check counts
   from it.**
7. **Check the state** as the superuser (`railway connect Postgres --environment staging`):

   ```sql
   SELECT id, status, onboarding_step, language, timezone FROM tenants;
   SELECT tenant_id, network FROM tenant_identities;
   SELECT tenant_id, connected_account_id, auth_config_id, status FROM calendar_connections;
   SELECT tenant_id, used_at IS NOT NULL AS used FROM onboarding_links;
   SELECT tenant_id, direction, channel, message_type FROM messages;
   ```

   Expect:
   - the tenant `active`, with a NULL step and your zone;
   - a `whatsapp` identity (its `external_id` is your `tel:` key);
   - one `active` connection under the `ac_…` from step 3;
   - the link used;
   - your inbound messages in `messages`, with `channel_inbox.body` nulled for them.

   In Composio → Connected accounts, the account is **ACTIVE** and its user id is the
   tenant's UUID, never a phone number.
8. **Replay the link**: tap it again, or open it in another browser. The page says "Your
   calendar is already connected" (api log, info level: `connect.already_connected`; for a
   late callback, `connect.callback_already_connected`), and nothing changes. (A link spent but not followed by a
   connection says "This link has expired" instead.)
9. **Write again.** An active tenant gets the fixed acknowledgement (`disposition: allowed`)
   until the agent arrives.

### Starting over with the same phone

There is no tenant deletion yet. To run onboarding again from scratch, as the superuser:

```sql
DELETE FROM tenants WHERE id = '<uuid>';  -- identities, links, connections, messages cascade
```

Then delete the account in Composio → Connected accounts, so the next connect does not meet
the old one.

### Reconnecting one user

There is no reconnect flow yet (it arrives with calendar reads). To send one user a fresh
link, as the superuser:

```sql
UPDATE tenants SET status = 'onboarding', onboarding_step = 'connect' WHERE id = '<uuid>';
```

Their next message gets a new link. The new connection marks the old one `revoked`, but with
`allow_multiple` the old account stays ACTIVE at Composio (ADR 0005).

### Removing a repeating event

Ask Remi in chat ("delete the weekly class"); for someone with Google connected, it changes
Google too.

Only if chat can't do it, as the superuser (run the same `WHERE` as a `SELECT` first to see
what would go):

```sql
DELETE FROM events WHERE tenant_id = '<uuid>' AND source = 'remi' AND rule IS NOT NULL AND title = '<title>';
```

## 6. Day-1 and day-8 checks

None of these block the iteration. They decide later work, so write each answer down where
the next plan will read it.

### Day 1: the GOWA SIM (record in ADR 0004, Consequences)

1. **Buttons and lists.** Look in the gateway's API docs (its UI, or `docs/openapi.yaml` in
   the GOWA repository at the deployed tag, `v9.6.0`) for an endpoint that sends buttons or a
   list. If there is one, send each to an iPhone and to an Android phone and note what each
   shows. If there is none, that is the answer.
2. **Polls.** Send one to your own number (check the field names against the same API docs
   if this answers 400):

   ```sh
   curl -u "<user>:<password>" -X POST https://<gowa domain>/send/poll \
     -H 'content-type: application/json' \
     -d '{"phone": "<your number, digits only>@s.whatsapp.net", "question": "Test?", "options": ["One", "Two"], "max_answer": 1}'
   ```

   Does it render on both phones? Then vote. Read the api log: an `ingress.recorded` with
   `message_count: 1` means the vote arrived and our parser kept it; `skipped_count: 1`
   means it arrived and the parser dropped it; no line at all means GOWA did not forward it,
   or `WHATSAPP_WEBHOOK_EVENTS` filtered it out.
3. **When our webhook fails.** First read the answer in GOWA's source at `v9.6.0`: find
   where it posts webhooks and note its timeout, number of attempts and backoff. Then
   confirm with three short probes. In each, send one message from your phone during the
   failure, restore, and watch whether that message arrives later, how often, and for how
   long the gateway logs retries:
   - **a 4xx:** set a different `GOWA__WEBHOOK_SECRET` on the api only, and redeploy it (we
     answer 401, `gowa.signature_rejected`);
   - **a timeout:** set the gateway's `WHATSAPP_WEBHOOK` to
     `http://10.255.255.1/webhooks/gowa`, which is unroutable, so every attempt times out.
     The volume keeps the session across the gateway's redeploy;
   - **a 5xx:** `personal-organizer` → ⋮ → **Restart**, and send while it restarts (Railway's
     edge answers 502). If the restart is too quick to catch, the timeout probe stands in.

   A message lost on a 4xx matters most: unlike Meta, which redelivers for seven days, a
   secret mismatch may then drop messages for good.
4. **LID-only addressing.** After step 5.9, keep chatting from the same phone for a day or
   two. WhatsApp may switch a chat to LID-only addressing, where the webhook carries no phone
   number. Our parser then keys the sender on the LID, which no tenant has, so the sender
   looks like a stranger: `inbound.handled` with `disposition: stranger` (or
   `stranger_muted`) for a person who is onboarded. Record whether it happens, and after how
   long. If it does, the bot cannot recognise that person
   until a later change decides what to do (for example, link the LID as a second identity).

Record the results as one bullet at the end of ADR 0004's Consequences, in this shape:

```markdown
- **Day-1 checks (<date>, GOWA v9.6.0).** Buttons: … Lists: … Polls: render on iOS …, on
  Android …; votes … Webhook failures: on 401 …; on a timeout …; on a 502 … (attempts,
  interval, how long before it gives up). LID-only addressing: …
```

### Day 1: Composio with our own client (record in ADR 0005, "Day-1 results")

- **The warning screen.** Did the connect pass Google's unverified-app screen and return
  `ACTIVE`? Step 5 of the end to end answers this.
- **Google sign-in inside WhatsApp's in-app browser.** At step 4, open the link from the
  chat on an iPhone and on an Android phone and let the in-app browser carry you through
  Google. Google refuses OAuth in some embedded browsers (`403: disallowed_useragent`). Note
  which phone and which browser worked, whether you had to "Open in browser", and what the
  page does when the return lands in a different browser than the one that started
  (the state binds the tenant, not the browser, by design).
- **The callback's query parameters.** At step 6, note the parameter *names* in the browser's
  address bar, not their values: `connected_account_id` or `connectedAccountId`. The code
  accepts both; record which arrives.
- **`?state=` round trip.** In the same address bar, confirm `state` is present. If it is
  not, the api logs `connect.callback_refused` with `reason: bad_state` and the page says
  "Calendar not connected".
- **A reconnect.** Use "Reconnecting one user" above with the same Google account. Does
  Composio return a second `ACTIVE` account (the old row becomes `revoked`), or refuse with a
  multiple-accounts error? If it refuses, the old account has to be deleted in Composio
  first; record which.
- **The SDK's own output.** During the end to end, look for any log line from a `composio`
  logger at INFO or above that carries more than ids, and check whether the SDK sends
  telemetry (the connector switches it off per call). Either is a leak to fix before inviting
  anyone.
- **What Sentry attached.** Sentry's default integrations attach request URLs to events and
  traces. A bad `/connect/callback?state=x` creates no Sentry *event* (`bad_state` is a log
  warning only, and logging events are off), so look under Traces (Performance) for a
  `/connect/callback` transaction: request it about ten times, or temporarily raise
  `SENTRY__TRACES_SAMPLE_RATE` (default `0.1`) on the api. Open one and check the URL data:
  `/connect/<token>` and `state=` must read as scrubbed. A `ca_…` account id may appear in
  the URL; it is not personal data.
- **Hebrew on a real phone.** In the Hebrew run (step 5.2), read the onboarding texts and the
  connect pages on the phone: the digits, the zone name (`Asia/Jerusalem`) and the URL sit
  inside right-to-left text, where bidirectional rendering can reorder them. Note anything
  that reads wrongly. The link must still be tappable.

### Day 8: the refresh token survived

Eight days after step 5.6 (Testing mode's expiry is about seven), confirm that the connection
still works. The app has no calendar reads yet, so ask Composio directly, with staging's key
and the connection's `connected_account_id` from step 5.7:

```sh
COMPOSIO_API_KEY=<staging key> uv run python - <<'PY'
from composio import Composio

result = Composio().tools.execute(
    "GOOGLECALENDAR_LIST_CALENDARS",
    {},
    connected_account_id="ca_...",
    dangerously_skip_version_check=True,
)
print(result["successful"], result["error"])
PY
```

Expect `True None`. The dashboard does the same: Connected accounts → the account shows
**ACTIVE** → run a read-only Calendar tool on it.

An auth error, or an account that is no longer ACTIVE, means the refresh token died. Either
the app was still in Testing when the user connected, or the user revoked access in their
Google account. Publish the app (2.4), then reconnect them (section 5). Record the result in
the ops checklist below.

## Inviting people

Invites live in the database (ADR 0006); no deploy is involved. Run `po-admin` inside the
worker, where the private database host resolves:

```sh
railway ssh --service worker --environment staging
po-admin invite +972501234567 --note "Mom"   # prints https://wa.me/<bot number>
po-admin list                                # newest first: open / used / revoked, member status
po-admin revoke +972501234567                # an invite nobody has used yet
po-admin suspend +972501234567               # a member: their messages get the invite-only line
po-admin unsuspend +972501234567             # back to active, or onboarding if they never finished
```

Forward the link yourself. The invitee taps it and writes anything; their first words pick
the language. `po-admin list` shows `used` once their first message is in.

- `already a member (…)`: they have a tenant; an invite would never be read.
- `invite already used; use suspend`: revoke is for invites, suspend for members.
- `on WHATSAPP__ALLOWED_PHONES; remove it there first`: env-listed numbers cannot be
  suspended, so you cannot lock yourself out.
- `drop the 0 after the country code`: `+972 050-…` or `+44 (0)20 …` would be stored as a
  number WhatsApp never delivers; type `+972 50-…`.
- **With `COMPOSIO__ENABLED=false`** an invitee is served (the fixed acknowledgement) but never
  gets a tenant: `list` keeps showing `open`, and `suspend` answers `not a member yet (open
  invite); use revoke`. Revoke is the cut-off there. `used`, `suspend` and `unsuspend` need
  Composio on. Switching Composio off later turns members whose invite is used into
  strangers until it is back on; numbers on `WHATSAPP__ALLOWED_PHONES` keep being served.
- `po-admin` prints numbers to your terminal only. Its log lines carry `sender_hash` and
  `tenant_id`, matching `inbound.handled`.

If `railway ssh` is unavailable, `railway run --service worker --environment staging uv run
po-admin list` works only from a machine that can reach the database host.

## When it does not work

### Deploying

| Symptom | Cause | Fix |
|---|---|---|
| pre-deploy fails: `role "app_definer" does not exist` | the migration ran without bootstrap first | restore the pre-deploy command to `sh -c "po-db bootstrap && alembic upgrade head"`; by hand, run bootstrap, then upgrade |
| pre-deploy fails: `must be member of role "app_definer"` | bootstrap ran from an older checkout, so `GRANT app_definer TO app_owner` is missing | run this branch's `po-db bootstrap`, then migrate |
| boot fails: `Invalid Composio settings: … required when COMPOSIO__ENABLED is true` | one of the four is missing on that service | set the full set on both services (section 4) |
| boot fails: `APP__PUBLIC_BASE_URL must be an origin …` or `… must be https:// when deployed` | a path, a trailing route, or `http://` | the bare `https://` origin |
| boot fails: `ONBOARDING__LINK_SECRET must be set to a real secret when deployed` | `.env.example`'s placeholder | generate one (section 4) |
| boot fails: `ONBOARDING__LINK_SECRET must be at least 32 characters` | a short secret | `token_hex(32)` (section 4) |
| boot fails: `COMPOSIO__CALENDAR_AUTH_CONFIG_ID must be an auth config id (ac_...)` | a `ca_…` or a toolkit slug | the `ac_…` id from 3.3 |

### Onboarding

| Symptom | Cause | Fix |
|---|---|---|
| an invited person still gets "Got it — I'm not smart yet" on their first message | `COMPOSIO__ENABLED` is not `true` on the **worker**: onboarding is gated on it | set it there too, with the full set |
| an invited person gets the invite-only line | their number is not on the list in the form it arrives | compare `po-gowa hash +<number>` with `sender_hash` on `inbound.handled`, hashing with staging's `LOGGING__PII_PEPPER` (`railway run --service worker --environment staging uv run po-gowa hash +<number>`) |
| an onboarded person gets the invite-only line | the chat moved to LID-only addressing; no tenant has that key | section 6, day-1 LID check |
| no reply, worker logs `onboarding.unaddressable` | the sender has no phone on the message and the tenant has none on file | nothing to send to; the sender must write from a number we know |
| worker logs `tenant.identity_conflict` | a sender key already belongs to another tenant; it is skipped, never reassigned | find the two tenants (`tenant_identities`); delete the stray one (Starting over) |
| no reply at all | the gateway | Sentry `gowa.unhealthy`; `docs/runbook-whatsapp-gateway.md`, "When replies stop" |
| every link, even a fresh one tapped at once, shows "This link has expired" (410; api logs `connect.page_refused`) | `ONBOARDING__LINK_SECRET` differs between api and worker | make them equal and redeploy both; then any message gets a fresh link |
| the link page says "This link has expired" on the first tap (410; api logs `connect.page_refused` or `connect.link_refused`) | older than `ONBOARDING__LINK_TTL_S` (15 min), tapped before (in another browser, or by someone it was forwarded to), or `ONBOARDING__LINK_SECRET` differs between api and worker (then every link does this) | write any message to the bot: it re-sends the latest usable link, or a fresh one |
| api answers 404 on `/connect/…` | `COMPOSIO__ENABLED` is not `true` on the api, or the api predates the connect code | set it, or deploy |

### Google and Composio

| Symptom | Cause | Fix |
|---|---|---|
| Google: `Error 400: redirect_uri_mismatch` | the Google client lacks Composio's callback URL | add it exactly as the auth config shows it (2.6) |
| Google: "Access blocked: … has not completed the Google verification process" | the app is still in **Testing**, and the user is not a test user | publish it (2.4) |
| Google: "This app is blocked" | a restricted scope was requested | Calendar only (2.5, 3.2) |
| Google: "Google hasn't verified this app" | expected | Advanced → Go to … (unsafe) |
| Google: `403: disallowed_useragent` | the sign-in opened in an embedded browser Google refuses | open the link in the phone's own browser; record it (section 6) |
| the connect POST answers 503, api logs `connect.link_provider_rejected` (also a Sentry event) | our configuration: the API key or auth config id is wrong, or from different Composio projects | fix them (section 4) |
| the connect POST answers 503, api logs `connect.link_provider_unavailable` | Composio is down or slow | retry; the link was spent, so write to the bot for a new one |
| the callback page says "Calendar not connected" (400), api logs `connect.callback_refused` with `reason: bad_state`, `expired_state` or `bad_account_id` | no or tampered `state` (a stripped query), a `state` older than an hour, or an account id of the wrong shape | a fresh link; if it recurs at once, the `?state=` round trip is broken (section 6) |
| same page, `reason: tenant_unavailable` | the tenant was deleted ("Starting over") or is in an unexpected status while the callback lands | send the bot a fresh message |
| same page, `reason: user_mismatch`, `auth_config_mismatch` or `not_active` | the account is not this tenant's, was made under another auth config (the API key and the config id from different Composio projects), or is not `ACTIVE` | the same Composio project for both; then a fresh link |
| "Not connected yet", api logs `reason: not_ready` | Composio still has the account INITIATED/INITIALIZING | reload in a few seconds |
| the callback answers 400 and api logs `connect.callback_provider_rejected` | Composio does not know that account id (a guess or a stale one) | nothing, unless it hits a real user: a fresh link |
| the callback answers 503 and api logs `connect.callback_provider_unavailable` or `connect.bind_race` | Composio down, or two accounts bound at once | reload the page |
| the callback answers 409, api logs `connect.account_conflict` (and a Sentry issue by that name) | the account is already bound to another tenant; it should be unreachable | a person must look: `calendar_connections.connected_account_id` for that account |
| connected (200), but no "You're all set", and api logs `connect.defer_failed` | the job could not be queued; the tenant is connected anyway | write any message to the bot; the connection is already active. Check the api's Postgres reachability |
| connected, but no "You're all set", and no `connect.defer_failed` | the `onboarding:connected` job (queue `webhooks`) has not run or the send failed | the worker log for that job and its `outbox.*` line; the worker's `WORKER__QUEUES` must include `webhooks` (the default does). `onboarding.all_set_skipped` with `reason: not_active`, `no_channel`, `no_phone` or `channel_not_configured` says what it had nowhere to send to |
| an unexpected 500 page on a `/connect/*` route, api logs `connect.failed` | a bug; the page asks for a new link | the Sentry event for the exception |
| the calendar stops working about a week after connecting | the app was in Testing when the user connected | publish, then "Reconnecting one user" |

### The gateway alarm

| Symptom | Cause | Fix |
|---|---|---|
| Sentry issue `gowa.unhealthy` | the gateway cannot carry messages; the `reason` tag says why | `docs/runbook-whatsapp-gateway.md`, "When replies stop"; then resolve the issue |
| `gowa.unhealthy` with `reason: bad_response`, `error_type` `InvalidURL` or `UnicodeEncodeError`, and no `status_code` | the worker never reached the gateway: the request was malformed before it was sent | check `GOWA__BASE_URL` (an embedded space or a non-numeric port) and `GOWA__DEVICE_ID` (a non-Latin-1 character) on the worker |
| Sentry quota: the issue fires once per 5 minutes while unhealthy (288 events a day) | the gateway is parked or down for a while | set `GOWA__ENABLED=false` on the worker (and api) instead of leaving the issue firing |
| worker logs `Task was not found` for `system:gowa_health`, once | GOWA was switched off on the worker while a check was queued | nothing: it is registered only with GOWA on |

## Ops checklist

- [ ] Pause the production services (`personal-organizer`, `worker`) until the first
      invitee: each deployment's ⋮ → **Remove**. Keep Postgres. Run on staging only. The
      next promotion to `production` deploys them again.
- [ ] When production comes back: **Postgres backups enabled before any real data.**
- [ ] Google OAuth app published "In production" (section 2). **Day 8 after connecting:
      confirm the calendar still works** (section 6), which proves the refresh token
      survived. Result: ____
- [ ] Day-1 GOWA checks (section 6), recorded in ADR 0004's Consequences.
- [ ] Day-1 Composio checks (section 6), recorded in ADR 0005's Day-1 results.
- [ ] The gateway alarm proved on staging (`docs/runbook-whatsapp-gateway.md`, Phase C
      step 8), and Sentry alerts on a new issue and on a regression.
- [ ] Before the agent iteration: read Anthropic's model deprecations page and the current
      model ids. v7's date for Haiku 4.5 is Oct 15, 2026, and `MODELS__FALLBACK_MODEL` is
      `claude-haiku-4-5` in both environments.

## Where the Done-When is proved

| Criterion | Where |
|---|---|
| An invited number onboards from WhatsApp and connects Google Calendar | section 5, on staging; and `tests/db/test_onboarding.py`, `tests/api/test_connect.py` with a fake Composio |
| Isolation holds with app-level tenant filters disabled | `tests/db/test_isolation.py` (`-m rls`), parametrised to run every case twice in one run: with the repositories' `scoped()` filters, and with `scoped()` replaced by a no-op |
| A choice works on every channel | `tests/unit/test_choices.py`: numbered, label and alias replies, Meta button `reply_id`, GOWA `selection.selected_id` |
| A logged-out gateway is never silent | `tests/worker/test_gowa_health.py`; on staging, `docs/runbook-whatsapp-gateway.md` Phase C step 8 |

## Deliberately not in this iteration

- Calendar reads, Composio usage metering, and reconnecting on auth errors: the
  calendar-read iteration. It must name the bound `connected_account_id` on every call and
  disable or delete superseded Composio accounts (ADR 0005).
- `pending_actions` and `scheduled_jobs`; token and message budgets.
- Native buttons and polls (`send_choice`): chosen by the day-1 checks.
- Tenant deletion, data export, and DB-backed invites: before the circle grows past a
  handful of people.
