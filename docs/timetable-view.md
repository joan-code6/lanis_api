# Resolved timetable

`GET /stundenplan/view` returns a timetable ready for display, using the same authenticated session as other endpoints. It adds dated DSB and native Schulportal changes after projecting the recurring timetable and applying account custom lessons. The existing `/stundenplan` contract remains available for settings and other consumers.

Query parameters:

| Parameter | Values | Default |
| --- | --- | --- |
| `view_mode` | `rolling`, `week` | `rolling` |
| `plan_mode` | `personal`, `all` | `personal` |
| `week_type` | `A`, `B` | Actual week |
| `refresh` | boolean | `false` |

Dates use Europe/Berlin. Rolling view returns the weekdays in the next seven calendar days, including today. Week view advances to next week on weekends. A/B previews for a different week type never inherit the actual date's substitutions.

The response includes `success`, `days`, `week_start`, `active_week`, `has_alternating_weeks`, `time_slots`, `exams`, `exams_error`, and `substitution_sources`. Each day has `date`, `name`, `lessons`, and `substitutionNotices`. Lesson fields use the UI names (`subject`, `period`, `start_time`, `end_time`, etc.). Changed lessons additionally include `cancelled`, `substitution`, and `original_lesson`. Times are formatted as `HH:mm`; periods can be numbers or ranges. Source records contain `name`, `error`, and optional `updated` timestamps. Clients should display source errors even if the base timetable succeeds.

Matching uses exact dates and normalized class membership, plus subject, teacher, and course identifiers. Upper-school year groups require teacher/course evidence. Ambiguous, conflicting, or unmatched reports remain visible as notices. Changes affecting only part of a double lesson split it using school period boundaries. Recurring templates, course links, homework, and input cache records are preserved.

## Caching and DSB configuration

The view reuses existing per-user `/stundenplan`, `/modules`, `/benutzer`, and `/vertretungsplan` response caches. Projection itself is deliberately not cached again: refreshing a source or invalidating an account timetable must be reflected immediately, without a second aggregate TTL. `refresh=true` bypasses timetable and substitution response caches; module and profile caches retain their existing policies.

`GET /dsb/school-plan` returns the configured school's DSB plan, without requiring credentials from the browser. It supports `refresh=true` and is also used by the dedicated DSB page. Successful plans share the existing ten-minute response cache across authenticated users of the same school. Concurrent cache misses are serialized. Cache keys include school and a configuration digest; other schools cannot consume that plan. Failed fetches are not cached.

`DSB_SCHOOL_ID`, `DSB_USERNAME`, and `DSB_PASSWORD` configure the school account. The existing 5201 integration is retained as the default during migration. A different school requires all three variables. An unconfigured school receives `success=false`, empty `tables`, and an error. No DSB credentials are returned to the frontend.

## Validation

Run `python -m pytest tests/test_timetable_substitutions.py tests/test_api_timetable_cache.py tests/test_timetable_enrichment.py tests/test_user_overrides.py tests/test_dsb.py tests/test_vertretungsplan.py` in the development environment. An optional real-data test accepts `LANIS_LIVE_DSB` and `LANIS_LIVE_TIMETABLE` paths to private JSON captures for the September 9, 2026 scenario. Keep captures and authenticated test credentials outside the repository.
