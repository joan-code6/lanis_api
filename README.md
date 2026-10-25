# LANiS (Schulportal Hessen) API

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/sph-client)](https://pypi.org/project/sph-client/)

Unofficial Python client and REST API integrations for Schulportal Hessen (SPH).
The PyPI package contains the reusable Python client; the repository also includes
the optional hosted API server and a terminal UI.

The client is actively developed against the portal's web interfaces. Portal
changes can occasionally require a new package release.

## Live Server
The hosted API is available at
[lanis-backend.joancode.dev](https://lanis-backend.joancode.dev/), with interactive
documentation at [/documentation](https://lanis-backend.joancode.dev/documentation).
<img width="1249" height="937" alt="image" src="https://github.com/user-attachments/assets/8fef241f-3cd5-432b-bc01-5f85b8d6efab" />


## Components

This monorepo contains the following components:

1. **sph_client** / **schulportal_hessen** contains the reusable Python client
2. **api** wraps the client in a REST API with caching and additional services
3. **TUI** contains a terminal interface that is currently not actively maintained

## Supported Modules:

The portal modules are referred to as applets because SPH is built on top of Moodle.


- `login` — authenticate and manage session credentials
- `benutzer` — user profile and class information
- `mein_unterricht` — courses, content, assignments, and attachments
- `kalender` — calendar events
- `nachrichten` — conversations, recipients, sending, and replies
- `stundenplan` — timetable data
- `lerngruppen` — study groups
- `oberstufenwahl` — student-facing elections and course choices
- `videokonferenz` — current student video rooms and safe join links
- `dateispeicher` — school file storage and downloads
- `dateiverteilung` — targeted notices, personal files, and authenticated downloads
- `school_list` — school names and IDs for login and school selection


## Installation

### API Server

```bash
# Install from source
git clone https://github.com/joan-code6/lanis_api.git
pip install -r requirements.txt
```

### Python package
```bash
python -m pip install sph-client
```

## Quick Start

### API Server

Create a `.env` file with a stable, private signing key before the first start:

```dotenv
JWT_SECRET=replace-this-with-a-long-random-value

# Optional admin observability portal. List normalized identities, never passwords.
LANIS_ADMIN_ACCOUNTS=5201:admin.one,1234:admin.two
LANIS_ADMIN_JWT_SECRET=replace-with-a-separate-random-value
LANIS_ADMIN_ORIGIN=https://admin.lanis.arg-server.de

# Authenticated checks sample onboarded registered users active in the past 30 days.
LANIS_UPTIME_INTERVAL_SECONDS=300
LANIS_UPTIME_TIMEOUT_SECONDS=15
# Optional Discord transition alerts. Keep the webhook URL secret.
LANIS_UPTIME_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/replace-me
# Optional new-user notifications. Falls back to the uptime webhook above.
LANIS_NEW_USER_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/replace-me
# Feedback notifications prefer this dedicated webhook, then fall back to the
# new-user webhook or uptime webhook if those are configured.
LANIS_FEEDBACK_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/replace-me
```

Keep that value unchanged across deployments; changing it invalidates issued
access tokens. The server refuses to start without it instead of silently using
an ephemeral key.

Admin access is checked against `LANIS_ADMIN_ACCOUNTS` and then authenticated
with the same Schulportal credentials. Admin credentials are not stored in the
configuration file. The observability API is under `/admin/*`; the legacy
`/metrics/stats` endpoint is retained only as a protected, deprecated alias.

The backend checks Schulportal every five minutes by default using two randomly
selected registered users who completed onboarding and were active within the
past 30 days. On failure, one confirmation round probes the same pair plus up
to two additional eligible users after 15 seconds. Background probes do not
update user activity. Incidents require two healthy scheduled cycles to recover.
The private admin portal exposes stored rounds, confidence, scope, and 90-day
availability summaries at `GET /admin/uptime`; the public status projection
contains only aggregate information.

Before logging in, each probe checks the start page for the Schulportal maintenance
heading/title, including maintenance pages returned with HTTP 200. Detected
maintenance is recorded as `down` with error code `maintenance`; login and module
requests are skipped. The aggregate cycle preserves that reason when all
confirmation probes report maintenance. Normal authenticated checks resume when
the maintenance page disappears; the existing two-cycle recovery rule still
applies. A failed start-page request falls back to authenticated checks. The
announced end time is not yet extracted or treated as proof of recovery.

Configure `LANIS_UPTIME_DISCORD_WEBHOOK_URL` to
receive one alert when the service becomes unavailable and one recovery message
when it becomes operational again; repeated checks during the same incident are
suppressed, and each transition links to the status page. Delivery outcomes are
retained in the admin view. The new-user webhook receives one message after a first successful
login; it can be separate from the uptime webhook with
`LANIS_NEW_USER_DISCORD_WEBHOOK_URL`. An immediate check can be triggered with
`POST /admin/uptime/check`.

Signed-in users can submit feature ideas, bug reports, and general feedback at
`POST /feedback`. Reports are stored in the separate `data/feedback.db` SQLite
database and can be reviewed or marked done in the admin portal at
`GET /admin/feedback` and `PATCH /admin/feedback/{report_id}`. Configure
`LANIS_FEEDBACK_DISCORD_WEBHOOK_URL` to post new submissions to a dedicated
Discord channel. If it is unset, the new-user webhook (or uptime webhook) is
used when configured. Discord delivery failures do not discard saved reports.

```bash
uvicorn api.api:app
```

The API is available at port `8000` with interactive docs at `/docs`.

### Python package


```python
from sph_client import SchulportalHessenAPI

api = SchulportalHessenAPI()

# Login
result = api.login("1234", "username", "password")
if result.get("success"):
    # Get available modules
    modules = api.get_available_modules()
    print(modules)

    # Fetch messages
    headers = api.nachrichten_get_headers()
    print(headers)

    # Get calendar events
    events = api.kalender_get_events()
    print(events)

    api.logout()
```
## Caching and sessions

The hosted API uses caching and persistent sessions to reduce portal requests and
keep navigation responsive.

- **Session TTL:** 1 hour inactivity timeout per session
- **Response cache:** 10 minutes for most endpoints
- **Always fresh:** `/videokonferenz` is never response-cached because room links may be short-lived and user-bound
- **Long cache (30 days):** `/modules`, `/apps`, `/benutzer`
- **School list cache:** 2 days with 3-day auto-refresh
- **File cache:** SHA-256 hashed, stored in `data/files/`
- **Background revalidation:** stale entries are refreshed asynchronously

### Cached access during Schulportal outages

Successful authenticated JSON reads are also retained in a private, in-memory
snapshot cache for up to 24 hours. If a later request observes a Schulportal
timeout, connection failure, or HTTP 5xx response, the API can return the last
snapshot for the same account, route, path parameters, and query parameters.
Authentication, TLS, and permission failures are never hidden by a snapshot.

Fallback responses include `X-LANIS-Cache: stale` and the original UTC fetch
time in `X-LANIS-Fetched-At`. Live responses use `fresh` or `hit`. The cache is
bounded to 100 entries per account, 2 MiB per response, and 64 MiB total. It is
cleared on logout and invalidated with the affected source data after writes.
Snapshots are held in process memory, so a backend restart clears them.

Authenticated clients can read `GET /cache/status` without contacting
Schulportal. It returns whether snapshots exist, their count, the most recent
successful fetch time, and the retention period. This timestamp describes the
latest successful cached read; it is not a complete account synchronization.

## Push notifications

Authenticated users can opt in to daytime polling for new messages and Vertretungsplan entries from the Lanis UI settings. The default polling window is 07:00–21:00 in the user's configured timezone, with a 15-minute interval. Each category creates its own baseline before sending notifications, so enabling it does not alert on existing content. Vertretungsplan notifications default to the user's own class and can instead target selected classes or the full plan.

Web Push requires these environment variables on the API server:

- `VAPID_PUBLIC_KEY`
- `VAPID_PRIVATE_KEY`
- `VAPID_SUBJECT` (for example, `mailto:admin@example.org`)

The `pywebpush` dependency is included in `requirements.txt`. Users can configure notification categories, Vertretungsplan class scope, the active window, interval, timezone, and whether message previews contain the sender and subject in **Settings → Benachrichtigungen**.

Subscriptions, category baselines and pending delivery retries live in `data/auth.db`. Normal process restarts preserve them and restart the polling worker. Keep that directory persistent (the supplied Docker Compose file mounts a named volume), use a stable working directory for a service installation, and preserve the `JWT_SECRET`/credential encryption key and VAPID key pair across deployments. Replacing these files or keys is not an ordinary restart.

An upgrade introducing per-login session ownership intentionally removes legacy subscriptions that cannot be safely attributed to a login. The browser must register its existing subscription again; updated frontends do this on authenticated page visits and periodically while visible. Do not retain unowned endpoints or attach them arbitrarily to another session to bypass that migration.

Push-service acceptance is not a device delivery receipt. Messages are retained by the push service for up to 24 hours while a device is unavailable; time-sensitive plan updates for one hour, and test pushes for ten minutes. The library's default is zero seconds, which discards offline messages. Only explicit push-service 404/410 responses expire a stored endpoint. Temporary network, signing or server configuration failures preserve subscriptions for retry.

Polling remains subject to each account's active time window, interval, unread-message status, class scope and live login session. A newly enabled category first records a baseline. A successful test push does not test these automatic polling conditions. Real push delivery and deployment configuration must be checked separately; regression tests use temporary databases and an isolated simulated push service.

## WhatsApp assistant

The hosted API can optionally expose a WhatsApp assistant. Users link
their WhatsApp account from **Settings → WhatsApp-Assistent** with a ten-minute,
single-use code. The code is sent through WhatsApp, so Schulportal credentials
never pass through Meta. Message previews are disabled by default, `STOP`
immediately removes the link, and duplicate webhook deliveries are ignored.

When AI is configured, the assistant uses an OpenRouter-compatible iterative
tool loop. It can reason between tool calls and access the same account-scoped
school areas as the LANIS UI, including timetable, substitutions, courses,
attendance, homework, exams, calendar, messages, files, elections, preferences,
and semantic search. Changes are never executed from a model tool call alone:
the backend creates an encrypted, short-lived proposal that the user must
confirm explicitly in WhatsApp. Recent conversation messages are encrypted,
limited to twelve messages, expire after 24 hours, and are deleted on unlink.
Replying to or marking an existing message as read requires message previews to
be enabled so the assistant can verify the exact conversation target.

Configure the AI provider with exactly these three values. `ai_endpoint` must
be a complete HTTPS Chat Completions URL:

```dotenv
ai_endpoint=https://ai.hackclub.com/proxy/v1/chat/completions
ai_api_key=replace-with-a-development-or-production-key
ai_default_model=openai/gpt-5.6-luna
```

If AI is unavailable, known basic information requests fall back to the
deterministic assistant. API keys, raw prompts, reasoning details, tool results,
and message contents are not logged.

Configure a Meta WhatsApp Business Platform / Cloud API application with:

```dotenv
WHATSAPP_ACCESS_TOKEN=replace-with-a-permanent-system-user-token
WHATSAPP_PHONE_NUMBER_ID=replace-with-the-cloud-api-phone-number-id
WHATSAPP_VERIFY_TOKEN=replace-with-a-random-webhook-verification-token
WHATSAPP_APP_SECRET=replace-with-the-meta-app-secret
# Keep this explicit so Graph API upgrades are intentional.
WHATSAPP_GRAPH_API_VERSION=vXX.X
# Digits only or international notation; formatting characters are removed.
WHATSAPP_PUBLIC_NUMBER=491234567890
LANIS_UI_BASE_URL=https://lanis.arg-server.de
```

In the Meta application, set the callback URL to
`https://<api-host>/whatsapp/webhook` and subscribe the WhatsApp Business Account
to `messages`. The endpoint validates Meta's `X-Hub-Signature-256` against the
raw request body and ignores events addressed to another phone-number ID.

All six `WHATSAPP_*` values above are required before account pairing is exposed.
The public number is returned only as a `wa.me` link; the API never exposes the
access token, app secret, or verification token.


## Deployment

A systemd service file is provided at `lanis-api.service`:

```bash
# Deploy using the provided script
./deploy.sh
```

The script can be run as a regular user. It requests `sudo` only when installing
or restarting the systemd service.

### Docker

Docker Compose runs the API on host port `9898` and stores its databases and
file cache in the persistent `lanis-data` volume. This is independent from the
existing systemd deployment.

Create a local `.env` before starting the container. At minimum it must contain
a stable `JWT_SECRET`; the same file can provide `VAPID_*`, `DSB_*`,
`PUBLIC_BASE_URL`, and optional AI settings. The file is passed to the container
at runtime and excluded from the image build context.

```bash
./deploy_docker.sh
```

Set `LANIS_DOCKER_BIND_ADDRESS` or `LANIS_DOCKER_PORT` in `.env` to override the
default `0.0.0.0:9898` publication. Install the nightly Docker deployment with:

```bash
./deploy_docker.sh --install-cron
```



## API reference

See the generated [Python API reference](docs/API.md) for the complete client
surface and method documentation.

## License

This project is released under the [MIT License](LICENSE).

### Parallel authenticated uptime monitoring

The uptime monitor samples **registered users**, not a separate monitor-account
list. An eligible user must have `onboarding.status = completed`, a real user
activity timestamp within the past 30 days, and a usable, unexpired stored SPH
credential/session. Skipped or incomplete onboarding, stale activity, account
deletion markers, and revoked/expired credentials exclude the account. Multiple
browser sessions do not increase a user's sampling probability.

Every cycle randomly chooses two distinct users, preferring different schools.
If either probe fails, it waits 15 seconds and tests the original pair plus up
to two new distinct eligible users in parallel. Eligibility is checked again
before every probe, including confirmation. Only selected credentials are
decrypted, under the existing account lifecycle lease; probes use fresh isolated
SDK sessions and never refresh the user's activity timestamp. Thus monitoring
cannot keep an inactive account eligible forever.

No additional accounts file or credential configuration is required. Four
eligible users enable the full confirmation round; two or three show reduced
coverage, and fewer than two produce an unknown insufficient-coverage result.
Each probe has a 90-second total deadline. Normal and incident cycles use the
configured `LANIS_UPTIME_INTERVAL_SECONDS` (default 300); retries never recurse.

Any functional failure in confirmation indicates partially impaired service
(`degraded`), including a single failing account. All sampled accounts failing
login indicate `down`. **Availability and confidence are independent:** a
single-account failure has limited confidence and account scope, but it still
counts as impairment in uptime calculations and opens a scoped incident. Two or
more failures with complete expected coverage have confirmed confidence; missing
or reduced confirmation coverage limits confidence. No functional failures plus
incomplete evidence remains `unknown`, and does not open or resolve incidents.

The stored evidence includes observed status, confidence, scope (account, one
sampled school, multiple sampled schools, or sampled accounts), completed versus
expected probes, and the number of accounts failing in both rounds. Public
status allowlists only aggregate confidence/scope/status values; Discord includes
human-readable confidence and scope without identifiers. An isolated generic
login failure may still be an account credential problem: limited confidence
makes that uncertainty explicit instead of deleting it from the measurement.

All-pass confirmation records a transient failure without opening an incident. Recovery
from an existing incident requires two consecutive healthy scheduled cycles;
manual checks cannot advance recovery. One aggregate observation per cycle feeds
existing time-weighted uptime and incidents. Private admin history retains both
rounds with cycle-local account labels and numbered school groups, never usernames, passwords, or school IDs.
The public status endpoint and Discord continue to expose only aggregate data.
Discord retains its existing incident/recovery transition deduplication.

`LANIS_UPTIME_PROBE_DEADLINE_SECONDS` configures the total per-account deadline
(default 90; clamped to 5–180). Request timeouts still use
`LANIS_UPTIME_TIMEOUT_SECONDS`. A manual check joins any currently running cycle
instead of starting another one. The admin endpoint waits up to five seconds and
then returns current status plus `active_cycle.phase`; the shared cycle continues
in the background, and the portal refreshes every ten seconds while on Uptime. Isolation/coalescing applies within one backend
process: run the scheduler in a single worker, as in the current deployment.
Server shutdown explicitly stops the shared cycle and reaps its probe processes
before draining Discord deliveries. HTTP waiter cancellation still leaves the
shared cycle running; shutdown prevents new cycles until scheduler startup.
Ambiguous credential failures remain operator-reviewed because the SDK's generic login
failure cannot reliably distinguish bad credentials from upstream failure;
accounts are not automatically disabled on that ambiguous signal.

## External agents

Claude connectors and other MCP clients can connect through a shared
OAuth-protected `/mcp` endpoint. The public ChatGPT plugin is planned; operator
custom-MCP testing and publication requirements are documented separately. Users approve and manage each connection
in LANIS Settings → Agents. See [setup, permissions, and plugin packaging](docs/agents.md).
