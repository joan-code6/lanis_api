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
git clone https://github.com/Lanis-dev/lanis_api.git
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

Successful authenticated JSON reads are also retained in private snapshots,
encrypted in the auth database with no age-based expiry. If a later request observes a Schulportal
timeout, connection failure, or HTTP 5xx response, the API can return the last
snapshot for the same account, route, path parameters, and query parameters.
Authentication, TLS, and permission failures are never hidden by a snapshot.

Fallback responses include `X-LANIS-Cache: stale` and the original UTC fetch
time in `X-LANIS-Fetched-At`. Live responses use `fresh` or `hit`. The cache is
bounded to 1,000 entries and 16 MiB of response bodies per account, with the same
16 MiB per-response limit. Accounts never evict each other’s snapshots. It is
cleared on logout, account deletion, and expiry of the last valid session.
Endpoint refreshes retain the previous successful snapshot, and backend restarts
restore persisted snapshots. Failed decryption leaves ciphertext on disk for
recovery with the correct key; invalid snapshot structure is removed.

Authenticated clients can read `GET /cache/status` without contacting
Schulportal. It returns whether snapshots exist, their count, the most recent
successful fetch time, and the retention period. This timestamp describes the
latest successful cached read; it is not a complete account synchronization.

### Daily outage snapshots

The API warms read-only user views daily starting at **03:00 Europe/Berlin** by default.
Set `LANIS_DAILY_CACHE_TIME=04:30` in `.env` to choose another start time
(24-hour `HH:MM`, always Europe/Berlin). Restart the API after changing `.env`.
Invalid values log a warning and fall back to `03:00`.

The schedule follows daylight saving changes. It processes one valid stored session per
user, sequentially, and refreshes the standard profile, modules, calendar,
substitution plan, timetable, courses, attendance, homework/submissions and
message headers. Supported file-storage folder trees, file-distribution lists,
and election forms are discovered from the user’s module capabilities. Submission
details are discovered from submission and course views. Current and next-year
calendar lists, course details, and calendar event details are discovered from
overviews;
previously requested parameter variants are refreshed too. Snapshot keys normalize
explicit route defaults, allowing a default UI request to use a view warmed
without redundant query parameters.
Native substitution-plan reads, including saved variants, run only when the
account's current module list advertises native plan support.

Message headers are cached without opening conversations. Nightly sync never
fetches conversation bodies, including previously cached bodies, because
Schulportal marks them read when fetched. Bodies are fetched only when the user
opens a conversation; successful interactive reads can still supply outage
snapshots. Messages never opened in LANIS have headers only during an outage.

Files advertised by file-storage trees, file distributions, course entries, and
submission details are downloaded sequentially through authenticated API routes.
Course files reuse the existing URL-hash cache; the other binary routes use
content-addressed blobs in that same cache directory, deduplicating identical
bytes across source URLs. Access still requires a per-user file reference and a
valid session. Binary files are stored on disk outside the 16 MiB JSON budget,
with the existing 256 MiB per-download bound; no aggregate binary quota is added.
Nightly reads refresh existing files while ordinary reads can serve local bytes
without contacting Schulportal. Active users' referenced files are protected
from age cleanup, and cleanup runs only after a positive portal health check.
Arbitrary search results, external websites and live conference join URLs are not
bulk mirrored. Settings stored entirely in LANIS can be read with local auth.

A fresh authenticated uptime check must report `up` before the batch starts.
Configure at least two explicitly enrolled monitor accounts through
`LANIS_UPTIME_ACCOUNTS_FILE` as described under Parallel SPH monitor below.
Legacy single-account credentials alone cannot produce a healthy result. Unknown,
unconfigured, degraded and down states defer the batch. Failed batches retry every
15 minutes for up to 24 attempts, then resume at the next daily run.
No credentials are copied into the scheduler or logs.

Refreshes replace outage snapshots only after a successful response. Expiring or
invalidating a live response does not delete its last successful outage snapshot.
Snapshots have no age expiry (`/cache/status` reports `retention_seconds: 0`)
and are encrypted in the auth SQLite database so backend restarts preserve them. Limits are 16 MiB per response,
1,000 snapshots and 16 MiB of response bodies per user. The oldest snapshots of
that same user are evicted at capacity; no shared byte quota evicts other accounts.
At 300 users the retained response-body budget is 4.69 GiB, plus Python object
overhead, pending disk writes, and the separate live-response cache in memory;
encryption and SQLite add overhead on disk. `GET /cache/status` exposes
`body_bytes`, `max_body_bytes`, and `max_snapshot_count` for the current user.
The `sync` object reports attempted and failed reads, quota evictions and the
attempt finish time. `sync.complete` is false when any read fails or a quota
prevents retention; successful HTTP reads rejected by snapshot limits count as
incomplete too. This reports coverage of discovered supported routes, not arbitrary
SPH apps or all possible search/filter queries.
This is bounded coverage, not a complete offline mirror of all Schulportal data.
Final-session revocation records a durable timestamp cutoff in the auth
transaction, so pre-revocation snapshots cannot return after a failed disk
deletion and later login. Account deletion removes this revocation metadata
after runtime snapshot cleanup. Session revocation and account deletion remove
private snapshots;
serving them always requires a valid LANIS session. Cached responses retain their
original fetch timestamps so the UI can identify stale data.

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

WhatsApp is now managed in **Settings → Agents**, alongside Signal. Both channels
use the shared backend harness, central AI access, approved MCP permissions and
persistent scheduled tasks. Existing WhatsApp users must reconnect to approve
the new permission model. See [hosted agent setup and migration](docs/hosted-agents.md).

The Meta Cloud API transport keeps its existing environment variables:
`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`,
`WHATSAPP_APP_SECRET`, `WHATSAPP_GRAPH_API_VERSION` (e.g. `vXX.X`), and
`WHATSAPP_PUBLIC_NUMBER`. Set the supported Graph version for your Meta app.
Register `https://<api-host>/whatsapp/webhook` and subscribe to messages; retain
the configured app secret for webhook signature verification.

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

### Rate-aware authenticated uptime monitoring

The monitor samples two distinct registered users, preferring different schools.
Eligible users must have completed onboarding, real activity within 30 days, and
usable, unexpired stored SPH credentials. Revoked/deleted accounts are excluded,
eligibility is rechecked before each probe, and only selected passwords are
decrypted under the account lifecycle lease. Monitoring never refreshes activity.

Accounts are probed **sequentially**, because they share the server's egress IP.
Each isolated SDK flow checks maintenance, login, and one authenticated module
list request. It does not resolve module links or open applets; a healthy module
list does not guarantee individual module operations. A hard budget of 12 wire
requests per account includes redirects, and exhausted budgets remain unknown.
The existing total deadline (90 seconds by default) and request timeout still
apply. Empty module lists and HTTP 401/403 are unknown evidence rather than a
service-wide outage.

HTTP 429, or HTTP 503 with `Retry-After`, immediately stops further requests,
including SDK fallback requests, the second account, and confirmation probes.
The aggregate observation is **unknown**, excluded from available/failed time,
and reduces coverage. It neither opens an incident nor confirms recovery.
`Retry-After` accepts seconds and HTTP dates. Cooldowns start at twice the normal
interval, double on repeated throttling to a one-hour backoff, and always wait at
least the server's `Retry-After` (bounded to one year). The retry timestamp and
streak are persisted, so manual requests and process restarts cannot bypass them.
This cooldown governs monitor traffic; normal user requests still share the IP.

Functional failures trigger one confirmation of the same two accounts after a
60-second quiet interval. There is no expansion to four accounts. Only failures
repeated across both accounts establish a service impairment. An isolated
account failure with another healthy account remains account-scoped and does
not count as general downtime. Missing or ambiguous evidence stays unknown.
Two healthy scheduled cycles confirm recovery; manual checks cannot advance it.
Unknown observations retain any pending incident without resolving it.

Each cycle persists one aggregate observation and private, cycle-local account
labels, numbered school groups, request counts, and safe diagnostics. It never
stores credentials in monitor history. Public responses allowlist status, safe
error codes, confidence, and scope, and describe these measurement limits.
Availability remains time-weighted; unknown and stale spans reduce coverage.
Historical generic errors without recorded HTTP evidence cannot reliably be
reclassified as rate limits and are left intact.

`LANIS_UPTIME_INTERVAL_SECONDS` (default 300) governs healthy and incident cycles;
there is no accelerated incident polling. `LANIS_UPTIME_TIMEOUT_SECONDS` bounds
every request, including explicit SDK timeouts. The total per-account deadline is
`LANIS_UPTIME_PROBE_DEADLINE_SECONDS` (default 90, clamped to 5–180).
Manual checks join a running cycle or return the stored observation until the
next probe is due, without adding samples or notifications. The admin endpoint
waits up to five seconds, then exposes `active_cycle.phase` while work continues.

Coalescing and scheduling assume a single backend worker, as in the current
deployment. Shutdown stops shared work, reaps child processes, and drains Discord
deliveries. Discord retains incident/recovery deduplication. Generic SDK login
failures remain ambiguous; accounts are not automatically disabled. All probes
share one network vantage point and do not establish global SPH availability.

## External agents

Claude connectors and other MCP clients can connect through a shared
OAuth-protected `/mcp` endpoint. The public ChatGPT plugin is planned; operator
custom-MCP testing and publication requirements are documented separately. Users approve and manage each connection
in LANIS Settings → Agents. See [setup, permissions, and plugin packaging](docs/agents.md).

## Shared Signal / WhatsApp agents

The backend-hosted agent runtime now lives under **Settings → Agents**. Both
messaging channels use the central AI configuration, approved LANIS MCP scopes,
encrypted conversations, and durable cron/one-time tasks. Existing WhatsApp users
must approve a new connection. See [setup, migration, APIs and operational limits](docs/hosted-agents.md).
