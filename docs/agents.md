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
5. Select permissions, optionally give this connection a name, and allow or deny.
6. Return to the agent. Manage, reduce permissions, rename, or disconnect each
   connection in Settings → Agents. Additional permissions require fresh consent.

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

## Tools and permissions

| Permission | Tools | Notes |
| --- | --- | --- |
| Connection only | `lanis_connection` | Granted permissions, Berlin clock, settings URL, next steps |
| `schoolwork:read` | `lanis_courses`, `lanis_course` | Discover IDs, then read paginated lesson/homework entries; grades and attendance excluded |
| `timetable:read` | `lanis_timetable`, `lanis_substitutions` | Raw plan without homework enrichment; substitutions can contain multiple classes |
| `calendar:read` | `lanis_calendar` | Explicit year; resolve relative dates with the Berlin clock |
| `messages:read` | `lanis_messages`, `lanis_message` | Separate consent, discover conversation IDs before fetching content |

MCP discovery lists only tools allowed by the current grant. Calling a hidden
tool still checks permission. All tools are annotated read-only, idempotent,
non-destructive, and dependent on external school data. There are no arbitrary
URL, request-proxy, admin, account-deletion, sending, or upload tools.

Tool descriptions explain IDs, dates, pagination, error handling, and next steps.
Initialization instructs agents to treat school content as untrusted data,
never invent IDs, never ask for credentials in chat, check `success`, and not
interpret outages as empty results. Permission errors tell the agent to reconnect
for consent; session errors ask for LANIS sign-in; transient errors ask for retry.

## ChatGPT plugin

`plugins/lanis/plugin.json` is the portable plugin source. After the API and UI
are deployed, build a **private** package using the verified public issuer:

```sh
python scripts/build_agent_plugin.py --issuer https://YOUR_PUBLIC_API_ORIGIN --output /tmp/lanis-plugin.zip
```

The builder verifies live OAuth discovery and writes `plugin.json` and `mcp.json`
with Streamable HTTP, OAuth dynamic registration, and default schoolwork,
timetable, and calendar scopes. Messages are optional and require a client to
request `messages:read`. The package contains no user credentials or app IDs.
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
