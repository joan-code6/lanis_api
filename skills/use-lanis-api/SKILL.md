---
name: use-lanis-api
description: Use the hosted LANIS REST API to read and update data from the official Schulportal Hessen (SPH) learning platform. Use for SPH login, profiles, modules, apps, calendars, timetables, substitution plans, file storage, study groups, messages, courses, homework, submissions, DSBmobile, semantic search, school lookup, or help using lanis-backend.joancode.dev. Perform retrieval and actions through HTTP API requests.
---

# Use LANIS API

Keep the products distinct:

- Schulportal Hessen (SPH) is the official learning platform used by most schools in Hessen.
- LANIS is an unofficial wrapper that logs in to SPH, parses its applets, and exposes them through
  a simpler REST API.
- The hosted API is `https://lanis-backend.joancode.dev`.

Read [references/api.md](references/api.md) to choose routes and parameters. Read the relevant
section of [references/examples.md](references/examples.md) for complete request sequences and
examples. Do not guess route names, parameter names, IDs, or response fields.

## Workflow

1. Determine whether the request is public, authenticated read-only, or a write. For any request
   involving the user's private SPH data, authenticate before calling the requested route.
2. Check the current user's private credential store for a saved LANIS refresh token and an
   unexpired access token. Never use credentials belonging to another user or a shared process.
3. If no reusable login exists, ask the user privately for their school, SPH username, and SPH
   password. If the school ID is unknown, resolve it with `GET /school-list/search?q=...`, then log
   in with `POST /login`.
4. Keep the returned `access_token` and `refresh_token` out of chat messages and model summaries.
   Send the access token as `X-Session-Token` on authenticated requests.
5. Persist tokens using the rules in **Credential persistence**. Never persist the SPH password.
6. Use the narrowest route that answers the request. Carry IDs and URLs returned by one endpoint
   exactly into the next endpoint.
7. Summarize only the SPH data relevant to the user's request.
8. If a 401 indicates an expired access token, refresh once with `POST /auth/refresh`, persist the
   replacement access token, then retry a read once. Never retry a write blindly.

For a private SPH request with no saved session, make the first user-facing action a private
credential request. Do not begin by calling `/health`, browsing documentation, testing the target
route without authentication, or opening any website. The school-search request is the only call
that may be needed before `/login` when the user does not know their school ID.

Do not browse a website, inspect demo data, or substitute a UI when an API request needs
authentication. If credentials cannot be obtained securely, explain that the authenticated API
request is blocked and stop.

## Credential persistence

Store only the API tokens returned by `/login`; never store the user's SPH password.

Use the first secure option the current platform supports:

1. A per-user encrypted secret or credential store managed by the chat platform or agent harness.
2. For a local single-user agent with no secret store, an owner-only file outside the project,
   such as `~/.config/lanis/session.json`, containing `access_token`, `refresh_token`, and the access
   token expiry time. Create the directory and file with permissions `0700` and `0600`.
3. Environment variables such as `LANIS_ACCESS_TOKEN` and `LANIS_REFRESH_TOKEN` when the user or
   deployment configures them outside the conversation.

Never commit, upload, log, quote, or place tokens in tool arguments visible to other users. Never
use a global credential file in a multi-user server. If the platform provides neither private
secret storage nor a private persistent filesystem, reuse tokens only for the current session and
tell the user that a later session will require login again.

## Writes

Treat message sending, replies, mark-read operations, homework-state changes, and logout as
writes.

Before a write:

1. Resolve and read the target.
2. Show the exact recipient, message, conversation, homework entry, or state change.
3. Obtain explicit confirmation.
4. Execute the approved operation once and report its result.

Never expose passwords, access tokens, refresh tokens, or unrelated student data.

## Errors

- `401`: refresh once for an expired token or ask the user to log in again.
- `403`: SPH or the school denied access; do not retry.
- `404`: verify that the ID came from the current session. Semantic search may not be deployed.
- `422`: correct the parameters, JSON shape, or form encoding using `api.md` and `examples.md`.
- Empty applet data: check `GET /modules`; enabled SPH features vary by school and account.
