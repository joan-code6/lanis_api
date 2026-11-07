# Status and account preferences investigation

Read-only investigation on 2026-11-07; no production changes or restarts.

## Evidence and causes

The latest 2,000 production monitor observations contained 1,838 up, 110
recovery_pending and 52 confirmed degraded observations. Of those 52, 30 had
one login_failed account, one modules_request_failed account and two healthy
accounts. The other 22 had two login_failed accounts and two healthy accounts.
These are sampled account observations, not proof of a global SPH outage.
No account identifiers, credentials or raw response payloads are included here.

The monitor resolved every app redirect before opening modules again, including
external apps. This creates unnecessary traffic and can consume the worker's
bounded runtime. Monitoring now skips redirect resolution and stops on the first
successful module. Internal SDK exceptions now produce unknown, not downtime.
Unrelated feature failures no longer corroborate a new shared incident.

The UI sends hex appearance colors and secondary_theme_color, while the API
accepted named colors only and silently discarded the secondary color. The UI
also sends homework.hide_completed_in_overview, which the API discarded. The
load catch includes the migration PATCH, explaining a 422 reported as a loading
failure. The API now accepts validated hex colors and persists both UI fields,
with compatibility for named colors and older homework display preferences.

## Validation and limits

Regression checks cover HTTP preferences save/reload, partial updates retaining
colors, invalid colors, monitor internal errors, independent account failures,
and skipping external redirect resolution. No production authenticated request
was replayed. Existing historical database records are not rewritten. Network
errors remain observations from one monitoring host; shared failures alone do
not establish SPH-wide impact. Reduced probe traffic is verified structurally,
not by a production latency benchmark.
