# Android closed beta: self-service Google Groups

Package: `de.joancode.lanis`. Users join
https://groups.google.com/g/lanis-beta/about with their Play Store Google account,
then explicitly opt into https://play.google.com/apps/testing/de.joancode.lanis
with that same account before downloading. Google Group membership establishes
eligibility only. LANIS does not verify membership, opt-in, installation or feedback.
The UI first opens Google account selection with the group/about page as the
return URL. Users must still confirm joining at Google; there is no automatic
join request, Google OAuth callback or membership check in LANIS.
Some supervised child accounts cannot join Google Groups; do not promise access
for every student. No Google password, OAuth or Groups API is involved.

## Activation gate and operator-reported state

Coordination comments on UI PR #125 report that Alpha uses
`lanis-beta@googlegroups.com`, the group permits joining and its public `/about`
page is readable without signing in. Conversations/member lists/posting remain
private. Version 1.0.2 (code 3) was submitted to CLOSED Alpha review, with prechecks
running. These are operator reports, not an independently verified live release.
Recruitment MUST remain off until approval and actual closed-test access are verified.
This agent changed no Play Console/group settings and deployed nothing.

## Rollout and configuration

1. Review/deploy lanis_api #98 through the normal separately approved process,
   then lanis_ui #125. The old admin PR #15 is superseded; no operator queue is needed.
2. Backend startup creates `android_beta_preferences` with only account ID,
   permanent invitation dismissal and cooldown-until timestamp. If the old
   `android_beta` table exists, startup preserves recruitment preferences then drops
   that obsolete registry (including emails, approvals and acknowledgement state).
   Migration is idempotent. Existing DB backups retain their normal retention policy;
   do not restore obsolete email registries into live use. No backfill or cron.
3. `ANDROID_BETA_ENABLED=false` is the default and must stay false during review.
   Optional `ANDROID_BETA_GROUP_URL` defaults to the group `/about` URL above.
   `ANDROID_BETA_JOIN_URL` defaults to the package's testing URL above.
4. The Console owner verifies that the CLOSED Alpha release is approved/published,
   intended countries/feedback settings are correct, and the Google Group remains
   selected for that CLOSED track. Do not change Console/group settings from this task.
5. With a real eligible Google account, join the group, sign into the same account
   on the closed opt-in page, explicitly join, and verify download. An account in
   internal testing may need to leave the internal test first to verify closed access.
6. Only after those checks, enable recruitment through the normal approved config
   rollout. Confirm Android-browser invitation after dashboard load and native
   Capacitor exclusion. Settings → App & Hilfe offers a small Beta ansehen card
   that opens read-only instructions manually, without resetting permanent dismissal.
   The manual entry works even before the preferences API is deployed; a short
   popup status note distinguishes unknown/disabled availability. It never writes
   preferences. Disabled recruitment displays no automatic invitation.

## Ongoing operation

There are no daily approvals, email exports, allowlist uploads or notification emails.
Users join the group and test themselves. Collect genuine feedback via the existing
feedback channel and monitor actual CLOSED enrollment in Google Play. The goal is
at least 12 real testers continuously opted in for 14 days; recruit a buffer.
Internal testing does not satisfy that requirement. Link clicks, invitation dismissal
and self-confirmation must never be counted as membership, enrollment or installation.

Showing an invitation or selecting Später stores a seven-day cooldown. Nicht mehr
anzeigen stores a permanent invitation preference. The two links appear in the
dashboard invitation and the popup opened from the small App & Hilfe card.
Account export includes these preferences; account deletion removes them.
Disable recruitment with `ANDROID_BETA_ENABLED=false` if access becomes unavailable.

Google references:
- https://support.google.com/googleplay/android-developer/answer/9845334
- https://support.google.com/googleplay/android-developer/answer/14151465
