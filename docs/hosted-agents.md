# Backend-hosted agents

Signal and WhatsApp are transports for the same LANIS agent. AI requests use the
existing central `ai_endpoint`, `ai_api_key`, and `ai_default_model` configuration.
The endpoint must be an HTTPS OpenAI-compatible `/chat/completions` URL. The
existing provider privacy settings (no data collection and ZDR) remain in effect.
No user API keys or Schulportal passwords are sent to messaging transports or AI.

This feature builds on the existing Agents/MCP implementation on `origin/main`.
Deploy the matching API and UI together.

## Connect and manage

In **Settings → Agents → Add**, choose Signal or WhatsApp, review AI processing
and retention, and choose Restricted, Full, or Custom access. Send the displayed
`LANIS XXXX-XXXX` message to the bot number within ten minutes. Codes are hashed,
single-use, channel-specific, and bound to the signed-in account. A sender already
connected to a different account cannot claim another account's code. Disconnect
it first. Re-pairing revokes the old grant and deletes its schedules.

Both channels appear in the normal Agents connection list. Rename them, change
permissions, manage schedules, or disconnect there. **STOP** immediately revokes
the channel and removes its pending changes, conversation, and schedules. Active
turns recheck their permissions before tool calls, AI continuations, persistence,
and sending replies. Permission edits clear conversation history and pending
changes. Ordinary school writes require a short-lived explicit chat confirmation;
the code is consumed before execution, so failed/ambiguous writes are not replayed.

## WhatsApp migration

Keep the existing Meta Cloud API configuration and signed `/whatsapp/webhook`.
Its queue, message deduplication and immediate STOP handling remain. All message
turns now enter the shared harness. The former `/whatsapp/pairing` endpoint returns
410 and the old WhatsApp settings page links to Agents.

Existing users must reconnect under Agents to approve the new MCP permission
model; there is no automatic expansion of their old consent. After pairing, the
legacy link, pairing code, transcript and pending actions are removed. Until then,
ordinary messages explain how to reconnect. Old pending action codes cannot run.

WhatsApp free-form outbound messages require an open 24-hour customer service
window. A scheduled task outside this window records `delivery_window_closed`;
it does not invent or send a template. Signal can deliver outside this window.
See the [Meta messaging documentation](https://whatsappbusiness.com/policy/?faq=5).

## Signal bridge

LANIS includes an optional [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)
service in its Docker Compose deployment. Add the following to the backend's
existing `.env` (alongside `JWT_SECRET` and the central AI configuration):

```dotenv
COMPOSE_PROFILES=signal
SIGNAL_BOT_NUMBER=+491234567890
# Docker uses http://signal-bridge:8080 automatically; omit SIGNAL_BRIDGE_URL.
# Optional local provisioning port and pinned image override:
# SIGNAL_BRIDGE_PORT=8081
# SIGNAL_BRIDGE_IMAGE=bbernhard/signal-cli-rest-api:0.101
```

Then run the normal `docker compose up -d --build` or the existing Docker deploy
script. Compose reads `COMPOSE_PROFILES` from `.env`, starts the bundled bridge
in JSON-RPC mode, and connects LANIS over the private Compose network. Signal's
account credentials and cryptographic keys persist in the `signal-data` volume.
They are created by Signal registration/linking, rather than pasted into `.env`.

For initial linking, open
`http://127.0.0.1:8081/v1/qrcodelink?device_name=LANIS` on the backend host, then
scan the QR code in the bot account's Signal app under **Settings → Linked devices**.
Use an SSH tunnel to that loopback port if administering remotely. Set
`SIGNAL_BOT_NUMBER` to this account's international number. Alternatively follow
the bridge's registration/verification instructions for a dedicated bot account.
Do not share the QR code or copy its account volume to external services.

When the API runs directly as a user service, the same bundled bridge can be
started with `docker compose --profile signal up -d signal-bridge`; the API's
Signal client defaults to `http://127.0.0.1:8081`. For an existing bridge or a
separate host, configure its URL instead:

```dotenv
SIGNAL_BRIDGE_URL=https://your-private-signal-bridge.example
SIGNAL_BOT_NUMBER=+491234567890
# Optional bearer token for an authenticated reverse proxy:
SIGNAL_BRIDGE_TOKEN=replace-locally-if-required
```

Leave `SIGNAL_BOT_NUMBER` unset to disable the Signal receiver. Leave
`COMPOSE_PROFILES` unset to avoid starting the bundled bridge. No production
service is started or reconfigured by this change itself.

Keep the bridge private: loopback or a protected internal network; use HTTPS and
an authenticated gateway across hosts. This backend does not expose a public
Signal webhook. It subscribes to `/v1/receive/{number}` over WebSocket and sends
through `/v2/send`. The account and encryption keys belong to the bridge. Incoming
nonempty direct text messages are accepted; groups, attachments, receipts and
sent-message syncs are ignored. The receiver reconnects after transport failure.
Eight concurrent Signal message tasks are allowed; STOP bypasses busy turns.
Only one receiver should own a bridge account. Multiple API workers deduplicate
received events, but do not provide a distributed Signal subscription coordinator.

## Schedules and execution

Ask in chat (e.g. “Summarize my homework weekdays at 07:00”) or create a five-field
cron task under the connection's settings. The chat tool and API also accept
one-time tasks with `at`, an ISO datetime containing a UTC offset. Cron tasks use
an IANA timezone, default `Europe/Berlin`; croniter receives timezone-aware dates
for daylight saving handling. `cron` and `at` are mutually exclusive. Recurring
intervals shorter than five minutes are rejected. Each connection has at most
20 stored tasks, including completed one-time tasks until deleted.

Jobs persist encrypted in the authentication SQLite database. The lifespan worker
claims due tasks transactionally with five-minute leases, so multiple API workers
cannot normally execute the same occurrence simultaneously. Restarted workers
recover expired claims; missed occurrences are coalesced into one run, followed
by the next future cron occurrence. Recovery is **at least once**: a crash between
transport delivery and recording its outcome can deliver a duplicate. Scheduled
runs expose read-only MCP tools and cannot change school data or create schedules.
A busy connection skips that occurrence with status `busy`. Execution or delivery
failures record `failed` and advance to the next recurrence; one-time tasks finish.
There is no retry of school writes, shell execution, arbitrary HTTP tool or
unrestricted filesystem access.

Each turn has a 180-second AI timeout and retains the engine's tool-call, response,
round and context limits. Per-sender and per-grant rate limits bound usage.
Conversation history retains at most 12 text messages for 24 hours, encrypted
using the existing credential key. Recipients, scheduled prompts, and pending
changes are encrypted too. Run metadata (no prompts or model reasoning) is kept
for 30 days. Pending pairing/action codes expire after ten minutes. Account export
includes history, schedules and run metadata; account deletion removes hosted
agent rows. The credential encryption key must remain stable across restarts.

## APIs

All management endpoints require the normal owner session header:

- `POST /agents/channels/{signal|whatsapp}/pairing`: name, mode, scopes.
- `GET /agents`: channel availability and the existing grant list.
- `GET|POST /agents/{grant_id}/schedules`: list or create tasks.
- `DELETE /agents/{grant_id}/schedules/{schedule_id}`: cancel a task.
- `GET /agents/{grant_id}/runs`: last 30 run records.
- Existing grant PATCH/DELETE endpoints manage permissions and disconnection.

A delivery status is exposed on each schedule. There is no independent daemon or
production service change required beyond restarting the API after deployment.
Configure the bridge and central AI locally before deployment; do not put secrets
in repository files. Offline tests cover persistence, scopes, confirmation,
revocation and transports; real Signal and Meta credentials are needed for an
end-to-end messaging check.
