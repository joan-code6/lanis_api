# SPH status and request reliability investigation

Date: 2026-11-08. Development base: `origin/main` at `ae45f19`.
Production was inspected read-only. No deployment, restart, production database
write, or notification was performed. Initial live SPH testing used the designated test account. Follow-up testing used
stored production credentials, explicitly authorized by the user. Credentials
were decrypted only in process memory and sent only to SPH's login endpoint;
identities, passwords, tokens, and response bodies were omitted from outputs.
No messages were opened or sent.

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

146 focused tests passed across login evidence, monitoring, public status,
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


## Follow-up: reproduce the cold-session failure

The user clarified that failures mainly happen when reopening an existing LANIS
session. Six accounts repeatedly failing restoration in the preceding 24 hours
were identified from the journal and matched to their valid local refresh-token
rows using read-only SQLite. Decryption succeeded; stored identities matched
those rows. The affected accounts did not contain the known test-password
placeholders checked. Multiple active tokens for an affected account contained
the same password, ruling out inconsistent device credentials in this sample.

Submitting these stored credentials with the portal's own hidden form fields
and server-assigned cookies reproduced rejection for every affected account:
HTTP 200, no authenticated session cookie, and `#lemon_code` state `5`. This
state is interpreted by SPH's delivered frontend bundle as a rejected login,
with instructions to check school, username, and password. Hidden static error
text is present even on ordinary login pages and was not used as evidence.
A production account without restoration failures in one of the affected
schools successfully obtained authenticated cookies using the same procedure.
The reproduction did not require simultaneous requests or heavy traffic.

Read-only metrics also show that some affected accounts previously had portal
profile data collected successfully. The evidence establishes rejection of their
saved login today; it does not establish whether a password changed, an account
was renamed/restricted, or SPH imposes an account/source-specific restriction.
A working existing browser session does not test a fresh password login.

A concrete backend failure chain is now reproduced:

1. LANIS's persisted refresh token remains valid for up to 90 days; refreshing
   its access token deliberately does not authenticate with SPH.
2. The live SPH client is held only in memory and discarded after one hour of
   inactivity, on detected SPH session expiry, or on process restart.
3. The next data request restores the client using the saved password. For the
   affected accounts SPH explicitly rejects that login, even through the
   portal-equivalent form flow.
4. Previously this was always reported as retryable HTTP 503. The app therefore
   kept a locally valid login and repeatedly retried credentials SPH rejected.
   The monitor independently counted the same rejection as portal downtime.

The patch now recognizes explicit portal rejection states `4`/`5` and returns
401 with a fresh-login instruction during cold restoration. The existing UI
401 interceptor tries local refresh once, then redirects to login after the
repeated 401. Temporary network failures remain 503 and keep existing tokens.
Both failure status and cooldown are shared across parallel requests. A fresh
successful login immediately clears the rejection. No server-side token or
private snapshot is deleted merely because SPH rejects a saved login.

The client also added a hardcoded `sph-login-upstream=3` cookie alongside SPH's
server-assigned `.hessen.de` cookie. Live requests confirmed duplicate conflicting
routing cookies. That assignment is removed, and an outgoing-cookie regression
test covers preserving the server choice. Paired live checks did not establish
that the duplicate caused the sampled failures: the designated test account
worked with either flow and a rejected account still failed with the server
choice preserved. This is treated as a separate protocol defect, not the proven
reason for credential rejection.

A local regression sends 1,000 concurrent restoration requests after explicit
rejection, verifies a shared 401 and one upstream attempt, and verifies immediate
recovery through a fresh successful login. Follow-up live restoration tests also
exercise the actual AuthManager with read-only production credential lookups;
production service state and databases remain untouched.

The live AuthManager reproduction completed 1,000 concurrent requests with a
rejected production login (all returned the new 401) and another 1,000 with a
working production login (all shared a restored session). Each scenario made
exactly one upstream login attempt. Thus the test exercised 2,000 backend
restoration calls while sending only two real credential submissions to SPH.
No browser sessions, local auth rows, or production process state were modified.
