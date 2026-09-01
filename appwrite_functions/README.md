# LANiS Appwrite Functions

This directory deploys the repository's complete FastAPI service on Appwrite
Functions. Run the Appwrite CLI from this directory so it discovers
`appwrite.config.json`:

```sh
cd appwrite_functions
appwrite push all --all
```

The bundle contains three Python Functions:

| Function | Entry point | Trigger | Purpose |
| --- | --- | --- | --- |
| `lanis-api` | `appwrite_functions/src/fastapi_main.py` | HTTP | Every FastAPI route through an ASGI-to-Functions adapter |
| `lanis-worker` | `appwrite_functions/src/worker.py` | async + minutely cron | Profile metrics, course files, cache refresh, and notification polling |
| `lanis-dsb-snapshot` | `src/dsb_snapshot.py` | UTC daily cron | Fetches and persists DSBmobile substitution snapshots |

Copy `.env.example` to a secret-managed Function environment. Appwrite
Functions provide a per-execution key in `x-appwrite-key`; a local
`LANIS_APPWRITE_API_KEY` is only a development fallback. Set
`LANIS_APPWRITE_ENCRYPTION_KEY` to a random value of at least 32 characters.
SPH passwords and pending file URLs are encrypted before they enter TablesDB.

`appwrite.config.json` provisions private TablesDB tables for encrypted SPH
credentials, response caches, user preferences/overrides/notifications,
profile metrics, DSB snapshots, and file metadata. Course attachments live in
the encrypted `lanis-files` Storage bucket. Appwrite Auth is authoritative:
`/login` returns a short-lived custom token, the browser exchanges it for an
Appwrite session and sends a freshly minted Appwrite JWT as `X-Session-Token`.

Before deploying, replace the project/region placeholders in the config and
set the same resource IDs in the Function environment. The scheduled Function
uses UTC (`0 15 * * *`). Configure one DSB credential set with `DSB_*`, or a
JSON list in `LANIS_DSB_SCHOOLS` for multiple schools. Never commit secrets.

The Function source is the whole backend repository, so deployed behavior uses
the same `api/` and `schulportal_hessen/` code as local development. For local
smoke tests from the repository root:

```sh
python -m compileall -q appwrite_functions/src
python -m pytest -q appwrite_functions/tests
```
