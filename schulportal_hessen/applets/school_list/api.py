"""
Schulportal Hessen School Directory API.

Provides access to the public school directory for all schools in Hesse.
The school list is fetched from the public exporteur endpoint and
includes schools organized by district/region.

This module does NOT require authentication - it uses a public endpoint.

District IDs roughly correspond to:
- 1: Gießen
- 2: Kassel  
- 3: Marburg
- 4: Fulda
- 5: Limburg-Weilburg
- 6: Lahn-Dill
- 7: Bergstraße/Odenwald
- 8: Main-Kinzig
- 9: Main-Taunus
- 10: Hochtaunus
- ...

Example
-----
>>> api.school_list_get_all()
{'success': True, 'districts': [{'id': '{district_id}', 'name': '{district_name}', 'schools': [...]}]}

>>> api.school_list_search_by_name("{school_name}")
{'success': True, 'query': '{school_name}', 'count': 5, 'results': [...]}
"""

from typing import Any, Dict

import json

import requests
from schulportal_hessen.tools.search import text_matches_query


def school_list_get_all(self) -> Dict[str, Any]:
    """
    Fetch and parse the school list from Schulportal Hessen

    Retrieves the complete list of all schools organized by district/region.
    The data is fetched from the public exporteur endpoint and parsed into 
    a structured JSON format.

    Returns:
        Dict with success status and parsed school list data

    Example response structure:
        {
            'success': True,
            'districts': [
                {
                    'id': '{district_id}',
                    'name': '{district_name}',
                    'schools': [
                        {
                            'id': '{school_id}',
                            'name': '{school_name}',
                            'location': '{city_name}'
                        },
                        ...
                    ]
                },
                ...
            ]
        }

    Example:
        >>> api.school_list_get_all()
        {
            'success': True,
            'districts': [
                {
                    'id': '{district_id}',
                    'name': '{district_name}',
                    'schools': [
                        {'id': '{school_id}', 'name': '{school_name}', 'location': '{city_name}'},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    try:
        # Fetch the school list from the public endpoint
        url = "https://startcache.schulportal.hessen.de/exporteur.php?a=schoollist"
        
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Transform the data into a cleaner structure
        districts = []
        for district in data:
            transformed_district = {
                'id': district.get('Id', ''),
                'name': district.get('Name', ''),
                'schools': []
            }
            
            # Transform schools within the district
            for school in district.get('Schulen', []):
                transformed_school = {
                    'id': school.get('Id', ''),
                    'name': school.get('Name', ''),
                    'location': school.get('Ort', '')
                }
                transformed_district['schools'].append(transformed_school)
            
            districts.append(transformed_district)
        
        return {
            'success': True,
            'districts': districts
        }
        
    except requests.RequestException as e:
        return {
            'success': False,
            'error': f'Failed to fetch school list: {str(e)}'
        }
    except json.JSONDecodeError as e:
        return {
            'success': False,
            'error': f'Failed to parse school list: {str(e)}'
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Unexpected error while fetching school list: {str(e)}'
        }


def school_list_get_by_district(self, district_id: str) -> Dict[str, Any]:
    """
    Fetch and parse schools for a specific district

    Retrieves the list of schools for a specific district/region by ID.

    Args:
        district_id: The district ID (e.g., '{district_id}')

    Returns:
        Dict with success status and parsed school data for the district

    Example:
        >>> api.school_list_get_by_district('{district_id}')
        {
            'success': True,
            'district': {
                'id': '{district_id}',
                'name': '{district_name}',
                'schools': [...]
            }
        }
    """
    try:
        # Fetch the school list from the public endpoint
        url = "https://startcache.schulportal.hessen.de/exporteur.php?a=schoollist"
        
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Find the district with matching ID
        for district in data:
            if district.get('Id', '') == district_id:
                transformed_district = {
                    'id': district.get('Id', ''),
                    'name': district.get('Name', ''),
                    'schools': []
                }
                
                # Transform schools within the district
                for school in district.get('Schulen', []):
                    transformed_school = {
                        'id': school.get('Id', ''),
                        'name': school.get('Name', ''),
                        'location': school.get('Ort', '')
                    }
                    transformed_district['schools'].append(transformed_school)
                
                return {
                    'success': True,
                    'district': transformed_district
                }
        
        return {
            'success': False,
            'error': f'District with ID {district_id} not found'
        }
        
    except requests.RequestException as e:
        return {
            'success': False,
            'error': f'Failed to fetch school list: {str(e)}'
        }
    except json.JSONDecodeError as e:
        return {
            'success': False,
            'error': f'Failed to parse school list: {str(e)}'
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Unexpected error while fetching school list: {str(e)}'
        }


def school_list_search_by_name(self, school_name: str) -> Dict[str, Any]:
    """
    Search for schools by name across all districts

    Performs a case-insensitive search for schools matching the provided name.

    Args:
        school_name: The school name or partial name to search for

    Returns:
        Dict with success status and list of matching schools

    Example:
        >>> api.school_list_search_by_name('{school_name}')
        {
            'success': True,
            'results': [
                {
                    'district_id': '{district_id}',
                    'district_name': '{district_name}',
                    'school': {'id': '{school_id}', 'name': '{school_name}', 'location': '{city_name}'}
                },
                ...
            ]
        }
    """
    try:
        # Fetch the school list from the public endpoint
        url = "https://startcache.schulportal.hessen.de/exporteur.php?a=schoollist"
        
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Search for matching schools with tolerant matching (accents, typos, partials)
        results = []
        query = (school_name or "").strip()
        
        for district in data:
            district_name = district.get('Name', '')
            for school in district.get('Schulen', []):
                school_name_value = school.get('Name', '')
                school_location = school.get('Ort', '')
                searchable_text = f"{school_name_value} {school_location} {district_name}"
                if text_matches_query(searchable_text, query):
                    results.append({
                        'district_id': district.get('Id', ''),
                        'district_name': district_name,
                        'school': {
                            'id': school.get('Id', ''),
                            'name': school_name_value,
                            'location': school_location
                        }
                    })
        
        return {
            'success': True,
            'query': school_name,
            'results': results,
            'count': len(results)
        }
        
    except requests.RequestException as e:
        return {
            'success': False,
            'error': f'Failed to fetch school list: {str(e)}'
        }
    except json.JSONDecodeError as e:
        return {
            'success': False,
            'error': f'Failed to parse school list: {str(e)}'
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Unexpected error while searching schools: {str(e)}'
        }
