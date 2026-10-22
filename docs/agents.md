# External agents

LANIS exposes one provider-neutral Streamable HTTP MCP endpoint at `/mcp`.
ChatGPT uses it through a plugin; Claude uses the same endpoint through a custom
connector. Every authorization creates an independent connection, including when
the same provider connects twice. No model is run by this feature.

## User flow

1. Open Settings → Agents for connection instructions and the MCP address.
2. Add the LANIS plugin in ChatGPT, a custom connector in Claude, or a remote
   MCP server in another OAuth-capable client. Start connecting from that client.
3. The client discovers OAuth metadata, registers its callback, and opens LANIS.
4. LANIS keeps the authorization request through sign-in, shows the account,
   self-declared client name, callback domain, and requested permissions.
5. Choose Restricted (the default), Full access, or Custom, then connect or deny.
   Individual permissions appear only under Custom. The screen uses the existing
   themed LANIS icon and shows the requesting domain.
6. Return to the agent. Manage, reduce permissions, rename, or disconnect each
   connection in Settings → Agents. Access can be changed within the client’s
   original requested capability ceiling. Reconnect for capabilities the client
   did not request.

The app name is **not verified identity**. The consent screen explicitly explains
this and displays the callback domain. Creating a client named “ChatGPT” does not
make that client trusted. Public clients use dynamic registration and S256 PKCE;
no school password or LANIS session token is given to the external client.

## Configuration and deployment

Set on the API:

```dotenv
LANIS_AGENT_ISSUER=https://YOUR_PUBLIC_API_ORIGIN
LANIS_AGENT_UI_ORIGIN=https://YOUR_LANIS_UI_ORIGIN
```

These examples are configuration placeholders, not real endpoints. The issuer
must be an origin without a path, credentials, query, or fragment. Use HTTPS
outside local development. Local defaults are `http://localhost:8000` and
`http://localhost:5173`. The UI's existing `VITE_API_URL` must point at the same API.
The approval URL includes the originating issuer. When the browser is configured
for a different backend, the UI blocks consent requests and offers the existing
backend-selection flow. It does not silently change backends or send existing
credentials to the issuer in the URL. The switch health-checks the chosen server,
clears the previous login and returns to the same request for a fresh sign-in.
Backend changes in another tab reload approval; stale agent API clients reject
requests until reload. A backend URL in a query parameter is never trusted as an
API destination without the explicit selection step.

Use the same persistent `JWT_SECRET` / credential encryption configuration as the
existing API. Install `requirements.txt` (including the official MCP Python SDK).

The existing startup migration adds agent tables to the existing auth database.
Back up that database before deployment. Connections and hashed tokens persist
across API restarts. There is no separate agent database or process to restart.

The reverse proxy must route `/mcp`, `/oauth/*`, `/agents*`, and `/.well-known/*`
to this API, retain Authorization and MCP headers, support POST/GET/DELETE, and
avoid caching these routes. Do not add an interactive proxy login in front of
MCP discovery. The UI must serve its SPA on `/agents/authorize`. The issuer's
host must reach the API unchanged (or be explicitly forwarded as the public
Host) for MCP DNS-rebinding protection. Configure trusted forwarded headers for
the actual proxy; never trust arbitrary forwarded IPs. Rate limits use the
server-validated client IP, so a proxy misconfiguration can pool clients together.

Public metadata endpoints:

- `/.well-known/oauth-protected-resource/mcp` (also available at the root alias)
- `/.well-known/oauth-authorization-server`

The server requires OAuth on MCP, advertises code + refresh grants, accepts
public clients (`token_endpoint_auth_method=none`), requires S256 PKCE and state,
and checks exact registered redirects. HTTP callbacks are allowed only on
loopback. Tokens are opaque, hashed at rest, client-bound and MCP-only; normal
API session tokens cannot authenticate MCP and agent tokens cannot authenticate
normal API routes. Access tokens last at most one hour. Refresh tokens rotate,
with an absolute 90-day family lifetime. Code/refresh replay revokes the family.
Revocation and scope reductions are checked again for each tool call.

Pending browser requests expire after ten minutes; one-use codes after two
minutes. Public registration and authorization are rate-limited per network
identity; token requests and school reads have separate limits. Used refresh
tokens remain until expiry for replay detection. Expired transient records are
pruned during new authorizations/registrations. Revoked connection metadata is
retained in account export and removed on account deletion; raw tokens, passwords,
and callback state are excluded from exports. Requests that have not yet been
approved have no user association and expire automatically.

## Access presets and tools

Clients request the complete scope catalog by default. This is the ceiling, not
an automatic grant. The server derives Restricted/Full scopes itself; it does
not trust a preset's submitted checkbox list. Custom grants exactly the selected
requested scopes. A client that explicitly requests a smaller scope set cannot
receive more than that set; Full is disabled until the full catalog is requested.

- **Restricted:** ordinary read access, excluding grades, attendance, writes,
  and opening new message conversations (Schulportal marks those as read).
- **Full access:** all supported school-account reads and writes, including
  grades/attendance. No admin, credentials, account deletion, or grant-management
  tools are exposed to agents.
- **Custom:** choose each capability separately, including sensitive reads,
  message read-state changes, sending, homework, timetable overrides, submissions,
  preferences and binding elections.

| Capability | Scopes | Tools |
| --- | --- | --- |
| Connection and clock | Always | `lanis_connection` |
| Schoolwork | `schoolwork:read`, `schoolwork:write` | Courses, paginated homework/lessons, study groups; change homework completion |
| Timetable | `timetable:read`, `timetable:write` | Raw plan and substitutions; personal lesson overrides |
| Calendar | `calendar:read` | Events for an explicit year |
| Messages | `messages:read`, `messages:mark-read`, `messages:write` | Headers and cached content; open/mark read; recipients, send and reply |
| Files | `files:read` | Browse/search Dateispeicher, read files up to 2 MiB, list distributed files |
| Submissions | `submissions:read`, `submissions:write` | List/detail, upload files up to 2 MiB, delete submitted files |
| Elections | `elections:read`, `elections:write` | Discover forms/options; submit confirmed selections |
| Profile | `profile:read` | School profile |
| Sensitive records | `grades:read`, `attendance:read` | Course marks and attendance (separate opt-ins) |
| Preferences | `preferences:read`, `preferences:write` | Read/patch personal display preferences |

Schoolwork responses always exclude grades and attendance, even under Full;
these have their own tools and grants. Timetable reads do not use homework
enrichment. `lanis_message` only reads the existing conversation cache; a cache
miss returns instructions instead of making an upstream request. The separately
permitted `lanis_open_message` discloses its read-state effect and has
`readOnlyHint=false`. Restricted therefore cannot silently clear unread reminders.

Discovery lists only tools allowed by the current grant. Direct calls also check
the required scope. Mutating tools have write annotations; file deletion and
binding elections are marked destructive. Agents are instructed to perform
writes only on user request and avoid automatic retries of ambiguous sends,
uploads and submissions. Election submission requires an explicit confirm flag.
External content is untrusted data, never instructions. IDs must come from read
tools, dates use Europe/Berlin, and school outages are not empty results.

OAuth consent uses the middleware's existing shared lifecycle lease; it never
upgrades it to an exclusive lease. Agent code exchange and refresh check account
existence through their existing SQLite transaction, without invoking session
cleanup on a second connection. Tests cover real signed browser sessions and an
expired session belonging to another account.

## ChatGPT plugin

`plugins/lanis/plugin.json` is the portable plugin source. After the API and UI
are deployed, build a **private** package using the verified public issuer:

```sh
python scripts/build_agent_plugin.py --issuer https://YOUR_PUBLIC_API_ORIGIN --output /tmp/lanis-plugin.zip
```

The builder verifies live OAuth discovery and writes `plugin.json` and `mcp.json`
with Streamable HTTP, OAuth dynamic registration, the existing LANIS icon, and
the full advertised scope ceiling. Users still default to Restricted at consent.
The package contains no user credentials or app IDs.
Building it does not upload, install, publish, or deploy anything. A public
listing still needs publisher details, privacy review, and platform submission;
account/workspace availability of custom plugins varies. Claude can connect to
the public MCP URL directly without installing this ChatGPT package.

Official references: [ChatGPT MCP plugins](https://developers.openai.com/plugins/build/mcp-server),
[plugin authentication](https://developers.openai.com/plugins/build/auth),
[Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
[MCP authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization).

## Local verification

```sh
python -m pytest tests/test_agents.py tests/test_account_data.py tests/test_auth_identity.py tests/test_account_session_revocation.py -q
```

The tests use fake accounts in temporary SQLite databases. They exercise real
OAuth HTTP endpoints and the real MCP transport, scope discovery, direct-call
permission checks, token replay, expiry, account isolation/deletion, independent
agents, and data projection. They do not contact Schulportal.

The UI demo at `/demo/settings/agents` has isolated sample connections for
interaction testing. It never creates a real authorization. Browser testing of
approval should also cover sign-in, reload, permission changes, rejection,
expired requests, small screens, dark mode, and return to the requesting client.
