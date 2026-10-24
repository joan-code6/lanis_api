# Private read-only account previews

The admin portal opens the real LANIS UI with a separate tab-scoped session.
This is privileged access to personal school data. Only configured, authenticated
administrators can request it; each launch attempt is recorded in the admin audit.
The private broker is an additional operational boundary, not a secret URL or a
replacement for authentication. Its source can safely be public.

## Deployment (separate, explicit production action)

Deploy the matching `lanis_ui` and `lanis_admin` changes first. The admin portal's
`VITE_LANIS_UI_URL` defaults to `https://lanis.arg-server.de`; local development
can set it to `http://localhost:5173`. The UI uses its build-configured
`VITE_API_URL`, never a backend chosen by the launch URL or a normal tab's custom
backend setting.

Install the backend requirements, including httpx. Add the following **user
service** alongside the existing API service, adjusting paths to the deployment.
Do not expose its socket through nginx, a tunnel, or a TCP listener.

```ini
[Unit]
Description=LANIS private impersonation broker
After=network.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/lanis
EnvironmentFile=/absolute/path/to/lanis/.env
Environment=LANIS_IMPERSONATION_SOCKET=%t/lanis-impersonation/broker.sock
RuntimeDirectory=lanis-impersonation
RuntimeDirectoryMode=0700
UMask=0077
ExecStart=/absolute/path/to/lanis/.venv/bin/uvicorn api.impersonation_broker:app --uds %t/lanis-impersonation/broker.sock --no-access-log
Restart=on-failure
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Set `LANIS_IMPERSONATION_SOCKET` on the existing API service to the same absolute
socket path (normally `/run/user/<uid>/lanis-impersonation/broker.sock`). Both
processes must use the same working directory, auth database, admin JWT secret,
admin allowlist and metrics database. The private app needs local database
access; it does not read SPH passwords or issue normal user tokens. Unix-user
compromise remains outside the protection offered by the socket. The broker
must not be hosted as a publicly accessible second API.

An unset socket disables exchange and creation. A stopped broker prevents new
launches; existing previews last until expiry or revocation. Removing the socket
configuration disables existing previews too. No production service is enabled
or restarted by this change.

## Protocol and limits

- `POST /admin/impersonation/launch` requires the existing revocable admin JWT.
  It forwards that JWT to `/launch` over the Unix socket; there is no in-process
  fallback. The broker rechecks admin authentication and audits before issuing.
- The returned random code expires after 45 seconds, is stored only as SHA-256,
  and can be consumed once atomically across API workers. The admin opens it in
  the URL fragment, with no opener relationship. Neither reusable session
  tokens nor passwords are included in URLs.
- `POST /impersonation/exchange` consumes the code and returns a non-refreshable
  restricted access token. Exchange and launch responses use `Cache-Control:
  no-store`. The UI removes the fragment before making requests.
- Previews last at most 15 minutes from creation. A maximum of five unexpired
  grants per administrator prevents accidental launch storms. Expired records
  are pruned by the broker; audit entries follow existing admin audit retention.
- Every authenticated preview request checks the grant, target session,
  administrator allowlist and administrator session. Ending a preview deletes
  only that grant; it never logs the student out. Target logout/session revocation
  and admin session revocation invalidate access. No new user refresh credential
  is created or exposed.
- `POST /impersonation/end` revokes the preview. The UI also clears preview
  storage on expiry, authentication loss or explicit end. Closing the tab clears
  its browser session storage; the server grant expires independently.

## Read-only scope

An explicit method/path allowlist in `api/impersonation.py` is enforced before
route dispatch, including routes bypassing ordinary account middleware.
New routes default to denied. Normal account reads remain available, including
school modules, profile, timetable, calendar, course overview, attendance,
preferences and file listings. Writes, exports, downloads, election forms,
video joins and message bodies are denied. Raw `/apps` and arbitrary `/meinunterricht/entry` page fetches are also denied.
Module discovery builds fresh metadata without following or probing launch links;
it returns only canonical native module paths and no external launch URLs. Both
normal module caches and outage snapshots are bypassed for previews so existing
OAuth callbacks cannot leak and preview responses cannot replace normal snapshots.
Message headers remain visible;
message bodies are excluded because reading can mark a message as read.
Dashboard notification aggregation is excluded because it updates persisted
notification state. Some panels therefore intentionally report unavailability.

Read-only concerns the account's actions, not network access: ordinary reads
can restore an SPH connection and update backend caches. Preview activity does
not count as normal authenticated activity in `client_dependency`.

The UI routes all app local-storage access through `appStorage`: normal sessions
continue using localStorage; previews use namespaced sessionStorage. Preview
tabs do not restore the operator's account caches or custom backend, refresh
sessions, register push, unsubscribe normal push, or register a service worker.
This is storage isolation, not protection against same-origin malicious script.

## Validation

Run `python -m pytest tests/test_impersonation.py tests/test_admin.py
 tests/test_account_session_revocation.py tests/test_auth_session_recovery.py`.
Use the UI storage and browser tests alongside both frontend production builds.
No live student account or production deployment is needed for these checks.
