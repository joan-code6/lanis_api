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

    def login(self, school_id: str, username: str, password: str) -> Dict[str, Any]:
        """
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
        """
        self.school_id = school_id

        # First, set initial cookies by visiting the login page
        try:
            # Visit login page to get initial cookies
            login_url = f"{self.BASE_LOGIN_URL}/?i={school_id}"
            self.session.get(login_url)

            # Prepare login data
            login_data = {
                'user2': username,
                'user': school_id + '.' + username,
                'password': password
            }

            # Set additional cookies that are typically present
            self.session.cookies.set('sph-login-upstream', '3', domain='hessen.de')
            self.session.cookies.set('Ilnglanguage', 'de', domain='hessen.de')
            self.session.cookies.set('schulportal_lastschool', school_id, domain='hessen.de')
            self.session.cookies.set('schulportal_logindomain', 'login.schulportal.hessen.de', domain='.hessen.de')
            self.session.cookies.set('rememberMe', '1', domain='hessen.de')

            # Perform login POST request
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': self.BASE_LOGIN_URL,
                'Referer': login_url,
                'Cache-Control': 'max-age=0'
            }

            response = self.session.post(
                login_url,
                data=login_data,
                headers=headers,
                allow_redirects=True
            )

            # Check if login was successful by looking for session cookies
            cookies_dict = {}
            for cookie in self.session.cookies:
                cookies_dict[cookie.name] = cookie.value

            if 'sid' in cookies_dict or 'SPH-Session' in cookies_dict:
                self.logged_in = True

                # Initialize cryptor for encrypted communications
                self.cryptor = Cryptor(self.session)
                try:
                    if self.cryptor.authenticate():
                        print("✓ Encryption initialized successfully")
                    else:
                        print("⚠ Encryption initialization failed - encrypted features may not work")
                except Exception as e:
                    print(f"⚠ Encryption setup error: {e}")
                    self.cryptor = None

                return {
                    'success': True,
                    'message': 'Login successful',
                    'cookies': cookies_dict,
                    'school_id': school_id,
                    'encryption_ready': self.cryptor is not None and self.cryptor.authenticated
                }
            else:
                return {
                    'success': False,
                    'message': 'Login failed - no session cookie received',
                    'status_code': response.status_code,
                    'cookies': cookies_dict
                }

        except requests.RequestException as e:
            return {
                'success': False,
                'message': f'Login request failed: {str(e)}'
            }

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

    def close(self):
        """Close the session"""
        self.session.close()


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