# SPH status and request reliability investigation

Date: 2026-11-08. Development base: `origin/main` at `ae45f19`.
Production was inspected read-only. No deployment, restart, production database
write, or notification was performed. Live SPH testing used only the designated
test account; no messages were opened or sent.

## Findings

The production monitor evidence from the preceding three days contains 24 cycles
classified as confirmed incidents. In 23 of these cycles, at least one account
was healthy in the confirmation round. Failed confirmation observations record
45 generic `login_failed` errors and five `modules_request_failed` errors. These
are observations, not proof that every incident was false: the existing data
cannot distinguish rejected credentials, an account restriction, session expiry,
or an upstream failure.

`login()` catches requests exceptions and previously converted them into a
message string. The monitor therefore lost timeout/connection/HTTP evidence and
classified any missing session cookie as downtime. Two unrelated rejected
accounts could consequently produce a confirmed incident. Registered, recently
active accounts are not necessarily accounts whose stored credentials still work.

Session restoration shares successful logins under a per-account lock, but a
failed login left no shared result. Every queued request could immediately make
another failing upstream login. This was reproduced with concurrent local
requests and is covered by a regression test. Journal entries also show repeated
session-restoration failures, although the journal includes automated probes and
must not be interpreted as a count of affected end users.

The client explicitly advertised Brotli and Zstandard, while the production
requests stack reports support for only gzip and deflate. This is a protocol
mismatch capable of breaking parsing if SPH chooses an unsupported encoding.
Live responses in this investigation used gzip; unsupported compression was not
established as the cause of the reported incidents.

The monitor called `get_available_modules()`, which resolves external redirects
and SSO applications, and then opened every module. That unnecessarily made a
portal availability probe depend on unrelated vendors, school directory size,
and a much larger number of upstream requests. A successful redirect was also
accepted as proof that a module opened.

## Changes

- Preserve safe structured login errors: timeout, connection, TLS, HTTP failure,
  request failure, or unconfirmed login. Missing cookies alone yield unknown
  monitor availability; positive transport/HTTP failure evidence still yields
  downtime. TLS and expired account sessions remain inconclusive.
- Preserve authentication classification when the apps endpoint hits a login gate.
- Probe only a small allowlist of native read pages directly from the app
  directory, without resolving vendor/SSO links; stop after the first HTTP 200.
  Accounts without a suitable native page yield unknown module availability.
- Advertise the compression encodings supported by the installed requests stack.
- Share failed session-restoration results for five seconds, matching the existing
  Retry-After response. Retain local refresh tokens and allow recovery afterwards.
- Log safe SPH failure codes, fixed portal hostnames, status codes, and durations.
  Transport diagnostics omit credentials, queries, tokens, cookies, bodies, and
  exception messages. Session-restoration warnings include the structured code.

## Validation

139 focused tests passed across login evidence, monitoring, public status,
registered-account sampling, session expiry/recovery, and outage caching.
`git diff --check` passed. Remaining Ruff findings in the checked files also
exist on the base revision (legacy typing, broad exception handling, and naive
`datetime.min`); the new transport and login-evidence test files pass Ruff.

Live checks established that a second test-account login did not invalidate the
first session. Three rounds of four concurrent requests to apps, timetable,
calendar, and lessons completed with HTTP 200 and gzip; the apps responses were
also validated as JSON. This short test does not prove that intermittent errors
are resolved. The patched synthetic probe completed successfully with login and
one native module verified. No production monitoring cycle was triggered.

## Remaining limits and next observation

This patch is not deployed. Existing historical incidents are left intact because
lost login diagnostics cannot be reconstructed reliably. After an explicitly
authorized deployment, correlate the new `schulportal.transport` codes with
session-restoration warnings and client request IDs when failures recur. A
persistently unconfirmed login requires an account credential/login-flow check;
unknown status must not be interpreted as verified health. The current evidence
does not justify attributing all app errors to an SPH outage or to one cause.
