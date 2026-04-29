from typing import Dict, Any
import requests
from functions.tools.cryptor import Cryptor

def login_using_env(self, env_path="../.env"):
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except Exception as e:
        return "error when trying to use dotenv"
    try:
        username = str(os.getenv("LANIS_API_USERNAME"))
        password = str(os.getenv("LANIS_API_PASSWORD"))
        school_id = str(os.getenv("LANIS_API_SCHOOL_ID"))
    except Exception as e:
        return "error loading creds from env"
    return login(self, school_id, username, password)

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


def logout(self) -> Dict[str, Any]:
    """
    Logout from Schulportal Hessen

    Returns:
        Dict containing logout status
    """
    if not self.logged_in:
        return {
            'success': False,
            'error': 'Not logged in'
        }

    try:
        logout_url = f"{self.BASE_START_URL}/index.php?logout=all"
        self.session.get(logout_url)

        self.logged_in = False
        self.cryptor = None

        return {
            'success': True,
            'message': 'Logged out successfully'
        }

    except requests.RequestException as e:
        return {
            'success': False,
            'error': f'Logout request failed: {str(e)}'
        }


def sid_validator(self) -> Dict[str, Any]:
    """
    Validate the current session by checking if the apps endpoint is accessible

    Returns:
        Dict containing validation status
    """
    if not self.logged_in:
        return {
            'valid': False,
            'error': 'Not logged in'
        }

    try:
        apps_url = f"{self.BASE_START_URL}/startseite.php?a=ajax&f=apps"
        response = self.session.get(apps_url, allow_redirects=False)

        if response.status_code == 200:
            return {
                'valid': True
            }
        else:
            self.logged_in = False
            return {
                'valid': False,
                'status_code': response.status_code
            }

    except requests.RequestException as e:
        self.logged_in = False
        return {
            'valid': False,
            'error': f'Validation request failed: {str(e)}'
        }