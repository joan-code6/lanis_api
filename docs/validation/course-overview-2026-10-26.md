# Course overview incident — 2026-10-26

Investigation used current UI main `d3a84ad2` and lanis_api main `3fc59de`.
The October 16 UI fix (#90) is present on main. The initial follow-up UI
PR (#116) was based on an outdated local branch and has been closed.

## Observed production behavior

Authenticated reads used only the configured test account. No production code,
service configuration, or database was changed, and no service was restarted.
No credentials, tokens, account identifiers, or raw portal HTML are recorded here.

- `/meinunterricht` returned `success: true`, `entries: []`, `courses: []`,
  `entry_count: 0`, and `course_count: 0`.
- It had `X-LANIS-Cache: hit` and `X-LANIS-Fetched-At: 2026-10-26T07:55:42Z`.
- `/meinunterricht/attendance` returned 13 courses, all 13 attendance summaries,
  and zero failed courses. Its cached result was fetched at `07:56:13Z`.
- A direct authenticated SPH read using the current development parser returned
  13 lesson entries and 13 course folders. The page contained the `mein Unterricht`
  heading and `table#aktuellTable`.
- At `08:05:45Z`, after the ten-minute API response cache expired, the same
  production overview endpoint returned 13 entries and 13 courses with
  `X-LANIS-Cache: fresh`. No deployment or restart was needed.

Times above are UTC; add one hour for Berlin on this date.

## What this establishes

The current frontend received a successful empty response from the backend.
This is not evidence that the older frontend course-folder fix was reverted.
A transient empty parse was accepted and cached for ten minutes independently
of attendance, which reads the upstream overview separately.

The raw upstream response responsible for the original empty parse was not
retained. Its exact contents cannot be reconstructed from these responses.
A login redirect, an interstitial, or a transient empty overview remains an
unproven explanation for that initial response.

The parser previously accepted any HTTP 200 HTML as a successful overview,
even without course elements or overview markup. The patch confirms an empty
result with one additional read, rejects login responses and unrecognized empty
pages, and permits an empty recognized overview. Failed parses do not update
the short-lived response cache or replace the last successful outage snapshot.

## Verification

Parser regressions cover transient empty-page recovery, transient empty-table
recovery, persistent invalid pages, an empty overview table, and a login page.
An API route regression covers a good response, an invalid upstream page after
response-cache expiry, preservation of the good snapshot, and subsequent recovery.
The live populated test account is also checked with the patched parser.
A real account with no courses was not available; that case uses a fixture.
