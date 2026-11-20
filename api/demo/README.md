# Public reviewer account

Select **Lanis Test Schule** (school ID `9999`), username
`Vorname.Nachname` (case insensitive), and any password. Empty passwords are
also accepted by the API. Other usernames at school `9999` are rejected locally.
All other schools retain their normal authentication.

The API returns fixed public access/refresh markers for client compatibility.
They are not JWTs, identify no stored session, never expire, and authorize only
the sandbox dispatcher and demo agent connection management. Refresh and logout work without a database. Logout and
account deletion simulate success; reviewers can always sign in again.

`dispatch` runs before account serialization, request diagnostics, authentication,
SPH clients, caching, metrics, notifications, and jobs. A recognized demo marker
never falls through to a real school handler. Admin and hosted messaging channels
remain unavailable. Unimplemented reads return a sandbox 404. Normal HTTP server
access logging remains controlled by the server configuration; no account,
credential, session, or analytics records are created for demo requests.

Sample responses are copied from the fictional frontend demo in
`lanis_ui/src/components/demo/{demoData,mockApi}.ts`. Courses, messages, calendar,
attendance, timetable, substitutions, study groups, and storage retain those
fixtures. Dates move forward by whole weeks to preserve timetable weekdays.
The resolved timetable view uses the API’s pure resolver to project the sample
lessons for today, rolling/week mode, and requested preview parameters.
Profile fields use the public reviewer identity. Downloads return a harmless
sample text file, and app launch pages remain local.

Writes return success and the expected response fields without performing the
action. Settings updates echo the requested change but do not persist. Reloads
return the original samples, and reviewers cannot change one another's data.
Uploaded content, message bodies, push subscriptions, feedback, diagnostics, and
passwords are discarded.

The frontend demo exporter has been discontinued. Maintain `responses.json`
alongside corresponding API changes using fictional data only. The API has no
Node.js or frontend runtime dependency.

## Reviewer agent connections

Connect an external MCP agent using the normal LANIS OAuth authorization flow.
Sign in to school `9999` as `Vorname.Nachname` with any password, review the
requested permissions, and approve. Agent discovery, PKCE, consent, scoped access,
token expiry/refresh, permission changes, and revocation use the existing OAuth
implementation. The public browser token itself never authenticates MCP.

Only OAuth connection metadata and hashed authorization/access/refresh tokens
are persisted for this public demo account. These are separate from school
sessions: no school password or Schulportal session is stored or restored.
Demo tool calls reuse fictional REST samples, discard writes, and skip tool
activity/last-used tracking, school caches, background jobs, and account analytics.
Course reads retain the normal separation from grades and attendance, and
read-only connections cannot invoke write tools. Simulated write results include
`simulated: true`; later reads still return the original fixtures.

The public demo identity shares its agent connection management list between
reviewers. Do not place private information in connection names. Signal/WhatsApp
pairing, hosted schedules, and hosted run history remain disabled to avoid real
messaging or model calls. Regular accounts keep their existing behavior.
