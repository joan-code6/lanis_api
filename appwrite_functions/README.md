# LANiS Appwrite Functions

This directory is a standalone Appwrite deployment bundle. The repository's
FastAPI `api/` service is intentionally not imported or modified. Run the
Appwrite CLI from this directory so it discovers `appwrite.config.json`:

```sh
cd appwrite_functions
appwrite push all --all
```

The bundle contains three Python Functions:

| Function | Entry point | Trigger | Purpose |
| --- | --- | --- | --- |
| `lanis-api` | `src/main.py` | HTTP | Health, login/refresh/logout, public school lookup, and protected LANiS operations |
| `lanis-worker` | `src/worker.py` | asynchronous execution | Profile metrics, course-file downloads, and cache revalidation |
| `lanis-dsb-snapshot` | `src/dsb_snapshot.py` | UTC daily cron | Fetches and persists DSBmobile substitution snapshots |

Copy `.env.example` to a secret-managed Function environment. Appwrite
Functions provide a per-execution key in `x-appwrite-key`; a local
`LANIS_APPWRITE_API_KEY` is only a development fallback. Set
`LANIS_APPWRITE_ENCRYPTION_KEY` to a random value of at least 32 characters.
SPH passwords and pending file URLs are encrypted before they enter TablesDB.

`appwrite.config.json` provisions one TablesDB database with private tables for
refresh tokens, response cache, profile metrics, DSB snapshots, and file
metadata, plus a private Storage bucket for downloaded course files. Keep all
table and bucket permissions server-only; the HTTP Function is the policy
boundary. The HTTP response retains the existing `X-Session-Token` contract,
and additionally returns an Appwrite custom token for clients that want to
exchange it for an Appwrite Auth session. Protected requests may use either
that LANiS token or an authenticated Appwrite Function execution
(`x-appwrite-user-id`).

Before deploying, replace the project/region placeholders in the config and
set the same resource IDs in the Function environment. The scheduled Function
uses UTC (`0 15 * * *`). Configure one DSB credential set with `DSB_*`, or a
JSON list in `LANIS_DSB_SCHOOLS` for multiple schools. Never commit secrets.

The source depends on the published `sph-client` package because Appwrite
bundles only this directory. For local smoke tests from the repository root:

```sh
python -m compileall -q appwrite_functions/src
python -m pytest -q appwrite_functions/tests
```
