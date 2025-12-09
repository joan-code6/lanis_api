from typing import Dict, Any
import json
from functions.tools.cryptor import Cryptor


def nachrichten_get_headers(self, get_type: str = "All", last: int = 0) -> Dict[str, Any]:
    """
    Fetch messages overview/headers (conversations list)

    Args:
        get_type: Filter type - "All", "visibleOnly", "unvisibleOnly"
        last: Pagination/refresh parameter (0 for full sync)

    Returns:
        Dict with success status and decrypted message data

    Example:
        >>> api.nachrichten_get_headers()
        {'success': True, 'total': 40, 'conversations': [...]}
    """
    if not self.logged_in:
        return {'success': False, 'error': 'Not logged in'}

    # Initialize cryptor if needed
    if not self.cryptor or not self.cryptor.authenticated:
        if not self.cryptor:
            self.cryptor = Cryptor()
        
        # Fetch nachrichten.php to get keys
        try:
            resp = self.session.get(f"{self.BASE_START_URL}/nachrichten.php")
            if resp.status_code == 200:
                self.cryptor.extract_keys(resp.text)
            else:
                return {'success': False, 'error': f'Failed to fetch nachrichten.php: {resp.status_code}'}
        except Exception as e:
            return {'success': False, 'error': f'Failed to initialize encryption: {str(e)}'}

    if not self.cryptor or not self.cryptor.authenticated:
        return {'success': False, 'error': 'Encryption not initialized'}

    try:
        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={
                'a': 'headers',
                'getType': get_type,
                'last': str(last)
            },
            headers={
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin'
            }
        )
        response.raise_for_status()

        data = response.json()

        # Decrypt the 'rows' field
        if 'rows' in data and data['rows']:
            decrypted = self.cryptor.decrypt(data['rows'])
            conversations = json.loads(decrypted)

            return {
                'success': True,
                'total': data.get('total', 0),
                'conversations': conversations
            }
        else:
            return {
                'success': True,
                'total': data.get('total', 0),
                'conversations': []
            }

    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to fetch messages: {str(e)}'
        }


def nachrichten_get_conversation(self, conversation_id: str, last: int = 0) -> Dict[str, Any]:
    """
    Fetch messages from a specific conversation

    Args:
        conversation_id: The conversation/thread ID
        last: Timestamp for pagination (0 for all messages)

    Returns:
        Dict with success status and decrypted messages
    """
    if not self.logged_in:
        return {'success': False, 'error': 'Not logged in'}

    # Initialize cryptor if needed
    if not self.cryptor or not self.cryptor.authenticated:
        if not self.cryptor:
            self.cryptor = Cryptor()
        
        # Fetch nachrichten.php to get keys
        try:
            resp = self.session.get(f"{self.BASE_START_URL}/nachrichten.php")
            if resp.status_code == 200:
                self.cryptor.extract_keys(resp.text)
            else:
                return {'success': False, 'error': f'Failed to fetch nachrichten.php: {resp.status_code}'}
        except Exception as e:
            return {'success': False, 'error': f'Failed to initialize encryption: {str(e)}'}

    if not self.cryptor or not self.cryptor.authenticated:
        return {'success': False, 'error': 'Encryption not initialized'}

    try:
        # Encrypt the conversation ID (Uniquid)
        encrypted_id = self.cryptor.encrypt(conversation_id)

        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={
                'a': 'read',
                'uniqid': encrypted_id
            },
            headers={
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin'
            }
        )
        response.raise_for_status()

        data = response.json()
        
        if 'message' in data:
            decrypted = self.cryptor.decrypt(data['message'])
            message_data = json.loads(decrypted)
            
            # The structure returned is a single message object with a 'reply' list
            # We want to return a list of messages including the main one and replies
            messages = [message_data]
            if 'reply' in message_data and message_data['reply']:
                messages.extend(message_data['reply'])
                
            return {
                'success': True,
                'messages': messages
            }
        else:
             return {
                'success': False,
                'error': f"No message data in response: {data}"
            }

    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to fetch conversation: {str(e)}'
        }


def nachrichten_search_recipients(self, query: str) -> Dict[str, Any]:
    """
    Search for message recipients (users)

    Args:
        query: Search query (name or partial name)

    Returns:
        Dict with success status and list of users
    """
    if not self.logged_in:
        return {'success': False, 'error': 'Not logged in'}

    if not self.cryptor or not self.cryptor.authenticated:
        return {'success': False, 'error': 'Encryption not initialized'}

    try:
        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={
                'a': 'searchRecipt',
                'q': query
            },
            headers={
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin'
            }
        )
        response.raise_for_status()

        # Try to parse as JSON first (might not be encrypted)
        try:
            users = response.json()
            return {
                'success': True,
                'users': users
            }
        except json.JSONDecodeError:
            # If not JSON, try to decrypt
            encrypted_data = response.text
            decrypted = self.cryptor.decrypt(encrypted_data)
            users = json.loads(decrypted)

            return {
                'success': True,
                'users': users
            }

    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to search recipients: {str(e)}'
        }


def nachrichten_send_message(self, message_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send a new message

    Args:
        message_data: Dict containing message details (recipients, subject, body, etc.)

    Returns:
        Dict with success status

    Note:
        The message_data structure needs to match SPH's expected format.
        This may require further investigation of the exact payload structure.
    """
    if not self.logged_in:
        return {'success': False, 'error': 'Not logged in'}

    if not self.cryptor or not self.cryptor.authenticated:
        return {'success': False, 'error': 'Encryption not initialized'}

    try:
        # Encrypt the message data
        encrypted_payload = self.cryptor.encrypt(json.dumps(message_data))

        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={
                'a': 'newmessage',
                'c': encrypted_payload
            },
            headers={
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin'
            }
        )
        response.raise_for_status()

        result = response.json()

        return {
            'success': True,
            'response': result
        }

    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to send message: {str(e)}'
        }