# Account analytics and private diagnostics

Deploy this lanis_api change before the corresponding LANIS UI analytics v3 update. No production deployment is performed by this PR.

## Configuration

Existing credential encryption configuration supplies the server secret. Diagnostics derive a separate HMAC-SHA256 domain key from that secret, then use AES-GCM with random nonces and authenticated versioned context. Keep the existing credential secret stable and backed up securely. Changing it makes existing diagnostics unreadable. No password/session ID is used as an encryption key; no encryption secret is shipped to the UI or PostHog.

Optional backend event delivery:

```dotenv
POSTHOG_PROJECT_KEY=<public phc_ project ingestion key>
POSTHOG_HOST=https://t.lanis.arg-server.de
```

Leaving the key unset disables backend PostHog delivery, while identity and encrypted diagnostics continue to work. Only the LANIS proxy and direct EU ingestion host are accepted. A private PostHog management token is never needed by the application.

## Client endpoints

`GET /analytics/identity` returns a stable random `la_…` reference for the authenticated canonical LANIS account. It is independent of password/session rotation and shared across devices. Only the backend stores the reference-to-account mapping.

`POST /analytics/diagnostics` accepts `kind` (error/rejection/react/api), message (max 4096), stack (max 16384), optional request UUID. It returns `diagnostic_id` (`ld_…`) and `retention_days=7`, without echoing details. Both routes require `X-Session-Token` and `X-LANIS-Analytics-Consent: account-v2`, return no-store and never call Schulportal.

The consent header is supplied by the consenting UI; it is not proof of legal consent from arbitrary third-party clients. Authentication, lifecycle locks and session revalidation protect account ownership and deletion races.

## Operator investigation

Existing admin authentication (`X-Admin-Token`), allowlist and lifecycle authorization apply to all routes:

- `GET /admin/analytics/accounts/{account_ref}` resolves the LANIS user ID.
- `GET /admin/analytics/diagnostics/{diagnostic_id}` decrypts a nonexpired diagnostic.
- `GET /admin/analytics/requests/{request_id}` retrieves nonexpired diagnostics associated with that request.

Each successful resolution/read writes an admin audit entry and returns no-store. An ordinary LANIS token does not grant access. Resolve only for authorized support/debugging; treat decrypted output as sensitive account data. No admin frontend is added to LANIS UI.

## Retention and correlation

Every API response receives a server-generated `X-LANIS-Request-ID`, exposed through CORS. Structured warning logs contain method, static route template, status, duration and request UUID, without raw URLs, bodies or exception text. Unhandled server exceptions for a consenting live session are encrypted in the same vault and return a generic 500 plus request UUID. Requests without consent still get correlation IDs but no diagnostic payload is retained by this middleware.

When configured, background `backend_request_completed` events use `app=lanis_api`, opaque account reference, static endpoint template, method, status, duration and request UUID. No IP enrichment, bodies, URLs, user names or stack text are sent to PostHog. Admin/analytics routes are excluded. At most 64 delivery tasks run; each has a three-second request timeout. Delivery failures do not fail the user request. These events measure backend responses, while UI events measure browser attempts: do not add the counts together.

Vault records expire after seven days, are filtered immediately on reads and purged during startup, hourly cleanup and writes. Limits: 20 writes/minute/account, newest 100 records/account, newest 5,000 globally. Limits can evict records before seven days. Diagnostics contain exception messages/stack traces, which may include sensitive context, and remain sensitive even encrypted. Request UUIDs and ownership metadata are indexed without encryption for correlation; error details are encrypted.

Account export includes analytics reference/diagnostic metadata, not decrypted error details. Account deletion atomically removes mapping and vault rows alongside other local account data. Sending and reads take shared account lifecycle leases so deletion cannot race an in-flight backend operation. Deleting local data or withdrawing consent does not automatically delete historical PostHog events. PostHog deletion requires a separate authorized project operation.

## Request investigation and health thresholds

`GET /admin/analytics/investigate/{account_ref}` returns the account mapping, diagnostic metadata and the latest 100 technical requests. `GET /admin/analytics/quality` returns five-minute observed request/failure/diagnostic counts, linked-account count, coverage text and internal warning thresholds. Both require normal admin authentication and audit successful access. No raw diagnostic details are returned by investigation.

The request ledger stores only live-session, consented traffic using static route templates. It retains up to 500 rows/account, 50,000 globally and seven days, with expiry on reads/cleanup and atomic account deletion. Requests already sent before withdrawal may complete. Five-minute warnings require sufficient volume: at least 20 requests, three affected accounts and 20% 5xx responses; or ten diagnostics from three accounts. They are internal operator warnings, not PostHog lifecycle alerts or a full incident detector.

Feedback accepts an optional `support_context` with release, safe page and at most ten request UUIDs. Ownership is checked against the submitting user's recent request ledger. The context is stored with feedback and shown to operators; it contains no automatic message/body capture. Existing feedback retention/deletion rules continue to apply.

`GET /admin/analytics/users/{school_id:username}` performs the same audited investigation by LANIS account identity, without creating an analytics reference for a nonconsenting account. Missing data returns 404 rather than implying the account is healthy.


## Operational Logs and tracing

Install the updated `requirements.txt` when deploying this revision. With the existing `POSTHOG_PROJECT_KEY`, operational export starts automatically; no new token is needed. `POSTHOG_OBSERVABILITY=0` disables it. `LANIS_ENVIRONMENT` and optional Git SHA `LANIS_RELEASE` label the resource. Missing configuration does not stop the API. Existing account-linked request events keep their consent and deletion barrier.

`lanis_api` exports OTLP HTTP/protobuf directly to EU PostHog at `/i/v1/logs` and `/i/v1/traces`. The separate analytics proxy does not need to support OTLP. Both SDK batch queues hold at most 2,048 records, export batches of 128, and use three-second exporter timeouts. Shutdown drains product delivery tasks and gives both operational providers one shared three-second deadline. It does not call the SDK’s unbounded force-flush path. After the deadline no new export batch starts; a request already in flight may finish under its own HTTP timeout. These are best-effort telemetry queues, not a durable audit log; overload/crash can lose buffered records. Do not sum browser attempts, backend summary events and request spans as independent requests.

The ASGI span covers the response body, not just response headers. Request logs distinguish TTFB, full duration, emitted bytes, HTTP status, cache category, cancellation and incomplete/failed streams. Response `X-LANIS-Trace-ID` connects browser events/Logs to the backend trace; CORS exposes it. Consent-gated, UUID-validated `X-LANIS-Client-Request-ID` and `X-LANIS-Session-ID` provide attempt/session correlation. Incoming trace baggage is not accepted, and trace/identity headers are not injected into Schulportal or provider requests.

Public module operations, Schulportal/DSB and provider HTTP calls, SQLite operations, queue attempts, daily cache, uptime checks, notifications/push, file cleanup, account export/deletion, MCP tools, hosted-agent turns, Signal/WhatsApp/Discord, and AI/embedding calls now have safe spans and operation logs. Background work is measured without attaching account names, grants, phones, prompts or payloads. The standalone `sph-client` package uses optional standard-library hooks and never imports the API or OpenTelemetry.

Existing Python application logs are exported as severity plus a fixed message and repository source/line. Their original message, arguments, exception details and extra fields stay local. Structured request/operation logs retain only approved technical fields. At the trace exporter, spans are rebuilt with allowlisted attributes; raw URLs, SQL, exception events/locals, status descriptions and library metadata are omitted. The resource does not include host/process details. No IP, username, school identifier, credential, form, message, filename, prompt, completion or tool result is exported.

`GET /admin/analytics/quality` now includes `telemetry`: enabled/configured booleans, product-event sent/failed/dropped counters, OTLP sent/failed batch counters and last successful delivery timestamps. Product ingestion checks HTTP errors and closes the response; failures no longer look like accepted delivery. These counters are process-local and reset on restart. A local SDK internal-metrics reader adds record-level queue-drop counters for both signals without exporting metrics. Sent/failed counters still count batches, not records. Shutdown records queued/in-flight records still unfinished at its deadline separately from confirmed queue or shutdown drops; an in-flight export may subsequently succeed. Download preparation and lazy stream consumption have separate spans, including late stream failure/cancellation and cross-thread iterator consumption. DSB requests retain tracing but do not change the Schulportal outage observation.

After deployment, verify real traffic appears under service `lanis_api` in Logs and Tracing and inspect the quality endpoint. Backend account events still require live consented sessions; login, expired sessions, bearer MCP and scheduled jobs are covered by operational signals instead. Operational signals omit account identity; only consenting browser sessions carry session correlation. This does not enable session replay, arbitrary console/stdout forwarding, LLM prompt capture or native Android crash collection.

References: [Python Logs](https://posthog.com/docs/logs/installation/python), [Python tracing](https://posthog.com/docs/distributed-tracing/installation/python).


Async database execution keeps the request/job trace context across aiosqlite’s per-connection queue. Each `_execute` call starts a content-free `sqlite.async` span in its caller, then copies that call’s context into the queued function. This covers connection shortcut queries, cursor operations, fetches and transactions, includes queue wait, and parents native SQLite cursor spans correctly even when concurrent requests share an existing connection. SQL text, parameters and results remain excluded from export. Shutdown removes the bridge without overwriting an integration installed after it.
