from typing import Any, Dict, List
import json

from schulportal_hessen.tools.cryptor import Cryptor
from schulportal_hessen.tools.search import normalize_search_text, value_matches_query


def _extract_users(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("users", "results", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _recipient_key(user: Dict[str, Any]) -> str:
    for key in ("id", "value", "userid", "user_id", "uid", "label"):
        value = user.get(key)
        if value:
            return str(value)
    return json.dumps(user, sort_keys=True, ensure_ascii=False)


def nachrichten_get_headers(
    self, get_type: str = "All", last: int = 0
) -> Dict[str, Any]:
    """Fetch messages overview/headers (conversations list).

    Retrieves the list of message conversations for the authenticated user.
    This endpoint returns encrypted data that is automatically decrypted.

    Parameters
    ----------
    get_type : str, optional
        Filter type for messages. Options:
        - "All" (default): All messages
        - "visibleOnly": Only visible messages
        - "unvisibleOnly": Only hidden messages
    last : int, optional
        Pagination parameter (0 for full sync). Pass the last message ID
        to fetch older messages incrementally.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing:
        - success (bool): Whether request succeeded
        - total (int): Total number of conversations
        - conversations (List[Dict]): List of conversation objects with:
          - id, sender, subject, date, unread, ...

    Raises
    ------
    Exception
        If encryption initialization fails or HTTP request fails.

    Notes
    -----
    This method requires the Cryptor to be initialized for
    decrypting the encrypted message data from SPH.

    Example
    -----
    >>> api.nachrichten_get_headers()
    {'success': True, 'total': 40, 'conversations': [
    ...     {'id': '{conversation_id}', 'sender': '{sender}', 'subject': '{subject}', 'date': '{date}', 'unread': True}
    ... ]}
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

            # Normalize read/unread: portal provides unread (0/1) inconsistently.
            for conv in conversations:
                if "unread" in conv:
                    unread_val = conv.get("unread")
                    try:
                        unread_bool = bool(int(unread_val))
                    except Exception:
                        unread_bool = bool(unread_val)
                else:
                    unread_bool = False
                    conv["unread"] = 0
                conv["read"] = not unread_bool

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
    """Fetch messages from a specific conversation.

    Retrieves all messages within a conversation thread, including
    the initial message and all replies.

    Parameters
    ----------
    conversation_id : str
        The unique conversation/thread identifier (e.g., "{conversation_id}").
        This can be obtained from nachrichten_get_headers().
    last : int, optional
        Timestamp for pagination. Pass 0 to fetch all messages
        in the conversation.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing:
        - success (bool): Whether request succeeded
        - messages (List[Dict]): List of message objects, each containing:
          - sender, content, date, attachments, reply (nested replies)

    Raises
    ------
    Exception
        If encryption fails or conversation not found.

    Example
    -----
    >>> api.nachrichten_get_conversation("{conversation_id}")
    {'success': True, 'messages': [
    ...     {'sender': '{sender}', 'content': '{message_text}', 'date': '{date}'},
    ...     {'sender': '{sender}', 'content': '{message_text}', 'date': '{date}'}
    ... ]}
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
        if not self.cryptor:
            self.cryptor = Cryptor(self.session)
        try:
            if not self.cryptor.authenticate():
                return {"success": False, "error": "Failed to authenticate encryption"}
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to initialize encryption: {str(e)}",
            }

    try:
        def _fetch_users(search_term: str) -> List[Dict[str, Any]]:
            response = self.session.post(
                f"{self.BASE_START_URL}/nachrichten.php",
                data={"a": "searchRecipt", "q": search_term},
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

            try:
                payload = response.json()
            except json.JSONDecodeError:
                decrypted = self.cryptor.decrypt(response.text)
                payload = json.loads(decrypted)
            return _extract_users(payload)

        base_query = (query or "").strip()
        normalized_query = normalize_search_text(base_query)

        query_variants: List[str] = []
        if base_query:
            query_variants.append(base_query)
        if normalized_query and normalized_query != base_query.lower():
            query_variants.append(normalized_query)
        query_variants.extend(token for token in normalized_query.split() if len(token) >= 2)
        query_variants = list(dict.fromkeys(variant for variant in query_variants if variant))

        if not query_variants:
            return {"success": True, "query": base_query, "users": [], "count": 0}

        aggregated_users: List[Dict[str, Any]] = []
        seen = set()
        for variant in query_variants:
            for user in _fetch_users(variant):
                key = _recipient_key(user)
                if key in seen:
                    continue
                seen.add(key)
                aggregated_users.append(user)

        filtered_users = [
            user for user in aggregated_users if value_matches_query(user, base_query)
        ]
        users_out = filtered_users or aggregated_users

        return {
            "success": True,
            "query": base_query,
            "users": users_out,
            "count": len(users_out),
        }

    except Exception as e:
        return {"success": False, "error": f"Failed to search recipients: {str(e)}"}


def nachrichten_send_message(self, message_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send a new message

    Args:
        message_data: Dict with keys:
            - recipients: list of recipient IDs (e.g., ["l-{recipient_id}"])
            - subject: message subject
            - body: message text

    Returns:
        Dict with success status and message ID if successful

    Example:
        >>> api.nachrichten_send_message({
        ...     "recipients": ["l-{recipient_id}"],
        ...     "subject": "{subject}",
        ...     "body": "{message_text}"
        ... })
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    if not self.cryptor or not self.cryptor.authenticated:
        if not self.cryptor:
            self.cryptor = Cryptor(self.session)
        try:
            if not self.cryptor.authenticate():
                return {"success": False, "error": "Failed to authenticate encryption"}
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to initialize encryption: {str(e)}",
            }

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


def nachrichten_reply_message(
    self, conversation_id: str, body: str, to: str = "all"
) -> Dict[str, Any]:
    """
    Reply to an existing conversation/thread.

    Args:
        conversation_id: The conversation's message id (data-msg) as shown in
            the read URL and returned by `nachrichten_get_headers()`.
        body: The reply text content.
        to: Recipient selector ("all" for group replies or a user id).

    Returns:
        Dict with success status and server result details.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    if not self.cryptor or not self.cryptor.authenticated:
        return {"success": False, "error": "Encryption not initialized"}

    try:
        # Build payload expected by the portal for replies (read.js)
        payload = {
            "to": to,
            "message": body,
            "replyToMsg": conversation_id,
        }

        encrypted_payload = self.cryptor.encrypt(json.dumps(payload))

        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={"a": "reply", "c": encrypted_payload},
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

        result = response.json()

        # Portal typically returns {'back': True, ...} on success
        if result.get("back") is True:
            return {"success": True, "details": result}
        else:
            return {"success": False, "error": "Reply failed", "details": result}

    except Exception as e:
        return {"success": False, "error": f"Failed to send reply: {str(e)}"}


def nachrichten_mark_read(self, conversation_id: str) -> Dict[str, Any]:
    """Mark a conversation as read by calling the same 'read' action used by the UI.

    This posts the encrypted `uniqid` and returns the portal response.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    if not self.cryptor or not self.cryptor.authenticated:
        if not self.cryptor:
            self.cryptor = Cryptor(self.session)
        try:
            if not self.cryptor.authenticate():
                return {"success": False, "error": "Failed to authenticate encryption"}
        except Exception as e:
            return {"success": False, "error": f"Failed to initialize encryption: {str(e)}"}

    try:
        encrypted_id = self.cryptor.encrypt(conversation_id)
        response = self.session.post(
            f"{self.BASE_START_URL}/nachrichten.php",
            data={"a": "read", "uniqid": encrypted_id},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return {"success": True, "raw": response.text}
    except Exception as e:
        return {"success": False, "error": f"Failed to mark read: {str(e)}"}
