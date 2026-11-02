# Videokonferenz live validation — 2026-10-15

The API and UI PRs were tested locally against Schulportal with production-stored
credentials under the user's authorization. The production credential database
was opened read-only. Production was not edited, deployed, or restarted. Account
identifiers, credentials, raw pages, and real room links are excluded from this
report and the fictional regression fixture.

## Live coverage

- 41 active accounts were checked; 40 logins succeeded, and one failed once.
- 38 eligible accounts across 11 schools exposed video-room catalogs.
- Their catalogs contained 490 account-bound room entries in total. This is a
  sum across accounts, not a claim of 490 unique conference rooms.
- Every catalog count matched the actual table rows. Every parsed room ID
  matched its portal `data-room` attribute.
- All 38 live status polls succeeded. Room state and joinability matched the
  authenticated status response. No rooms were open at test time.
- The updated implementation was checked again across all 38 eligible accounts:
  all succeeded, all status polls succeeded, and all 490 room IDs were preserved.
- The portal's `module/videokonferenz/js/sus_start.js` confirms the polling
  action (`a=sus_start`, `b=update`) and the use of DOM room IDs.

## API behavior

The local HTTP harness reused the PR's endpoint function with real authenticated
clients, without starting unrelated application background jobs. Repeated
requests with both refresh values always performed a new overview request and
status poll. Responses carried `Cache-Control: private, no-store`.

Two accounts with different catalogs (8 and 12 entries) returned their own room
lists. A request without the local test identity was rejected. A forced timeout
of only the status request against the real eight-room catalog preserved room
metadata, set status to unknown, cleared all join URLs, and disabled joining.
Restoring the status request recovered the live overview.

## Fixes and regression coverage

Expired sessions previously remained in the API's client cache. Authentication
failures now invalidate the exact failed client so subsequent requests can
restore it. Overview/status redirects are rejected; HTTP 401/403 responses are
classified as authentication failures. Timeout and upstream failures retain
recoverable behavior.

Malformed status arrays (including objects, booleans, or nulls) no longer appear
as an authoritative all-closed status response. Unavailable status responses
clear join URLs. Invalid URL syntax, malformed ports, blank URLs, and URLs with
embedded credentials cannot produce join links; the UI also rejects embedded
credentials. An unavailable live status now produces an unavailable summary in
the UI, rather than incorrectly claiming that no rooms are open.

A fictional fixture covers the observed blank first column header, `data-room`
IDs, multiple teacher labels, and paired hidden-open/visible-waiting controls.
Tests cover status authority, expiry/redirects at both fetch stages, malformed
JSON, transport failures, cache headers, exact client invalidation, and URL
validation.

## Browser coverage

The actual UI component and API service were connected to the local live-data
harness. The demo route supplied only the navigation/auth shell. Names, teacher
labels, IDs, and links were replaced before reaching the browser for the public
screenshot captures; counts and live state were preserved.

Verified the eight-room live waiting state, manual refresh, and a delayed request
that retained room cards while disabling refresh. Two extra polling callbacks
during that pending request produced no overlapping request. A controlled open
state offered exactly one HTTPS join link with a new-tab target and protective
rel attributes; its navigation was intercepted. A subsequent controlled HTTP
outage removed joining and showed unknown states while retaining room metadata.
An embedded-credential URL did not produce a join action. Dashboard and global
search navigation were checked with a generic module name/URL and Videokonferenz
exposed only through `direct_url`: both routed to the native page. A real-catalog
status timeout and subsequent recovery were also verified in the browser.
At a 390px mobile dark-mode viewport there was no horizontal overflow.

## Verification and limits

- 37 focused video tests passed.
- Full API suite: 458 passed, 1 skipped.
- Ruff passes for the module and its tests; TypeScript and whitespace checks pass.

No real conference was entered. All live rooms were closed, so successful room
entry, an actual live open-to-closed transition, and media/call behavior were not
validated. Controlled open/error states are identified as controlled tests,
not presented as observed live activity. This does not validate a production
deployment of these PRs or the account whose login failed.
