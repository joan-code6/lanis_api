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
