# LANiS (Schulportal Hessen) API

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/sph-client)](https://pypi.org/project/sph-client/)

Unofficial Python client and REST API integrations for Schulportal Hessen (SPH).
The PyPI package contains the reusable Python client; the repository also includes
the optional hosted API server and a terminal UI.

The client is actively developed against the portal's web interfaces. Portal
changes can occasionally require a new package release.

## Live Server
The hosted API is available at
[lanis-backend.joancode.dev](https://lanis-backend.joancode.dev/), with interactive
documentation at [/documentation](https://lanis-backend.joancode.dev/documentation).
<img width="1249" height="937" alt="image" src="https://github.com/user-attachments/assets/8fef241f-3cd5-432b-bc01-5f85b8d6efab" />


## Components

This monorepo contains the following components:

1. **sph_client** / **schulportal_hessen** contains the reusable Python client
2. **api** wraps the client in a REST API with caching and additional services
3. **TUI** contains a terminal interface that is currently not actively maintained

## Supported Modules:

The portal modules are referred to as applets because SPH is built on top of Moodle.


- `login` — authenticate and manage session credentials
- `benutzer` — user profile and class information
- `mein_unterricht` — courses, content, assignments, and attachments
- `kalender` — calendar events
- `nachrichten` — conversations, recipients, sending, and replies
- `stundenplan` — timetable data
- `lerngruppen` — study groups
- `school_list` — school names and IDs for login and school selection


## Installation

### API Server

```bash
# Install from source
git clone https://github.com/joan-code6/lanis_api.git
pip install -r requirements.txt
```

### Python package
```bash
python -m pip install sph-client
```

## Quick Start

### API Server

Create a `.env` file with a stable, private signing key before the first start:

```dotenv
JWT_SECRET=replace-this-with-a-long-random-value
```

Keep that value unchanged across deployments; changing it invalidates issued
access tokens. The server refuses to start without it instead of silently using
an ephemeral key.

```bash
uvicorn api.api:app
```

The API is available at port `8000` with interactive docs at `/docs`.

### Python package


```python
from sph_client import SchulportalHessenAPI

api = SchulportalHessenAPI()

# Login
result = api.login("1234", "username", "password")
if result.get("success"):
    # Get available modules
    modules = api.get_available_modules()
    print(modules)

    # Fetch messages
    headers = api.nachrichten_get_headers()
    print(headers)

    # Get calendar events
    events = api.kalender_get_events()
    print(events)

    api.logout()
```
## Caching and sessions

The hosted API uses caching and persistent sessions to reduce portal requests and
keep navigation responsive.

- **Session TTL:** 1 hour inactivity timeout per session
- **Response cache:** 10 minutes for most endpoints
- **Long cache (30 days):** `/modules`, `/apps`, `/benutzer`
- **School list cache:** 2 days with 3-day auto-refresh
- **File cache:** SHA-256 hashed, stored in `data/files/`
- **Background revalidation:** stale entries are refreshed asynchronously

## Push notifications

Authenticated users can opt in to daytime polling for new messages and Vertretungsplan entries from the Lanis UI settings. The default polling window is 07:00–21:00 in the user's configured timezone, with a 15-minute interval. Each category creates its own baseline before sending notifications, so enabling it does not alert on existing content. Vertretungsplan notifications default to the user's own class and can instead target selected classes or the full plan.

Web Push requires these environment variables on the API server:

- `VAPID_PUBLIC_KEY`
- `VAPID_PRIVATE_KEY`
- `VAPID_SUBJECT` (for example, `mailto:admin@example.org`)

The `pywebpush` dependency is included in `requirements.txt`. Users can configure notification categories, Vertretungsplan class scope, the active window, interval, timezone, and whether message previews contain the sender and subject in **Settings → Benachrichtigungen**.


## Deployment

A systemd service file is provided at `lanis-api.service`:

```bash
# Deploy using the provided script
./deploy.sh
```

The script can be run as a regular user. It requests `sudo` only when installing
or restarting the systemd service.

### Docker

Docker Compose runs the API on host port `9898` and stores its databases and
file cache in the persistent `lanis-data` volume. This is independent from the
existing systemd deployment.

Create a local `.env` before starting the container. At minimum it must contain
a stable `JWT_SECRET`; the same file can provide `VAPID_*`, `DSB_*`,
`PUBLIC_BASE_URL`, and optional AI settings. The file is passed to the container
at runtime and excluded from the image build context.

```bash
./deploy_docker.sh
```

Set `LANIS_DOCKER_BIND_ADDRESS` or `LANIS_DOCKER_PORT` in `.env` to override the
default `0.0.0.0:9898` publication. Install the nightly Docker deployment with:

```bash
./deploy_docker.sh --install-cron
```



## API reference

See the generated [Python API reference](docs/API.md) for the complete client
surface and method documentation.

## License

This project is released under the [MIT License](LICENSE).
