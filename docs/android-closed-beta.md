# Android closed beta: rollout and operator runbook

Package: `de.joancode.lanis`. Candidate opt-in link:
`https://play.google.com/apps/testing/de.joancode.lanis`.

## Verified scope and activation gate

No Play Console changes or private-track inspection were performed by this implementation.
Last operator-provided state: 1.0.2 (versionCode 3) on INTERNAL;
CLOSED Alpha had no release and zero testers. This is not evidence of a live
closed test. Keep invitations disabled until the operator completes the checks below.

Google documentation:
- https://support.google.com/googleplay/android-developer/answer/9845334
- https://support.google.com/googleplay/android-developer/answer/14151465

Internal testing does not meet the closed-test production-access requirement.
The goal is at least 12 real testers continuously opted into the CLOSED test
for 14 days. Registration, allowlisting, popup acknowledgement and link clicks
are not proof of opt-in, installation, participation or feedback.

## Rollout order (separate approval; nothing deployed by these PRs)

1. Review the lanis_api PR, then deploy it through the normal approved process.
   Startup `auth_db.initialize()` adds the `android_beta` table with unique account,
   opaque request ID and unique normalized email indexes. No manual migration or
   backfill. Back up `data/auth.db` normally. Rollback can leave the unused table.
2. Deploy lanis_admin, then lanis_ui after backend availability is confirmed.
   Both use their existing backend URL and session/admin authentication settings.
3. Set backend `ANDROID_BETA_ENABLED=false` (the default) during preparation.
   `ANDROID_BETA_JOIN_URL` defaults to the candidate link above; replace it with
   the actual CLOSED opt-in URL copied from Play Console. There is no cron or Play API.
4. In Play Console select this package, Testing > Closed testing > Alpha.
   Create/select the closed track and publish an eligible signed release there.
   Verify review/publication is complete and the intended countries are available.
   Select/create the email tester list for this CLOSED track, save it and record
   all pre-existing tester addresses. Configure a real feedback channel.
5. Copy the CLOSED opt-in URL from that track. Verify with an allowlisted Google
   account that it explicitly offers joining this closed test, followed by download.
   If that account is enrolled in internal testing, leave internal testing first
   and verify the closed track enrollment; an internal install does not count.
6. Only then enable `ANDROID_BETA_ENABLED=true` via the normal approved deployment
   configuration process. Verify signup in Android Chrome and exclusion in Capacitor.

## Daily manual approvals

1. Open Android-Beta in the private admin portal. Filter Ausstehend.
2. Copy pending addresses and add them to the existing CLOSED tester list in Play
   Console. Alternatively paste ALL current Play Console tester addresses into the
   export panel, confirm preservation and download the full CSV. The export unions
   those addresses with all registered pending and approved addresses, deduplicates,
   and emits one address per line, no header and no UTF-8 BOM.
3. CSV uploads can REPLACE an existing Play list. Inspect the full union before
   upload; preserve testers who registered outside LANIS too. Export never approves.
4. Save changes in Play Console and verify they apply to the closed track.
5. Back in the portal select exactly those requests and choose Freischalten, then
   confirm that the addresses were added. A changed or withdrawn request rejects
   the entire approval batch; reload and process the current addresses.
6. Approved users see the invitation on their next Android-browser visit/resume.
   Settings retains their opt-in link. They must explicitly join using the listed
   Google account before downloading. There are no notification emails.
7. Collect genuine feedback and monitor real closed-test enrollment in Play Console.
   Keep at least 12 testers continuously enrolled for 14 days; recruit a buffer.
   Do not infer enrollment or fill in fabricated feedback from LANIS statuses.

## Corrections, withdrawal and privacy

Changing email resets approval and creates a new revision. Withdrawal clears the
email and approval; deletion removes the entire row and account-related audit
entries through existing cleanup. Account exports include the user's beta state.
Remove obsolete/withdrawn/deleted addresses from Play Console manually when
appropriate: the application cannot change an external allowlist. Exports preserve
existing addresses rather than silently removing external testers.

Signup conflicts return a generic message without an account identity. Ordinary
users can read/change only their own state. Full lists, exports and approval use
existing admin permissions, lifecycle gates and audits. No Google credentials or
OAuth are involved. Prompt dismissal is permanent until registration; Später
sets a seven-day account-backed cooldown. Settings remains accessible either way.

Disable invitations with `ANDROID_BETA_ENABLED=false` to pause recruitment and
join links. Existing users can still read their state and withdraw.
