# Public reviewer account

Select **Lanis Test Schule** (school ID `9999`), username
`Vorname.Nachname` (case insensitive), and any password. Empty passwords are
also accepted by the API. Other usernames at school `9999` are rejected locally.
All other schools retain their normal authentication.

The API returns fixed public access/refresh markers for client compatibility.
They are not JWTs, identify no stored session, never expire, and authorize only
the sandbox dispatcher. Refresh and logout work without a database. Logout and
account deletion simulate success; reviewers can always sign in again.

`dispatch` runs before account serialization, request diagnostics, authentication,
SPH clients, caching, metrics, notifications, and jobs. A recognized demo marker
never falls through to a real handler. Admin, MCP, OAuth, and agent management
are unavailable. Unimplemented reads return a sandbox 404. Normal HTTP server
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

To refresh the fixtures, install the frontend dependencies, then run from the
lanis_api checkout:

```sh
node scripts/export_frontend_demo.cjs /path/to/lanis_ui
```

This uses the frontend's existing esbuild dependency once to export JSON. The API
has no Node.js or frontend runtime dependency. Commit the refreshed JSON with any
corresponding API changes.
