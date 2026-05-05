# sph_client API Documentation


## BASE

### __init__

Initialize the API client with a session for cookie management.

### get_apps

Retrieve available apps/modules for the logged-in user

Returns:
    Dict containing folders and available apps/entries

Example response structure:
    {
        "error": "0",
        "folders": [{"name": "Start", "logo": "fa fa-newspaper-o", "farbe": "faebcc"}, ...],
        "entrys": [{"Name": "Kalender", "Farbe": "168647", "Logo": "fa fa-calendar", ...}, ...],
        "till": 1765299123
    }

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

Returns:
    Dict with success status, calendar configuration, categories, groups, and export links.

Example:
    >>> api.kalender_get_overview()
    {'success': True, 'calendar': {'first_id': '...', 'can_write': False}, 'categories': [...]}

### kalender_get_events

Fetch calendar events using the same POST contract as the web UI.

Args:
    year: 0 for the current school year, 1 for the next school year.
    start: Calendar start mode used by the web UI.
    category: Filter by category id.
    search: Free-text search filter.
    target: Zielgruppe filter.
    view_id: Selected calendar view id. If omitted, the current default view is used.

Returns:
    Dict with success status and a normalized list of events.

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

Args:
    username: DSBmobile username or school identifier (e.g. {username}).
    password: DSBmobile password (e.g. {password}).

Returns:
    Dict with success status and session cookie data.

Example:
    >>> api.dsb_login("{username}", "{password}")
    {"success": True, "session_cookie": "{dsb_cookie}"}

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
