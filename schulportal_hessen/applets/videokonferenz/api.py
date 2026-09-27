"""Read the student's native Schulportal video-room overview.

The upstream module is a server-rendered page whose presentation has changed
over time.  Parsing is deliberately based on the German column/action labels,
with class names used only as a fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

_ROOM_ID_KEYS = ("room", "raum", "id", "kurs", "course", "lerngruppe", "group", "lg")
_UPDATED_RE = re.compile(
    r"(?:Stand|Ansicht\s+aktuell(?:\s+seit)?|aktualisiert(?:\s+am)?)\s*:?\s*"
    r"((?:\d{1,2}\.\d{1,2}\.\d{2,4}(?:\s+um)?\s+)?"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*Uhr)",
    re.IGNORECASE,
)


def _text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _safe_url(value: str, source_url: str) -> str | None:
    if not value or value.startswith(("#", "javascript:", "data:")):
        return None
    absolute = urljoin(source_url, value)
    parsed = urlsplit(absolute)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return absolute


def _room_id(join_url: str | None, name: str, position: int, node: Any) -> str:
    for attribute in ("data-room-id", "data-id", "data-room", "data-kurs"):
        value = str(node.get(attribute) or "").strip()
        if value:
            return value
    if join_url:
        query = parse_qs(urlsplit(join_url).query)
        for key in _ROOM_ID_KEYS:
            values = query.get(key)
            if values and values[0].strip():
                return values[0].strip()
    seed = f"{name}|{join_url or ''}|{position}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _status(action_text: str, action_node: Any, join_url: str | None) -> str:
    value = action_text.casefold()
    classes = " ".join(action_node.get("class", [])).casefold() if action_node else ""
    if "raum betreten" in value or "beitreten" in value or "btn-success" in classes:
        return "open"
    if (
        "raum nicht offen" in value
        or "noch nicht" in value
        or "warten" in value
        or "btn-danger" in classes
        or "btn-warning" in classes
    ):
        return "waiting"
    if not join_url or "geschlossen" in value or "beendet" in value:
        return "closed"
    return "unknown"


def _teachers(cell: Any) -> list[str]:
    if not cell:
        return []
    labelled = [_text(node) for node in cell.select(".label, .badge, li")]
    values = [value for value in labelled if value]
    if not values:
        values = [part.strip() for part in re.split(r"[,;/|\n]+", cell.get_text("\n", strip=True))]
    return list(dict.fromkeys(value for value in values if value))


def _links(cell: Any, source_url: str, action_link: Any = None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not cell:
        return result
    for link in cell.select("a[href]"):
        if action_link is not None and link is action_link:
            continue
        href = _safe_url(str(link.get("href") or ""), source_url)
        if not href:
            continue
        result.append({"label": _text(link) or "Link öffnen", "url": href})
    return result


def _parse_table(table: Any, source_url: str) -> list[dict[str, Any]]:
    header_nodes = table.select("thead th") or table.select("tr:first-child th")
    headers = [_text(cell).casefold() for cell in header_nodes]

    def column(*names: str) -> int | None:
        for index, header in enumerate(headers):
            if any(name in header for name in names):
                return index
        return None

    group_index = column("lerngruppe", "kurs", "raum")
    teacher_index = column("lehrkräft", "lehrer")
    action_index = column("aktion", "status")
    links_index = column("links", "material")
    # The production page intentionally leaves the first header blank even
    # though that column contains the Lerngruppe name.
    if group_index is None and action_index is not None and headers:
        group_index = 0
    if group_index is None or action_index is None:
        return []

    rooms: list[dict[str, Any]] = []
    rows = table.select("tbody tr")
    if not rows:
        rows = [row for row in table.select("tr") if not row.find("th")]
    for position, row in enumerate(rows):
        cells = row.find_all(["td", "th"], recursive=False)
        if max(group_index, action_index) >= len(cells):
            continue
        name = _text(cells[group_index])
        if not name:
            continue
        action_cell = cells[action_index]
        action_nodes = action_cell.select("a[href], [data-href], [data-url], button")
        action_node = next(
            (
                node
                for node in action_nodes
                if "hidden" not in (node.get("class") or [])
                and not node.has_attr("hidden")
                and "display:none" not in str(node.get("style") or "").replace(" ", "").casefold()
            ),
            action_nodes[0] if action_nodes else None,
        )
        action_target = ""
        if action_node:
            action_target = str(
                action_node.get("href")
                or action_node.get("data-href")
                or action_node.get("data-url")
                or ""
            )
        join_url = _safe_url(action_target, source_url)
        action_text = _text(action_node) or _text(action_cell)
        state = _status(action_text, action_node, join_url)
        rooms.append(
            {
                "id": _room_id(join_url, name, position, row),
                "name": name,
                "teachers": _teachers(cells[teacher_index]) if teacher_index is not None and teacher_index < len(cells) else [],
                "status": state,
                "status_label": action_text or {
                    "open": "Raum betreten",
                    "waiting": "Raum nicht offen",
                    "closed": "Raum geschlossen",
                }.get(state, "Status unbekannt"),
                "join_url": join_url,
                "can_join": bool(join_url and state == "open"),
                "links": _links(cells[links_index], source_url) if links_index is not None and links_index < len(cells) else [],
            }
        )
    return rooms


def _parse_cards(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    rooms: list[dict[str, Any]] = []
    selectors = "[data-room-id], [data-room], .videokonferenz-room, .conference-room"
    for position, card in enumerate(soup.select(selectors)):
        name_node = card.select_one(".caption, .room-name, .panel-title, h2, h3, h4")
        name = _text(name_node)
        action_node = card.select_one(
            "a[href].btn, a[href][role='button'], [data-href], [data-url]"
        )
        if not name or not action_node:
            continue
        join_url = _safe_url(
            str(
                action_node.get("href")
                or action_node.get("data-href")
                or action_node.get("data-url")
                or ""
            ),
            source_url,
        )
        action_text = _text(action_node)
        teacher_node = card.select_one(".teachers, .teacher, [data-role='teachers']")
        rooms.append(
            {
                "id": _room_id(join_url, name, position, card),
                "name": name,
                "teachers": _teachers(teacher_node),
                "status": _status(action_text, action_node, join_url),
                "status_label": action_text or "Status unbekannt",
                "join_url": join_url,
                "can_join": bool(join_url and _status(action_text, action_node, join_url) == "open"),
                "links": _links(card, source_url, action_node),
            }
        )
    return rooms


def parse_videokonferenz_overview(
    html: str,
    source_url: str = "https://start.schulportal.hessen.de/videokonferenz.php",
) -> dict[str, Any]:
    """Parse the video-room list without exposing session data or page markup."""
    soup = BeautifulSoup(html or "", "html.parser")
    source_host = (urlsplit(source_url).hostname or "").casefold()
    if (
        source_host == "login.schulportal.hessen.de"
        or soup.select_one("form[action*='login']")
        or "login.schulportal.hessen.de" in (html or "")
    ):
        return {"success": False, "error": "Schulportal session expired", "error_kind": "authentication"}

    rooms: list[dict[str, Any]] = []
    recognized = False
    for table in soup.select("table"):
        parsed = _parse_table(table, source_url)
        if parsed:
            recognized = True
            rooms.extend(parsed)
            continue
        headers = " ".join(_text(cell).casefold() for cell in table.select("th"))
        recognized = recognized or ("lerngruppe" in headers and "aktion" in headers)

    if not rooms:
        rooms = _parse_cards(soup, source_url)
        recognized = recognized or bool(rooms)

    page_text = _text(soup)
    recognized = recognized or (
        "videokonferenz" in page_text.casefold()
        and ("ansicht aktuell" in page_text.casefold() or "lerngruppe" in page_text.casefold())
    )
    updated_match = _UPDATED_RE.search(page_text)
    updated_label = updated_match.group(1).strip(" .") if updated_match and updated_match.group(1) else None

    # Prevent duplicate rows when responsive markup contains the same room twice.
    unique_rooms = list({room["id"]: room for room in rooms}.values())
    return {
        "success": True,
        "available": recognized,
        "source": "schulportal",
        "rooms": unique_rooms,
        "count": len(unique_rooms),
        "open_count": sum(room["status"] == "open" for room in unique_rooms),
        "updated_label": updated_label,
    }


def videokonferenz_get_rooms(self: Any) -> dict[str, Any]:
    """Fetch the authenticated student's current video-conference rooms."""
    if not self.logged_in:
        return {"success": False, "error": "Not logged in", "error_kind": "authentication"}
    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/videokonferenz.php",
            timeout=(10, 30),
        )
        response.raise_for_status()
        result = parse_videokonferenz_overview(
            response.text,
            getattr(response, "url", f"{self.BASE_START_URL}/videokonferenz.php"),
        )
        if not result.get("success") or not result.get("rooms"):
            result["status_live"] = bool(result.get("success"))
            return result

        try:
            status_response = self.session.get(
                f"{self.BASE_START_URL}/videokonferenz.php",
                params={"a": "sus_start", "b": "update"},
                timeout=(10, 30),
            )
            status_response.raise_for_status()
            payload = json.loads(status_response.text)
            if not isinstance(payload, list):
                raise TypeError("Unexpected video-room status response")
            open_room_ids = {str(value) for value in payload}
            for room in result["rooms"]:
                is_open = str(room["id"]) in open_room_ids
                room["status"] = "open" if is_open else "waiting"
                room["status_label"] = "Raum betreten" if is_open else "Raum nicht offen"
                room["can_join"] = bool(is_open and room.get("join_url"))
            result["open_count"] = sum(
                room["status"] == "open" for room in result["rooms"]
            )
            result["status_live"] = True
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            # The overview remains useful, but the server-rendered buttons are
            # only placeholders until this polling endpoint responds.
            for room in result["rooms"]:
                room["status"] = "unknown"
                room["status_label"] = "Aktueller Status nicht verfügbar"
                room["can_join"] = False
            result["open_count"] = 0
            result["status_live"] = False
        return result
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch video rooms: {exc}", "error_kind": "upstream"}
    except Exception as exc:  # noqa: BLE001 - normalize unexpected parser failures
        return {"success": False, "error": f"Failed to parse video rooms: {exc}", "error_kind": "parsing"}
