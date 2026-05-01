from html import unescape
from typing import Any, Dict, List, Optional
import json
import re

import requests
from bs4 import BeautifulSoup


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).lower() in {"1", "true", "yes"}


def _parse_calendar_page(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    calendar_node = soup.find(id="calender")
    calendar_meta = {
        "first_id": calendar_node.get("data-firstid", "") if calendar_node else "",
        "new_events_count": calendar_node.get("data-neuetermine", "")
        if calendar_node
        else "",
        "can_write": _parse_bool(calendar_node.get("data-canwrite"))
        if calendar_node
        else False,
        "key": calendar_node.get("data-key", "") if calendar_node else "",
        "public_view": _parse_bool(calendar_node.get("data-publicview"))
        if calendar_node
        else False,
        "institution": calendar_node.get("data-institution", "")
        if calendar_node
        else "",
        "is_admin": _parse_bool(calendar_node.get("data-isadmin"))
        if calendar_node
        else False,
    }

    page_title = ""
    title_tag = soup.find("h1", class_="hidden-xs")
    if title_tag:
        page_title = title_tag.get_text(" ", strip=True)

    inline_script = soup.find(string=re.compile(r"var startView =", re.I))
    script_text = (
        inline_script.parent.get_text("\n", strip=False)
        if inline_script and inline_script.parent
        else html
    )

    categories: List[Dict[str, Any]] = []
    for match in re.finditer(
        r"categories\.push\(\{\s*id:\s*(\d+),\s*name:'(.*?)',\s*color:'(.*?)',\s*logo:'(.*?)'\s*\}\s*\);",
        script_text,
        re.S,
    ):
        categories.append(
            {
                "id": int(match.group(1)),
                "name": unescape(match.group(2).strip()),
                "color": match.group(3).strip(),
                "logo": match.group(4).strip(),
            }
        )

    groups: List[Dict[str, Any]] = []
    for match in re.finditer(
        r"groups\.push\(\{\s*id:\s*(\d+),\s*name:\s*['\"](.*?)['\"]", script_text, re.S
    ):
        groups.append(
            {
                "id": int(match.group(1)),
                "name": unescape(match.group(2).strip()),
            }
        )

    if not groups:
        for match in re.finditer(
            r"groups\.push\(\{\s*id:\s*(\d+),\s*name:'(.*?)'\s*\}\s*\);",
            script_text,
            re.S,
        ):
            groups.append(
                {
                    "id": int(match.group(1)),
                    "name": unescape(match.group(2).strip()),
                }
            )

    export_links: List[Dict[str, str]] = []
    for link in soup.select(".btn-group.export a[href]"):
        export_links.append(
            {
                "label": link.get_text(" ", strip=True),
                "url": link.get("href", ""),
            }
        )

    return {
        "page_title": page_title,
        "calendar": calendar_meta,
        "categories": categories,
        "groups": groups,
        "export_links": export_links,
    }


def kalender_get_overview(self) -> Dict[str, Any]:
    """
    Fetch the calendar overview page and extract its metadata.

    Returns:
        Dict with success status, calendar configuration, categories, groups, and export links.

    Example:
        >>> api.kalender_get_overview()
        {'success': True, 'calendar': {'first_id': '...', 'can_write': False}, 'categories': [...]}
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/kalender.php")
        response.raise_for_status()
        parsed = _parse_calendar_page(response.text)
        return {"success": True, **parsed}
    except requests.RequestException as e:
        return {
            "success": False,
            "error": f"Failed to fetch calendar overview: {str(e)}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to parse calendar overview: {str(e)}",
        }


def _normalize_event_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = []
        for key in ("events", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        if not items:
            items = [payload]
    else:
        items = []

    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "id": item.get("Id", item.get("id", "")),
                "title": item.get("title", ""),
                "category": item.get("category", item.get("Category", "")),
                "description": item.get("description", ""),
                "start": item.get("start"),
                "end": item.get("end"),
                "all_day": item.get("allDay", item.get("all_day", False)),
                "new": item.get("Neu", item.get("new", "")),
                "editable": item.get("editable", False),
                "properties": item.get("properties", {}),
                "raw": item,
            }
        )
    return normalized


def kalender_get_events(
    self,
    year: int = 0,
    start: str = "year",
    category: str = "",
    search: str = "",
    target: str = "",
    view_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
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
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        overview = self.kalender_get_overview()
        selected_view = view_id or overview.get("calendar", {}).get("first_id", "")
        groups = overview.get("groups", [])
        categories = overview.get("categories", [])

        post_data = {
            "f": "getEvents",
            "year": year,
            "start": start,
            "k": category,
            "s": search,
            "z": target,
            "u": selected_view,
        }
        response = self.session.post(
            f"{self.BASE_START_URL}/kalender.php", data=post_data
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = json.loads(response.text)

        events = _normalize_event_payload(payload)

        for event in events:
            # Map category ID to category name and color
            if event.get("category"):
                cat_id = str(event["category"])
                for cat in categories:
                    if str(cat.get("id")) == cat_id:
                        event["category_name"] = cat.get("name", "")
                        event["category_color"] = cat.get("color", "")
                        break

        return {
            "success": True,
            "events": events,
            "count": len(events),
            "categories": categories,
            "groups": groups,
            "filters": {
                "year": year,
                "start": start,
                "category": category,
                "search": search,
                "target": target,
                "view_id": selected_view,
            },
            "raw": payload,
        }
    except requests.RequestException as e:
        return {"success": False, "error": f"Failed to fetch calendar events: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to parse calendar events: {str(e)}"}


def kalender_get_event(
    self, event_id: str, view_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Fetch a single calendar event via the same `getEvent` POST action as the UI.

    Args:
        event_id: Internal event id.
        view_id: Selected calendar view id. If omitted, the current default view is used.

    Returns:
        Dict with success status and the parsed event payload.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        overview = self.kalender_get_overview()
        selected_view = view_id or overview.get("calendar", {}).get("first_id", "")

        response = self.session.post(
            f"{self.BASE_START_URL}/kalender.php",
            data={"f": "getEvent", "id": event_id, "u": selected_view},
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = json.loads(response.text)

        return {
            "success": True,
            "event": payload,
            "filters": {
                "event_id": event_id,
                "view_id": selected_view,
            },
        }
    except requests.RequestException as e:
        return {"success": False, "error": f"Failed to fetch calendar event: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to parse calendar event: {str(e)}"}
