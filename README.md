# LANiS (Schulportal Hessen) functions / API

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/sph-client)](https://pypi.org/project/sph-client/)

This project allows you dynamicly accses the School Portal Hessen via python / REST API.

If you like this project maybe consider contacting me and either help contribute to add more modules or to donate your sph account temporarily for me to add more Modules

## Components

This monorepo contains the following components:

1. **sph_client** / **schulportal_hessen** This contains the functions which allow you to accses the SPH dynamicly
2. **api** this wraps all functions from sph_client into a REST API with additional features such as Cashing
3. **TUI** Contains a TUI which is currently not actively maintained

## Supported Modules:

Site note: modules are often refered as applets since the SPH is build ontop of Moodle.


- login (a simple module to login and obtain session credentials)
- benutzer (data about the user such as their age name and class)
- mein_unterricht (returns overview data about all classes the user attends)
- mein_unterricht_detailed (shows you a detailed view of a given class)
- kalender (shows you data from the build in calendar in the sph)
- nachrichten (pretty self explanitory right?)
- stundenplan (important! as of the 11.06.26 this code has been only ported from another framework called lanis_mobile (which is written in go) and hasnt been tested since i dont have accses to such a module at my school)
- lerngruppen (same here. This is a module often used by higher education institutions and has been ported without any testing!)
- school_list (provides you with a map of school names to theire ids, important for login and school selection) 


## Installation

### API Server

```bash
# Install from source
git clone https://github.com/joan-code6/lanis_api.git
pip install -r requirements.txt
```

### Python package
```bash
pip install sph_client 
```

## Quick Start

### API Server

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
## Cashing and Sessions

Fun Fact: Thanks to the cashing a api connected with a simple frontend such as lanis.arg-server.de gives you a UI which navigates waaaayyy faster then the schoolportal its self! And your phone recieves and sends waaayyy less data thanks to the api filtering out all the unimportant things and only sending the data you need!

- **Session TTL:** 1 hour inactivity timeout per session
- **Response cache:** 10 minutes for most endpoints
- **Long cache (30 days):** `/modules`, `/apps`, `/benutzer`
- **School list cache:** 2 days with 3-day auto-refresh
- **File cache:** SHA-256 hashed, stored in `data/files/`
- **Background revalidation:** stale entries are refreshed asynchronously


## Deployment

A systemd service file is provided at `lanis-api.service`:

```bash
# Deploy using the provided script
./deploy.sh

```

## AI Decleration
I use AI heavily but not irresponsibly!
I do not VibeCode as i review all code, have a good understanding of the projects structure and decide over the main aspects of the project.
