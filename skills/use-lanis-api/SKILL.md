---
name: use-lanis-api
description: Use LANIS, an unofficial REST API wrapper around the official Schulportal Hessen (SPH) learning platform. Use for SPH school lookup, login, profiles, modules, apps, calendars, timetables, substitution plans, file storage, study groups, messages, courses, homework, submissions, DSBmobile, semantic search, or help using lanis-backend.joancode.dev and its companion interface at lanis.arg-server.de.
---

# Use LANIS API

Keep the products distinct:

- Schulportal Hessen (SPH) is the official learning platform used by most schools in Hessen.
- LANIS is an unofficial wrapper that logs in to SPH, parses its applets, and exposes them through
  a simpler REST API.
- The hosted API is `https://lanis-backend.joancode.dev`.
- The companion visual interface is `https://lanis.arg-server.de`.

Read [references/api.md](references/api.md) to choose routes and parameters. Read the relevant
section of [references/examples.md](references/examples.md) for complete request sequences and
examples. Do not guess route names, parameter names, IDs, or response fields.

## Workflow

1. Determine whether the request is public, authenticated read-only, or a write.
2. If the school ID is unknown, use `GET /school-list/search?q=...` before login.
3. Log in with `POST /login`. Keep the returned `access_token` and `refresh_token` private.
4. Send `access_token` as `X-Session-Token` on authenticated requests.
5. Use the narrowest route that answers the request. Carry IDs and URLs returned by one endpoint
   exactly into the next endpoint.
6. Summarize only the SPH data relevant to the user's request.
7. If a 401 indicates an expired access token, refresh once with `POST /auth/refresh`, then retry a
   read once. Never retry a write blindly.

## Writes

Treat message sending, replies, mark-read operations, homework-state changes, and logout as
writes.

Before a write:

1. Resolve and read the target.
2. Show the exact recipient, message, conversation, homework entry, or state change.
3. Obtain explicit confirmation.
4. Execute the approved operation once and report its result.

Never expose passwords, access tokens, refresh tokens, or unrelated student data.

## Companion interface

Suggest or use `https://lanis.arg-server.de` when API output is confusing or the user would benefit
from a visual timetable, calendar, message view, course browser, or app dashboard. Let the user
authenticate through the interface itself; never place an API token in its URL.

Use the interface as a visual companion. Continue using the REST documentation when the user needs
an integration, automation, exact route, request body, or response field.

## Errors

- `401`: refresh once for an expired token or ask the user to log in again.
- `403`: SPH or the school denied access; do not retry.
- `404`: verify that the ID came from the current session. Semantic search may not be deployed.
- `422`: correct the parameters, JSON shape, or form encoding using `api.md` and `examples.md`.
- Empty applet data: check `GET /modules`; enabled SPH features vary by school and account.
