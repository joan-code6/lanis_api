import requests
import threading
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import json

from schulportal_hessen.tools.cryptor import Cryptor


class SchulportalHessenAPI:
    """
    API Client for Schulportal Hessen (SPH) - Unofficial Python API Wrapper

    This class provides a programmatic interface to interact with the official
    Schulportal Hessen (https://schulportal.hessen.de), the Hessian School Portal
    used by schools in the German state of Hesse.

    The API enables automated access to:
    - User authentication and session management
    - Message exchange (Nachrichten)
    - Calendar events (Kalender)
    - Course materials and assignments (Mein Unterricht)
    - User profile data (Benutzerverwaltung)
    - School directory lookups
    - DSBmobile substitution plans

    Authentication is handled via Schulportal credentials (school ID + username + password).
    The API manages HTTP session cookies and supports optional end-to-end
    encryption for message handling.

    Attributes
    ----------
    school_id : str, optional
        The Schul-ID (school identifier) for the logged-in user.
    logged_in : bool
        Whether the user is currently authenticated.
    cryptor : Cryptor, optional
        Encryption handler for encrypted communications.
    dsb_session : requests.Session, optional
        Separate session for DSBmobile substitution plans.

    Base URLs
    ----------
    - Login: https://login.schulportal.hessen.de
    - Start/Content: https://start.schulportal.hessen.de

    Example
    ----------
    >>> api = SchulportalHessenAPI()
    >>> result = api.login("{school_id}", "{username}", "{password}")
    >>> if result["success"]:
    ...     messages = api.nachrichten_get_headers()
    ...     events = api.kalender_get_events()
    ...     api.logout()
    """

    BASE_LOGIN_URL = "https://login.schulportal.hessen.de"
    BASE_START_URL = "https://start.schulportal.hessen.de"

    def __init__(self):
        """Initialize the API client with a session for HTTP cookie management.

        Creates a new requests.Session with proper headers configured for
        the Schulportal Hessen web application. Sets up default User-Agent
        and Accept headers to mimic browser behavior.

        The session persists cookies across requests, enabling automatic
        session handling after successful login.

        Note
        -----
        After initialization, call login() with valid credentials
        to authenticate before making other API calls.
        """
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self.school_id: Optional[str] = None
        self.logged_in = False
        self.cryptor: Optional[Cryptor] = None
        self.dsb_session: Optional[requests.Session] = None
        self.dsb_logged_in: bool = False
        self.dsb_plan_urls: List[str] = []
        self._dsb_lock = threading.RLock()

    def get_apps(self) -> Dict[str, Any]:
        """Retrieve available apps/modules for the logged-in user.

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
        """
        if not self.logged_in:
            return {"success": False, "error": "Not logged in. Please login first."}

        try:
            apps_url = f"{self.BASE_START_URL}/startseite.php?a=ajax&f=apps"

            response = self.session.get(apps_url)
            response.raise_for_status()

            data = response.json()
            return {"success": True, "data": data}

        except requests.RequestException as e:
            return {"success": False, "error": f"Failed to retrieve apps: {str(e)}"}
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Failed to parse response: {str(e)}"}

    def get_available_modules(self) -> List[Dict[str, Any]]:
        """Return the logged-in user's available modules with resolved URLs.

        This helper reads the raw app list returned by :meth:`get_apps` and
        normalizes each entry into a compact structure that is easier to use in
        client code. Relative links are converted to absolute URLs.

        Returns
        -------
        List[Dict[str, str]]
            A list of modules containing:
            - name: Display name of the module
            - url: Absolute access URL
            - color: Module color value from the portal
            - logo: Icon class for the module
            - folders: Folder/group metadata attached to the module
            - target: Link target, usually "_self"
            - usable: Whether the module is supported by this package
            - usage: List of API method names to use with the module

        Notes
        -----
        If the user is not logged in or the app list cannot be loaded, an
        empty list is returned.
        """
        apps_data = self.get_apps()

        if not apps_data.get("success"):
            return []

        modules: List[Dict[str, Any]] = []
        entries = apps_data.get("data", {}).get("entrys", [])

        usage_by_link = {
            "kalender.php": [
                "kalender_get_overview",
                "kalender_get_events",
                "kalender_get_event",
            ],
            "nachrichten.php": [
                "nachrichten_get_headers",
                "nachrichten_get_conversation",
                "nachrichten_search_recipients",
                "nachrichten_send_message",
                "nachrichten_reply_message",
            ],
            "meinunterricht.php": [
                "meinunterricht_get_overview",
                "meinunterricht_get_course",
                "meinunterricht_get_entry_details",
                "meinunterricht_get_weekly_view",
                "meinunterricht_get_submissions",
                "meinunterricht_set_homework_done",
                "meinunterricht_download_file",
            ],
            "vertretungsplan.php": ["vertretungsplan_get_plan"],
            "stundenplan.php": ["stundenplan_get_plan"],
            "dateispeicher.php": [
                "dateispeicher_get_root",
                "dateispeicher_get_node",
                "dateispeicher_search_files",
            ],
            "lerngruppen.php": ["lerngruppen_get_overview"],
            "benutzerverwaltung.php": ["benutzer_get_data"],
        }

        for entry in entries:
            link = entry.get("link", "")
            usage = []
            for link_key, methods in usage_by_link.items():
                if link_key in link:
                    usage = methods
                    break
            usable = len(usage) > 0
            # Convert relative links to absolute URLs
            if link.startswith("http"):
                full_url = link
            else:
                full_url = f"{self.BASE_START_URL}/{link}"

            modules.append(
                {
                    "name": entry.get("Name"),
                    "url": full_url,
                    "color": entry.get("Farbe"),
                    "logo": entry.get("Logo"),
                    "folders": entry.get("Ordner", []),
                    "target": entry.get("target", "_self"),
                    "usable": usable,
                    "usage": usage,
                }
            )

        return modules

    def get_cookies(self) -> Dict[str, str]:
        """Return the current session cookies as a plain dictionary.

        Returns
        -------
        Dict[str, str]
            Mapping of cookie names to values for the active HTTP session.
        """
        cookies_dict = {}
        for cookie in self.session.cookies:
            cookies_dict[cookie.name] = cookie.value
        return cookies_dict

    # Message/Nachrichten methods
    def nachrichten_get_headers(
        self, get_type: str = "All", last: int = 0
    ) -> Dict[str, Any]:
        """Fetch the conversation list for the authenticated user.

        Args:
            get_type: Message filter. Common values are "All", "visibleOnly",
                and "unvisibleOnly".
            last: Pagination marker used for incremental fetches.

        Returns:
            Dict containing the decrypted conversation list and the reported
            total count.
        """
        ...

    def nachrichten_get_conversation(
        self, conversation_id: str, last: int = 0
    ) -> Dict[str, Any]:
        """Fetch the full message thread for a conversation.

        Args:
            conversation_id: Encrypted conversation identifier returned by
                :meth:`nachrichten_get_headers`.
            last: Pagination marker for older messages.

        Returns:
            Dict containing the decrypted message payload and nested replies.
        """
        ...

    def nachrichten_search_recipients(self, query: str) -> Dict[str, Any]:
        """Search message recipients by name or partial name.

        Args:
            query: Search term used to look up users in the recipient picker.

        Returns:
            Dict containing the matching users.
        """
        ...

    def nachrichten_send_message(self, message_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send a new Nachricht using the portal's encrypted payload format.

        Args:
            message_data: Dictionary with at least these keys:
                recipients: List of recipient ids such as ["l-{recipient_id}"]
                subject: Message subject text
                body: Message body text

        Returns:
            Dict with success status and the returned message id when sending
            succeeds.
        """
        ...

    def nachrichten_reply_message(
        self, conversation_id: str, body: str, to: str = "all"
    ) -> Dict[str, Any]:
        """Reply to an existing message conversation.

        Args:
            conversation_id: Conversation uniqid to reply to.
            body: Reply message content.
            to: Recipient selector ("all" or a user id).

        Returns:
            Dict with success status and response details.
        """
        ...

    # Mein Unterricht methods
    def meinunterricht_get_overview(self) -> Dict[str, Any]:
        """Fetch the Mein Unterricht overview with current entries.

        Returns:
            Dict containing the parsed overview entries and the raw HTML.
        """
        ...

    def meinunterricht_get_course(self, course_id: str) -> Dict[str, Any]:
        """Fetch the detailed page for a single course folder.

        Args:
            course_id: Course/book id from the portal's data-book attribute.

        Returns:
            Dict containing course metadata, entries, attendance information,
            and attached files.
        """
        ...

    def meinunterricht_get_entry_details(self, url: str) -> Dict[str, Any]:
        """Fetch a linked Mein Unterricht entry by URL.

        Args:
            url: Relative path or absolute URL for the linked entry.

        Returns:
            Dict containing the fetched content and its detected content type.
        """
        ...

    def meinunterricht_get_weekly_view(self) -> Dict[str, Any]:
        """Fetch the weekly Mein Unterricht view.

        Returns:
            Dict containing the HTML response for the weekly class overview.
        """
        ...

    def meinunterricht_get_submissions(self) -> Dict[str, Any]:
        """Fetch the Mein Unterricht submissions/assignments view.

        Returns:
            Dict containing the HTML response for the submissions page.
        """
        ...

    def meinunterricht_set_homework_done(
        self, course_id: str, entry_id: str, done: bool = True
    ) -> Dict[str, Any]:
        """Mark or unmark a homework entry as completed.

        Args:
            course_id: Course/book id for the homework entry.
            entry_id: Entry id for the homework item.
            done: True to mark as done, False to mark as not done.

        Returns:
            Dict containing the requested state and whether the update
            succeeded.
        """
        ...

    def meinunterricht_download_file(self, url: str) -> Dict[str, Any]:
        """Download an attached file using the authenticated session.

        Args:
            url: Relative or absolute file URL extracted from course entries.

        Returns:
            Dict containing filename metadata and binary content.
        """
        ...

    # Kalender methods
    def kalender_get_overview(self) -> Dict[str, Any]:
        """Fetch and parse the calendar overview page.

        Returns:
            Dict containing calendar metadata, categories, groups, and export
            links.
        """
        ...

    def kalender_get_events(
        self,
        year: int = 0,
        start: str = "year",
        category: str = "",
        search: str = "",
        target: str = "",
        view_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch calendar events with the same filters as the web UI.

        Args:
            year: School year selector, where 0 is the current year.
            start: Calendar start mode such as "year", "month", "week", or
                "day".
            category: Category id filter.
            search: Free-text search term.
            target: Target-group filter.
            view_id: Optional calendar view id. If omitted, the current default
                view is used.

        Returns:
            Dict containing the parsed events, filter metadata, and raw payload.
        """
        ...

    def kalender_get_event(
        self, event_id: str, view_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch a single calendar event using the portal's getEvent action.

        Args:
            event_id: Internal event id.
            view_id: Optional calendar view id. If omitted, the default view
                from the overview page is used.

        Returns:
            Dict containing the parsed event payload and the applied filters.
        """
        ...

    # Vertretungsplan methods
    def vertretungsplan_get_plan(self, include_raw: bool = False) -> Dict[str, Any]:
        """Fetch the substitution plan (vertretungsplan.php).

        Args:
            include_raw: Include the raw HTML response in the payload.

        Returns:
            Dict containing the parsed substitution days and metadata.
        """
        ...

    # Stundenplan methods
    def stundenplan_get_plan(self) -> Dict[str, Any]:
        """Fetch the timetable (stundenplan.php).

        Returns:
            Dict containing timetable data for all and personal views.
        """
        ...

    # Dateispeicher methods
    def dateispeicher_get_root(self) -> Dict[str, Any]:
        """Fetch the root folder for the file storage (dateispeicher.php)."""
        ...

    def dateispeicher_get_node(self, folder_id: int = 0) -> Dict[str, Any]:
        """Fetch files and folders for a specific dateispeicher node."""
        ...

    def dateispeicher_search_files(self, query: str) -> Dict[str, Any]:
        """Search files in the dateispeicher by name."""
        ...

    # Lerngruppen methods
    def lerngruppen_get_overview(self) -> Dict[str, Any]:
        """Fetch study groups and exam data (lerngruppen.php)."""
        ...

    def benutzer_get_data(self) -> Dict[str, Any]:
        """Fetch the authenticated user's profile data.

        Returns:
            Dict containing the lowercased profile fields extracted from
            benutzerverwaltung.php.
        """

    # School List methods
    def school_list_get_all(self) -> Dict[str, Any]:
        """Fetch and parse the complete public school directory.

        Returns:
            Dict containing all districts and their schools.
        """
        ...

    def school_list_get_by_district(self, district_id: str) -> Dict[str, Any]:
        """Fetch the schools for one district by id.

        Args:
            district_id: District id such as "7".

        Returns:
            Dict containing the matching district and its schools.
        """
        ...

    def school_list_search_by_name(self, school_name: str) -> Dict[str, Any]:
        """Search the public school list by school name.

        Args:
            school_name: School name or partial name to search for.

        Returns:
            Dict containing the matching schools and the total count.
        """
        ...

    # DSBmobile methods
    def dsb_login(self, username: str, password: str) -> Dict[str, Any]:
        """
        Login to DSBmobile to access substitution plans.

        Args:
            username: DSBmobile username or school identifier (e.g. {username}).
            password: DSBmobile password (e.g. {password}).

        Returns:
            Dict with success status and session cookie data.
        """
        ...

    def dsb_get_plan_urls(
        self, username: Optional[str] = None, password: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch substitution plan iframe URLs after login.

        Args:
            username: DSBmobile username or school identifier (e.g. {username}).
            password: DSBmobile password (e.g. {password}).

        Returns:
            Dict with plan iframe URLs.
        """
        ...

    def dsb_get_substitution_plan(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        plan_index: int = 0,
        plan_url: Optional[str] = None,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        """
        Fetch and parse the substitution plan table from DSBmobile.

        Args:
            username: DSBmobile username or school identifier (e.g. {username}).
            password: DSBmobile password (e.g. {password}).
            plan_index: Which iframe plan URL to parse (default: 0).
            plan_url: Explicit plan URL to fetch (overrides plan_index).

        Returns:
            Dict with the plan URL, title, parsed tables, and optional raw HTML.
        """
        ...

    def logout(self) -> Dict[str, Any]:
        """Logout from Schulportal Hessen"""
        ...

    def login(self, school_id: str, username: str, password: str) -> Dict[str, Any]:
        """Login to Schulportal Hessen"""
        ...

    def sid_validator(self, sid: str) -> bool:
        """Validate a given session ID (sid)"""
        ...

    def login_using_env(self):
        """Login using set creds in .env"""
        ...

    def close(self):
        """Close the session"""
        self.session.close()
        if getattr(self, "dsb_session", None):
            self.dsb_session.close()
            self.dsb_session = None
        self._dsb_cookie = None
        self._dsb_session_id = None
        self.dsb_logged_in = False


# Import and attach the login methods
from .applets.login.api import login, logout, sid_validator, login_using_env

SchulportalHessenAPI.login = login
SchulportalHessenAPI.logout = logout
SchulportalHessenAPI.sid_validator = sid_validator
SchulportalHessenAPI.login_using_env = login_using_env

# Import and attach the nachrichten methods
from .applets.nachrichten.api import (
    nachrichten_get_headers,
    nachrichten_get_conversation,
    nachrichten_search_recipients,
    nachrichten_send_message,
    nachrichten_reply_message,
    nachrichten_mark_read,
)

SchulportalHessenAPI.nachrichten_get_headers = nachrichten_get_headers
SchulportalHessenAPI.nachrichten_get_conversation = nachrichten_get_conversation
SchulportalHessenAPI.nachrichten_search_recipients = nachrichten_search_recipients
SchulportalHessenAPI.nachrichten_send_message = nachrichten_send_message
SchulportalHessenAPI.nachrichten_reply_message = nachrichten_reply_message
SchulportalHessenAPI.nachrichten_mark_read = nachrichten_mark_read

# Import and attach the mein_unterricht methods
from .applets.mein_unterricht.api import (
    meinunterricht_get_overview,
    meinunterricht_get_course,
    meinunterricht_get_entry_details,
    meinunterricht_get_weekly_view,
    meinunterricht_get_submissions,
    meinunterricht_set_homework_done,
    meinunterricht_download_file,
)

SchulportalHessenAPI.meinunterricht_get_overview = meinunterricht_get_overview
SchulportalHessenAPI.meinunterricht_get_course = meinunterricht_get_course
SchulportalHessenAPI.meinunterricht_get_entry_details = meinunterricht_get_entry_details
SchulportalHessenAPI.meinunterricht_get_weekly_view = meinunterricht_get_weekly_view
SchulportalHessenAPI.meinunterricht_get_submissions = meinunterricht_get_submissions
SchulportalHessenAPI.meinunterricht_set_homework_done = meinunterricht_set_homework_done
SchulportalHessenAPI.meinunterricht_download_file = meinunterricht_download_file

# Import and attach the kalender methods
from .applets.kalender.api import (
    kalender_get_overview,
    kalender_get_events,
    kalender_get_event,
)

SchulportalHessenAPI.kalender_get_overview = kalender_get_overview
SchulportalHessenAPI.kalender_get_events = kalender_get_events
SchulportalHessenAPI.kalender_get_event = kalender_get_event

# Import and attach the vertretungsplan methods
from .applets.vertretungsplan.api import vertretungsplan_get_plan

SchulportalHessenAPI.vertretungsplan_get_plan = vertretungsplan_get_plan

# Import and attach the stundenplan methods
from .applets.stundenplan.api import stundenplan_get_plan

SchulportalHessenAPI.stundenplan_get_plan = stundenplan_get_plan

# Import and attach the dateispeicher methods
from .applets.dateispeicher.api import (
    dateispeicher_get_root,
    dateispeicher_get_node,
    dateispeicher_search_files,
)

SchulportalHessenAPI.dateispeicher_get_root = dateispeicher_get_root
SchulportalHessenAPI.dateispeicher_get_node = dateispeicher_get_node
SchulportalHessenAPI.dateispeicher_search_files = dateispeicher_search_files

# Import and attach the lerngruppen methods
from .applets.lerngruppen.api import lerngruppen_get_overview

SchulportalHessenAPI.lerngruppen_get_overview = lerngruppen_get_overview

# Import and attach the benutzer methods
from .applets.benutzer.api import benutzer_get_data

SchulportalHessenAPI.benutzer_get_data = benutzer_get_data

# Import and attach the school_list methods
from .applets.school_list.api import (
    school_list_get_all,
    school_list_get_by_district,
    school_list_search_by_name,
)

SchulportalHessenAPI.school_list_get_all = school_list_get_all
SchulportalHessenAPI.school_list_get_by_district = school_list_get_by_district
SchulportalHessenAPI.school_list_search_by_name = school_list_search_by_name

# Import and attach the DSBmobile methods
from .external.dsb.api import (
    dsb_login,
    dsb_get_plan_urls,
    dsb_get_substitution_plan,
)

SchulportalHessenAPI.dsb_login = dsb_login
SchulportalHessenAPI.dsb_get_plan_urls = dsb_get_plan_urls
SchulportalHessenAPI.dsb_get_substitution_plan = dsb_get_substitution_plan
