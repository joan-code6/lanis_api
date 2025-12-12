import requests
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import json

from functions.tools.cryptor import Cryptor


class SchulportalHessenAPI:
    """
    API Client for Schulportal Hessen (SPH)

    This class provides methods to interact with the Schulportal Hessen platform,
    allowing programmatic access to login, retrieve available apps, and navigate
    the portal as a normal user would through a browser.
    """

    BASE_LOGIN_URL = "https://login.schulportal.hessen.de"
    BASE_START_URL = "https://start.schulportal.hessen.de"

    def __init__(self):
        """Initialize the API client with a session for cookie management."""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1'
        })
        self.school_id: Optional[str] = None
        self.logged_in = False
        self.cryptor: Optional[Cryptor] = None

    def get_apps(self) -> Dict[str, Any]:
        """
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
        """
        if not self.logged_in:
            return {
                'success': False,
                'error': 'Not logged in. Please login first.'
            }

        try:
            apps_url = f"{self.BASE_START_URL}/startseite.php?a=ajax&f=apps"

            response = self.session.get(apps_url)
            response.raise_for_status()

            data = response.json()
            return {
                'success': True,
                'data': data
            }

        except requests.RequestException as e:
            return {
                'success': False,
                'error': f'Failed to retrieve apps: {str(e)}'
            }
        except json.JSONDecodeError as e:
            return {
                'success': False,
                'error': f'Failed to parse response: {str(e)}'
            }

    def get_available_modules(self) -> List[Dict[str, str]]:
        """
        Get a simplified list of available modules with their access URLs

        Returns:
            List of dicts containing module name and full URL
        """
        apps_data = self.get_apps()

        if not apps_data.get('success'):
            return []

        modules = []
        entries = apps_data.get('data', {}).get('entrys', [])

        for entry in entries:
            link = entry.get('link', '')
            # Convert relative links to absolute URLs
            if link.startswith('http'):
                full_url = link
            else:
                full_url = f"{self.BASE_START_URL}/{link}"

            modules.append({
                'name': entry.get('Name'),
                'url': full_url,
                'color': entry.get('Farbe'),
                'logo': entry.get('Logo'),
                'folders': entry.get('Ordner', []),
                'target': entry.get('target', '_self')
            })

        return modules

    def get_cookies(self) -> Dict[str, str]:
        """
        Get current session cookies

        Returns:
            Dict of cookie name-value pairs
        """
        cookies_dict = {}
        for cookie in self.session.cookies:
            cookies_dict[cookie.name] = cookie.value
        return cookies_dict

    # Message/Nachrichten methods
    def nachrichten_get_headers(self, get_type: str = "All", last: int = 0) -> Dict[str, Any]:
        """Fetch messages overview/headers (conversations list)"""
        ...

    def nachrichten_get_conversation(self, conversation_id: str, last: int = 0) -> Dict[str, Any]:
        """Fetch messages from a specific conversation"""
        ...

    def nachrichten_search_recipients(self, query: str) -> Dict[str, Any]:
        """Search for message recipients (users)"""
        ...

    def nachrichten_send_message(self, message_data: Dict[str, Any]) -> Dict[str, Any]:
        """ToDo make it work"""
        ...

    # Mein Unterricht methods
    def meinunterricht_get_overview(self) -> Dict[str, Any]:
        """Fetch "mein Unterricht" overview page with current entries"""
        ...

    def meinunterricht_get_course(self, course_id: str) -> Dict[str, Any]:
        """Fetch detailed view of a specific course/class folder"""
        ...

    def meinunterricht_get_entry_details(self, url: str) -> Dict[str, Any]:
        """Fetch details for a specific entry/link from mein Unterricht"""
        ...

    def meinunterricht_get_weekly_view(self) -> Dict[str, Any]:
        """Fetch weekly view of class entries"""
        ...

    def meinunterricht_get_submissions(self) -> Dict[str, Any]:
        """Fetch student submissions/assignments (Abgaben)"""
        ...
    def benutzer_get_data(self) -> Dict[str, Any]:
        """Fetch student/user data from benutzerverwaltung.php"""

    # School List methods
    def school_list_get_all(self) -> Dict[str, Any]:
        """Fetch and parse all schools organized by district"""
        ...

    def school_list_get_by_district(self, district_id: str) -> Dict[str, Any]:
        """Fetch schools for a specific district"""
        ...

    def school_list_search_by_name(self, school_name: str) -> Dict[str, Any]:
        """Search for schools by name across all districts"""
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

    def close(self):
        """Close the session"""
        self.session.close()


# Import and attach the login methods
from .applets.login.api import (
    login,
    logout,
    sid_validator
)

SchulportalHessenAPI.login = login
SchulportalHessenAPI.logout = logout
SchulportalHessenAPI.sid_validator = sid_validator

# Import and attach the nachrichten methods
from .applets.nachrichten.api import (
    nachrichten_get_headers,
    nachrichten_get_conversation,
    nachrichten_search_recipients,
    nachrichten_send_message
)

SchulportalHessenAPI.nachrichten_get_headers = nachrichten_get_headers
SchulportalHessenAPI.nachrichten_get_conversation = nachrichten_get_conversation
SchulportalHessenAPI.nachrichten_search_recipients = nachrichten_search_recipients
SchulportalHessenAPI.nachrichten_send_message = nachrichten_send_message

# Import and attach the mein_unterricht methods
from .applets.mein_unterricht.api import (
    meinunterricht_get_overview,
    meinunterricht_get_course,
    meinunterricht_get_entry_details,
    meinunterricht_get_weekly_view,
    meinunterricht_get_submissions
)

SchulportalHessenAPI.meinunterricht_get_overview = meinunterricht_get_overview
SchulportalHessenAPI.meinunterricht_get_course = meinunterricht_get_course
SchulportalHessenAPI.meinunterricht_get_entry_details = meinunterricht_get_entry_details
SchulportalHessenAPI.meinunterricht_get_weekly_view = meinunterricht_get_weekly_view
SchulportalHessenAPI.meinunterricht_get_submissions = meinunterricht_get_submissions

# Import and attach the benutzer methods
from .applets.benutzer.api import (
    benutzer_get_data
)

SchulportalHessenAPI.benutzer_get_data = benutzer_get_data

# Import and attach the school_list methods
from .applets.school_list.api import (
    school_list_get_all,
    school_list_get_by_district,
    school_list_search_by_name
)

SchulportalHessenAPI.school_list_get_all = school_list_get_all
SchulportalHessenAPI.school_list_get_by_district = school_list_get_by_district
SchulportalHessenAPI.school_list_search_by_name = school_list_search_by_name