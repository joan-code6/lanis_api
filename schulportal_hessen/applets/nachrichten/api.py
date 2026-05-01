from typing import Dict, Any
import json
from schulportal_hessen.tools.cryptor import Cryptor


def nachrichten_get_headers(
    self, get_type: str = "All", last: int = 0
) -> Dict[str, Any]:
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
        return {"success": False, "error": "Not logged in"}

    # Initialize cryptor if needed
    if not self.cryptor or not self.cryptor.authenticated:
        if not self.cryptor:
            self.cryptor = Cryptor(self.session)

        # Authenticate the cryptor to set up encryption keys
        try:
            auth_result = self.cryptor.authenticate()
            if not auth_result:
                return {"success": False, "error": "Failed to authenticate encryption"}
        except Exception as e:
            import traceback

            return {
                "success": False,
                "error": f"Failed to initialize encryption: {str(e)}",
                "details": str(e),
            }

    if not self.cryptor or not self.cryptor.authenticated:
        return {"success": False, "error": "Encryption not initialized"}

    try:
        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={"a": "headers", "getType": get_type, "last": str(last)},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        response.raise_for_status()

        data = response.json()

        # Debug: log the response structure
        print(
            f"[DEBUG] nachrichten_get_headers response keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}"
        )

        # Decrypt the 'rows' field
        if "rows" in data and data["rows"]:
            decrypted = self.cryptor.decrypt(data["rows"])
            conversations = json.loads(decrypted)

            return {
                "success": True,
                "total": data.get("total", 0),
                "conversations": conversations,
            }
        else:
            return {"success": True, "total": data.get("total", 0), "conversations": []}

    except Exception as e:
        import traceback

        return {
            "success": False,
            "error": f"Failed to fetch messages: {str(e)}",
            "trace": traceback.format_exc(),
        }


def nachrichten_get_conversation(
    self, conversation_id: str, last: int = 0
) -> Dict[str, Any]:
    """
    Fetch messages from a specific conversation

    Args:
        conversation_id: The conversation/thread ID
        last: Timestamp for pagination (0 for all messages)

    Returns:
        Dict with success status and decrypted messages
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    # Initialize cryptor if needed
    if not self.cryptor or not self.cryptor.authenticated:
        if not self.cryptor:
            self.cryptor = Cryptor(self.session)

        # Authenticate the cryptor to set up encryption keys
        try:
            if not self.cryptor.authenticate():
                return {"success": False, "error": "Failed to authenticate encryption"}
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to initialize encryption: {str(e)}",
            }

    if not self.cryptor or not self.cryptor.authenticated:
        return {"success": False, "error": "Encryption not initialized"}

    try:
        # Encrypt the conversation ID (Uniquid)
        encrypted_id = self.cryptor.encrypt(conversation_id)

        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={"a": "read", "uniqid": encrypted_id},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        response.raise_for_status()

        data = response.json()

        if "message" in data:
            decrypted = self.cryptor.decrypt(data["message"])
            message_data = json.loads(decrypted)

            # The structure returned is a single message object with a 'reply' list
            # We want to return a list of messages including the main one and replies
            messages = [message_data]
            if "reply" in message_data and message_data["reply"]:
                messages.extend(message_data["reply"])

            return {"success": True, "messages": messages}
        else:
            return {"success": False, "error": f"No message data in response: {data}"}

    except Exception as e:
        return {"success": False, "error": f"Failed to fetch conversation: {str(e)}"}


def nachrichten_search_recipients(self, query: str) -> Dict[str, Any]:
    """
    Search for message recipients (users)

    Args:
        query: Search query (name or partial name)

    Returns:
        Dict with success status and list of users
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    if not self.cryptor or not self.cryptor.authenticated:
        return {"success": False, "error": "Encryption not initialized"}

    try:
        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={"a": "searchRecipt", "q": query},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        response.raise_for_status()

        # Try to parse as JSON first (might not be encrypted)
        try:
            users = response.json()
            return {"success": True, "users": users}
        except json.JSONDecodeError:
            # If not JSON, try to decrypt
            encrypted_data = response.text
            decrypted = self.cryptor.decrypt(encrypted_data)
            users = json.loads(decrypted)

            return {"success": True, "users": users}

    except Exception as e:
        return {"success": False, "error": f"Failed to search recipients: {str(e)}"}


def nachrichten_send_message(self, message_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send a new message

    Args:
        message_data: Dict with keys:
            - recipients: list of recipient IDs (e.g., ["l-14480"])
            - subject: message subject
            - body: message text

    Returns:
        Dict with success status and message ID if successful

    Example:
        >>> api.nachrichten_send_message({
        ...     "recipients": ["l-14480"],
        ...     "subject": "Hello",
        ...     "body": "Test message"
        ... })
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    if not self.cryptor or not self.cryptor.authenticated:
        return {"success": False, "error": "Encryption not initialized"}

    try:
        recipients = message_data.get("recipients", [])
        subject = message_data.get("subject", "")
        body = message_data.get("body", "")

        # Build payload in the format SPH expects (based on Flutter reverse engineering)
        payload = [
            {"name": "subject", "value": subject},
            {"name": "text", "value": body},
        ]
        for recipient in recipients:
            payload.append({"name": "to[]", "value": recipient})

        encrypted_payload = self.cryptor.encrypt(json.dumps(payload))

        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={"a": "newmessage", "c": encrypted_payload},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )

        result = response.json()

        if result.get("back") is True:
            return {
                "success": True,
                "message_id": result.get("id"),
            }
        else:
            return {
                "success": False,
                "error": "Message sending failed",
                "details": result,
            }

    except Exception as e:
        return {"success": False, "error": f"Failed to send message: {str(e)}"}
