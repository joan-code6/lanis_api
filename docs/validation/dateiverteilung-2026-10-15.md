# Dateiverteilung live validation — 2026-10-15

The PR implementation was tested locally against Schulportal using credentials
read from the production credential store, as authorized by the user. The
production database connection was read-only. The production application was
not edited, deployed, or restarted. No credentials, account identifiers, raw
portal pages, or downloaded documents are included in this report or fixtures.

## Live coverage

- 41 distinct active accounts across 14 schools were checked once.
- 40 logins succeeded; one failed and was not retried.
- 34 accounts across seven schools exposed Dateiverteilung.
- 33 eligible accounts had genuinely empty recipient pages.
- One account had three distributions: two notices and one file distribution.
- After the fixes, all 34 overview responses matched the portal's distribution
  and file counts: three distributions and one file in total.
- The real generated download succeeded as an `application/pdf` attachment with
  a PDF signature, a `.pdf` filename, and 511,436 bytes. File contents were
  consumed only for validation and were not added to the repository.

## Defects found and fixed

The old parser returned an empty list for the populated account. Current portal
markup uses `.verteilung` panels, `.headline`, `.panel-body`, and
`.verteilungmulti .readAndDownload` buttons, instead of the synthetic anchor
markup covered by the original tests. The portal's `module/dateiverteilung/js/user.js`
generates recipient downloads as `dateiverteilung.php?a=user&v=<distribution>&k=<file>`.

The parser now preserves panel order, separates titles from dates/read controls,
extracts author provenance and multiline notices, recognizes notice-only panels,
and constructs those recipient download URLs. The URL validator permits only
one `a=user` action with single numeric `v` and `k` values and no extra keys;
legacy download links remain supported. A fictional fixture reproduces the
observed structure, with additional multi-file and read/unread variations.

The UI previously used a generated download button's label as the saved filename.
It now uses the server's UTF-8 Content-Disposition filename, preserving the file
extension. Source section labels now use generated IDs so names with spaces do
not break accessibility relationships. Cancelled requests cannot update results
or trigger a late download. Downloads are explicitly non-cacheable, and response
cleanup also runs when the response ends before stream iteration starts.

## API and browser checks

The local harness used the PR's endpoint functions and its in-memory cache with
real authenticated Schulportal clients. Unrelated application background jobs
were not started; the production API was not used to serve the new routes.

Verified cache reuse, refresh bypass, populated/empty account separation,
query-length validation, and rejection of external/ambiguous URLs before any
upstream request. The real PDF also downloaded through the HTTP endpoint. The
same download URL under an empty account from another school returned an error
(502), without returning a file.

The browser rendered the three real distributions and correct provenance,
unread count, and download control. Search matched a real title. The download
used the server filename and correct PDF Blob; the final save action was
intercepted to avoid retaining the personal document. Loading, overview failure,
download failure, recovery, and a real empty account were exercised. At a
390-pixel mobile viewport in dark mode there was no horizontal overflow, and
all source section labels resolved correctly.

## Limits

Only one live account contained files, and its one file was generated from a
template. Other file types, multiple-file distributions, additional markup
variants, and transport failures are covered with fictional fixtures and
controlled tests, rather than claimed as additional live coverage. This does
not validate a production deployment of these PRs or the failed login account.

## Automated verification

- Dateiverteilung tests: 52 passed.
- Dateiverteilung API branch full suite: 421 passed, 1 skipped.
- Ruff passes for the module and its tests; `git diff --check` passes.
- UI TypeScript check (`tsc --noEmit`) passes.
- Nine filename decoding/fallback/path/control-character checks pass with
  `node scripts/test-download-filename.cjs`.
