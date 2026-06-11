# Unofficial LANiS (Schulportal Hessen) API

This repository provides:

- a Python client (`sph-client`) for Schulportal Hessen
- a FastAPI server wrapper around the client
- optional tooling for TUI usage and DSB/substitution-plan workflows

> This project is unofficial and not affiliated with Schulportal Hessen.

## Features

- Login/session handling for Schulportal Hessen
- Access to core portal areas (apps, profile, calendar, messages, classes, timetable, file storage, school list)
- DSBmobile substitution plan helpers
- FastAPI endpoints for browser/app integrations
- Generated API docs:
  - `/docs/API.md` (main generated package documentation)
  - `/api-documentation.md` (extended generated reference output in repo root)

## Installation

```bash
pip install -e .
```

For development (includes `pytest` and `ruff`):

```bash
pip install -e .[dev]
```

## Python client quick start

```python
from sph_client import SchulportalHessenAPI

api = SchulportalHessenAPI()
login_result = api.login("<school_id>", "<username>", "<password>")

if login_result.get("success"):
    modules = api.get_available_modules()
    print(modules)

api.logout()
api.close()
```

## Run the FastAPI server

From the repository root (after installing dependencies):

```bash
uvicorn api.api:app --reload
```

Default URL: `http://localhost:8000`

Useful endpoints:

- `GET /health`
- `POST /login`
- `POST /logout`
- `GET /apps`
- `GET /modules`
- `GET /benutzer`
- `GET /kalender`, `GET /kalender/events`
- `GET /nachrichten/headers`
- `GET /meinunterricht`
- `GET /school-list`
- `POST /dsb/login`, `POST /dsb/plan`

## API testing

Integration tests are in `/api-tests.py` and follow the project convention for API endpoint checks. They require a running API server and credentials via environment variables:

- `LANIS_API_URL`
- `LANIS_API_SCHOOL_ID`
- `LANIS_API_USERNAME`
- `LANIS_API_PASSWORD`

Run:

```bash
pytest api-tests.py
```

## Repository structure

- `/schulportal_hessen` – core client implementation
- `/api` – FastAPI server and related utilities
- `/docs` – generated package docs
- `/tui` – terminal UI client
- `/scripts` – helper scripts (including DSB tracking)

## License

MIT
