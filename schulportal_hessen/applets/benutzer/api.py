from typing import Dict, Any
import json
from bs4 import BeautifulSoup


def benutzer_get_data(self) -> Dict[str, Any]:
    """Fetch user profile data from benutzerverwaltung.php.

    Retrieves personal profile information for the authenticated
    user including name, email, school details, and other
    account information stored in the user management system.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing:
        - success (bool): Whether request succeeded
        - data (Dict): User profile fields (lowercased keys):
          - login, nachname, vorname, email, geschlecht, ...

    Raises
    ------
    Exception
        If HTTP request fails or HTML parsing fails.

    Example
    -----
    >>> api.benutzer_get_data()
    {'success': True, 'data': {
        'login': 'max.mustermann',
        'nachname': 'Mustermann',
        'vorname': 'Max',
        'email': 'max.mustermann@schule.hessen.de'
    }}
    """
    if not self.logged_in:
        return {'success': False, 'error': 'Not logged in'}

    try:
        # First visit the start page to establish school context
        self.session.get(
            f"{self.BASE_START_URL}/",
        )
        
        response = self.session.post(
            f"{self.BASE_START_URL}/benutzerverwaltung.php",
            data={'a': 'userData'},
        )
        if response.status_code != 200:
            return {'success': False, 'error': f'Failed to fetch user data: {response.status_code}'}

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find the table with user data
        table = soup.find('table', class_='table table-striped')
        if not table:
            # TODO: just create a new file:

            open("temp/debug_benutzer.html", "x", encoding="utf-8").write(response.text)
            return {'success': False, 'error': 'User data table not found'}

        user_data = {}
        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all('td')
            if len(cells) == 2:
                key = cells[0].get_text(strip=True).rstrip(':')
                value = cells[1].get_text(strip=True)
                
                # Special handling for gender
                if key == 'Geschlecht':
                    icon = cells[1].find('i', class_='fas')
                    if icon and icon.get('title'):
                        value = icon['title']
                
                user_data[key.lower()] = value

        return {'success': True, 'data': user_data}

    except Exception as e:
        return {'success': False, 'error': f'Failed to parse user data: {str(e)}'}