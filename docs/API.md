# sph_client API Documentation


## BASE

### __init__

Initialize the API client with a session for HTTP cookie management.

Creates a new requests.Session with proper headers configured for
the Schulportal Hessen web application. Sets up default User-Agent
and Accept headers to mimic browser behavior.

The session persists cookies across requests, enabling automatic
session handling after successful login.

Note
-----
After initialization, call login() with valid credentials
to authenticate before making other API calls.

### get_apps

Retrieve available apps/modules for the logged-in user.

Fetches the user's personalized list of available modules from the
startseite.php AJAX endpoint. This includes navigation items like
Kalender, Nachrichten, Mein Unterricht, etc.

Returns
-------
Dict[str, Any]
    A dictionary containing:
    - success (bool): Whether the request succeeded
    - data (Dict): The raw response with folders and entries
      - folders: List of folder groupings with name, logo, color
      - entries: List of available apps/modules
      - till: Timestamp (cache validity)

Raises
------
RequestsException
    If the HTTP request fails.

Example
-------
>>> api.get_apps()
{'success': True, 'data': {
    'error': '0',
    'folders': [{'name': 'Start', 'logo': 'fa fa-newspaper-o', 'farbe': 'faebcc'}],
    'entrys': [{'Name': 'Kalender', 'Farbe': '168647', 'Logo': 'fa fa-calendar'}],
    'till': 1765299123
}}

### get_available_modules

Get a simplified list of available modules with their access URLs

Returns:
    List of dicts containing module name and full URL

### get_cookies

Get current session cookies

Returns:
    Dict of cookie name-value pairs

### nachrichten_get_headers

Fetch messages overview/headers (conversations list)

### nachrichten_get_conversation

Fetch messages from a specific conversation

### nachrichten_search_recipients

Search for message recipients (users)

### nachrichten_send_message

ToDo make it work

### meinunterricht_get_overview

Fetch "mein Unterricht" overview page with current entries

### meinunterricht_get_course

Fetch detailed view of a specific course/class folder

### meinunterricht_get_entry_details

Fetch details for a specific entry/link from mein Unterricht

### meinunterricht_get_weekly_view

Fetch weekly view of class entries

### meinunterricht_get_submissions

Fetch student submissions/assignments (Abgaben)

### meinunterricht_set_homework_done

Mark or unmark homework as done for a specific entry

### kalender_get_overview

Fetch and parse the calendar overview page

### kalender_get_events

Fetch calendar events using the page's getEvents action

### kalender_get_event

Fetch a single calendar event using the page's getEvent action

### benutzer_get_data

Fetch student/user data from benutzerverwaltung.php

### school_list_get_all

Fetch and parse all schools organized by district

### school_list_get_by_district

Fetch schools for a specific district

### school_list_search_by_name

Search for schools by name across all districts

### dsb_login

Login to DSBmobile to access substitution plans.

Args:
    username: DSBmobile username or school identifier (e.g. {username}).
    password: DSBmobile password (e.g. {password}).

Returns:
    Dict with success status and session cookie data.

### dsb_get_plan_urls

Fetch substitution plan iframe URLs after login.

Args:
    username: DSBmobile username or school identifier (e.g. {username}).
    password: DSBmobile password (e.g. {password}).

Returns:
    Dict with plan iframe URLs.

### dsb_get_substitution_plan

Fetch and parse the substitution plan table from DSBmobile.

Args:
    username: DSBmobile username or school identifier (e.g. {username}).
    password: DSBmobile password (e.g. {password}).
    plan_index: Which iframe plan URL to parse (default: 0).
    plan_url: Explicit plan URL to fetch (overrides plan_index).

Returns:
    Dict with the plan URL, title, parsed tables, and optional raw HTML.

### logout

Logout from Schulportal Hessen

### login

Login to Schulportal Hessen

### sid_validator

Validate a given session ID (sid)

### login_using_env

Login using set creds in .env

### close

Close the session


## LOGIN

### login_using_env

Login using credentials stored in a .env file.

Args:
    env_path: Optional path to the .env file. If omitted, looks for .env
        in the current working directory.

Returns:
    Dict with the login result or an error description.

### login

Login to Schulportal Hessen

Args:
    school_id: The school ID (e.g., "1234")
    username: Username in format firstname.lastname
    password: User password

Returns:
    Dict containing login status and any error messages

Example:
    >>> api = SchulportalHessenAPI()
    >>> result = api.login("1234", "firstname.lastname", "password123")
    >>> print(result)

### logout

Logout from Schulportal Hessen

Returns:
    Dict containing logout status

### sid_validator

Validate the current session by checking if the apps endpoint is accessible

Returns:
    Dict containing validation status


## KALENDER

### kalender_get_overview

Fetch the calendar overview page and extract its metadata.

Retrieves the calendar configuration including available
categories, groups, and user-specific settings.

Returns
-------
Dict[str, Any]
    Dictionary containing:
    - success (bool): Whether request succeeded
    - page_title (str): Page heading
    - calendar (Dict): Configuration with first_id, can_write, key, etc.
    - categories (List[Dict]): Available event categories
    - groups (List[Dict]): Available groups
    - export_links (List[Dict]): Export options (iCal, PDF, etc.)

Raises
------
RequestsException
    If the HTTP request fails.

Example
-----
>>> api.kalender_get_overview()
{'success': True, 'calendar': {'first_id': 'v-123', 'can_write': False},
 'categories': [{'id': 20, 'name': 'Sonstige Termine', 'color': '#2e2e2e'}],
 'groups': [], 'export_links': [{'label': 'als PDF', 'url': '...'}]}

### kalender_get_events

Fetch calendar events using the same POST contract as the web UI.

Retrieves calendar events with filtering options matching the SPH web
interface functionality.

Parameters
----------
year : int, optional
    School year: 0 = current year, 1 = next year.
start : str, optional
    Calendar start mode. Options: "year", "month", "week", "day".
category : str, optional
    Filter by category ID (from kalender_get_overview categories).
search : str, optional
    Free-text search filter (matches title, location, description).
target : str, optional
    Target group filter (Zielgruppe).
view_id : str, optional
    Specific calendar view ID. If omitted, uses default view.

Returns
-------
Dict[str, Any]
    Dictionary containing:
    - success (bool): Whether request succeeded
    - events (List[Dict]): Event objects with id, title, category,
      description, start, end, all_day, editable, etc.
    - count (int): Number of events returned
    - categories (List[Dict]): Available categories
    - groups (List[Dict]): Available groups
    - filters (Dict): The filters used for this query

Example
-----
>>> api.kalender_get_events(year=0, start="month", category="20")
{'success': True, 'events': [{'id': 'e-123', 'title': 'Exam', 'category': 20, ...}],
 'count': 1, 'categories': [...], 'filters': {...}}

### kalender_get_event

Fetch a single calendar event via the same `getEvent` POST action as the UI.

Args:
    event_id: Internal event id.
    view_id: Selected calendar view id. If omitted, the current default view is used.

Returns:
    Dict with success status and the parsed event payload.


## DSB

### dsb_login

Login to DSBmobile to access substitution plans.

DSBmobile (https://www.dsbmobile.de) is a separate platform
from Schulportal Hessen that provides substitution/replacement
plan information for schools.

This method establishes a separate session for DSBmobile
and stores the authentication cookies for subsequent calls.

Parameters
----------
username : str
    DSBmobile username, typically in format "{school_id}{username}"
    or just the school identifier depending on school configuration.
password : str
    DSBmobile password.

Returns
-------
Dict[str, Any]
    Dictionary containing:
    - success (bool): Whether login succeeded
    - session_cookie (str): The DSBMobile session cookie value
    - session_id (str): ASP.NET session ID
    - response_url (str): Final redirect URL

Raises
------
RequestsException
    If the HTTP request fails.

Notes
-----
DSBmobile uses different credentials than SPH. The username
is typically provided by the school administration.
This is a completely separate system from Schulportal Hessen.

Example
-----
>>> api.dsb_login("F1234", "mypassword")
{'success': True, 'session_cookie': 'abc123...', 'session_id': 'def456...'}

### dsb_get_plan_urls

Fetch plan URLs from GetData XHR endpoint after login.

Args:
    username: DSBmobile username or school identifier (e.g. {username}).
    password: DSBmobile password (e.g. {password}).

Returns:
    Dict with a list of plan URLs.

Example:
    >>> api.dsb_get_plan_urls("{username}", "{password}")
    {"success": True, "plan_urls": ["{plan_url}", ...]}

### dsb_get_substitution_plan

Fetch and parse the substitution plan table from DSBmobile.

Args:
    username: DSBmobile username or school identifier.
    password: DSBmobile password.
    plan_index: Which plan URL to parse (default: 0).
    plan_url: Explicit plan URL to fetch (overrides plan_index).
    include_raw: Include raw HTML in response.
    klasse: Filter results by class name (e.g. "05A", "10C").

Returns:
    Dict with the plan URL, title, parsed tables, and optional raw HTML.

Example:
    >>> api.dsb_get_substitution_plan("{username}", "{password}")
    {"success": True, "plan_url": "{plan_url}", "tables": [...]}

### _filter_tables_by_klasse

Filter tables to only include rows matching the given class name.
