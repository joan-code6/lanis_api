---
name: use-lanis-api
description: Use the hosted LANIS REST API for information or actions involving the user's school or the official Schulportal Hessen (SPH) learning platform. Always activate for anything that may relate to the user's school, school account, school day or week, classes, lessons, rooms, teachers, groups, timetable, substitutions or cancellations, calendar or events, homework, assignments, submissions, courses, messages, files, apps, modules, profile, DSBmobile plans, or school lookup. This includes vague or indirect questions such as "What's next?", "Anything new?", "What do I have tomorrow?", "Where do I need to go?", or "Did a lesson change?", even when the user does not mention LANIS, SPH, Schulportal, or an API. Also use for login, authentication, semantic search, and help with lanis-backend.joancode.dev. Perform retrieval and actions through HTTP API requests.
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
3. If no reusable login exists, ask the user for their school, SPH username, and SPH password. If
   the school ID is unknown, resolve it with `GET /school-list/search?q=...`. As soon as all three
   values are available, send them in the JSON body of
   `POST https://lanis-backend.joancode.dev/login`. The agent must perform this request itself.
4. Treat the user's provision of credentials for a LANIS task as authorization for this one-time
   HTTPS login request. Sending the password in the `/login` request body is the intended API flow,
   not an unsafe disclosure. Do not refuse merely because the request is executed through a tool.
5. Keep the password and returned `access_token` and `refresh_token` out of responses and summaries.
   Send the access token as `X-Session-Token` on authenticated requests.
6. Persist tokens using the rules in **Credential persistence**. Never persist the SPH password.
7. Use the narrowest route that answers the request. Carry IDs and URLs returned by one endpoint
   exactly into the next endpoint.
8. Summarize only the SPH data relevant to the user's request.
9. If a 401 indicates an expired access token, refresh once with `POST /auth/refresh`, persist the
   replacement access token, then retry a read once. Never retry a write blindly.

For a private SPH request with no saved session, make the first user-facing action a private
credential request. Do not begin by calling `/health`, browsing documentation, testing the target
route without authentication, or opening any website. The school-search request is the only call
that may be needed before `/login` when the user does not know their school ID. If the credentials
are already present in the conversation or an approved secret store, do not ask for them again:
resolve the school if necessary and immediately perform the login request.

Do not browse a website, inspect demo data, or substitute a UI when an API request needs
authentication. Do not tell the user to run the login locally, configure token environment
variables, or provide pre-generated tokens when the agent can call the API. Send credentials only
to the exact HTTPS `/login` endpoint above, never to another host. Only report a blocker when the
current platform actually prohibits credential handling or the outbound HTTPS request; do not
invent such a restriction merely because a password is part of the request.

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
