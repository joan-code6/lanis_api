from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


_DATE_TAG_RE = re.compile(r"data-tag=\"(\d{2}\.\d{2}\.\d{4})\"")
_LAST_EDIT_RE = re.compile(
    r"Letzte\s+Aktualisierung:\s*(\d{2})\.(\d{2})\.(\d{4})\s+um\s*(\d{2}):(\d{2}):(\d{2})\s+Uhr",
    re.IGNORECASE,
)


def _parse_hours(value: str) -> str:
    numbers = re.findall(r"\d+", value)
    if not numbers or len(numbers) > 2:
        return value.strip()
    if len(numbers) == 2:
        return f"{numbers[0]} - {numbers[1]}"
    return numbers[0]


def _parse_date_tag(tag: str) -> Optional[datetime]:
    tag = (tag or "").strip()
    if not tag:
        return None
    for fmt in ("%d_%m_%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(tag, fmt)
        except ValueError:
            continue
    return None


def _parse_last_updated(html: str) -> Optional[datetime]:
    match = _LAST_EDIT_RE.search(html)
    if not match:
        return None
    try:
        day, month, year, hour, minute, second = (int(value) for value in match.groups())
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _parse_info_tables(container: Optional[Any]) -> List[Dict[str, Any]]:
    if not container:
        return []

    table = container.find("table", class_="infos")
    if not table:
        return []

    infos: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        row_classes = " ".join(row.get("class", []))
        is_header = "header" in row_classes

        if is_header:
            if current:
                infos.append(current)
            header_text = cells[0].get_text(" ", strip=True)
            current = {"header": header_text, "values": []}
            continue

        if current is None:
            current = {"header": "", "values": []}

        current["values"].append(cells[0].decode_contents().strip())

    if current:
        infos.append(current)

    return infos


def _extract_ajax_dates(html: str) -> List[str]:
    dates: List[str] = []
    for match in _DATE_TAG_RE.finditer(html):
        date_str = match.group(1)
        if date_str not in dates:
            dates.append(date_str)
    return dates


def _normalize_tag_id(date_str: str) -> str:
    parsed = _parse_date_tag(date_str)
    if not parsed:
        return date_str.replace(".", "_")
    return parsed.strftime("%d_%m_%Y")


def _parse_non_ajax_day(
    soup: BeautifulSoup, date_tag: str
) -> Optional[Dict[str, Any]]:
    parsed = _parse_date_tag(date_tag)
    if not parsed:
        return None

    vtable = soup.find(id=f"vtable{date_tag}")
    if not vtable:
        return None

    headers: List[str] = []
    for th in vtable.find_all("th"):
        if th.has_attr("data-field"):
            headers.append(th.get("data-field", "").strip())

    def cell_value(name: str, fields: List[Any]) -> str:
        if name not in headers:
            return ""
        index = headers.index(name)
        if index >= len(fields):
            return ""
        return fields[index].get_text(" ", strip=True)

    substitutions: List[Dict[str, Any]] = []
    for row in vtable.select("tbody tr"):
        if row.find("td", attrs={"colspan": True}):
            continue
        fields = row.find_all("td")
        substitutions.append(
            {
                "tag": parsed.strftime("%d.%m.%Y"),
                "tag_en": parsed.strftime("%Y-%m-%d"),
                "stunde": _parse_hours(cell_value("Stunde", fields)),
                "fach": cell_value("Fach", fields) or None,
                "art": cell_value("Art", fields) or None,
                "raum": cell_value("Raum", fields) or None,
                "hinweis": cell_value("Hinweis", fields) or None,
                "lehrer": cell_value("Lehrer", fields) or None,
                "vertreter": cell_value("Vertreter", fields) or None,
                "klasse": cell_value("Klasse", fields) or None,
            }
        )

    info_container = soup.find(id=f"tag{date_tag}")
    return {
        "date": parsed.strftime("%d.%m.%Y"),
        "substitutions": substitutions,
        "infos": _parse_info_tables(info_container) or None,
    }


def _fetch_ajax_day(self, date_str: str) -> Dict[str, Any]:
    response = self.session.post(
        f"{self.BASE_START_URL}/vertretungsplan.php",
        params={"a": "my"},
        data={"tag": date_str, "ganzerPlan": "true"},
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = json.loads(response.text)

    substitutions: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            substitutions.append(
                {
                    "tag": item.get("Tag"),
                    "tag_en": item.get("Tag_en"),
                    "stunde": _parse_hours(str(item.get("Stunde", ""))),
                    "vertreter": item.get("Vertreter"),
                    "lehrer": item.get("Lehrer"),
                    "klasse": item.get("Klasse"),
                    "klasse_alt": item.get("Klasse_alt"),
                    "fach": item.get("Fach"),
                    "fach_alt": item.get("Fach_alt"),
                    "raum": item.get("Raum"),
                    "raum_alt": item.get("Raum_alt"),
                    "hinweis": item.get("Hinweis"),
                    "hinweis2": item.get("Hinweis2"),
                    "art": item.get("Art"),
                    "lehrerkuerzel": item.get("Lehrerkuerzel"),
                    "vertreterkuerzel": item.get("Vertreterkuerzel"),
                    "lerngruppe": item.get("Lerngruppe"),
                    "hervorgehoben": item.get("_hervorgehoben"),
                }
            )

    return {"date": date_str, "substitutions": substitutions}


def vertretungsplan_get_plan(self, include_raw: bool = False) -> Dict[str, Any]:
    """Fetch the substitution plan (vertretungsplan.php) for the logged-in user.

    Returns a list of substitution days with their entries. The endpoint can be in
    an AJAX or non-AJAX format; this method handles both.

    Args:
        include_raw: Include the raw HTML page in the response.

    Returns:
        Dict with success status, last_updated timestamp, and day entries.

    Example:
        >>> api.vertretungsplan_get_plan()
        {
            "success": True,
            "last_updated": "{timestamp}",
            "days": [
                {"date": "{date}", "substitutions": [{"fach": "{subject}"}]}
            ],
            "count": 1
        }
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/vertretungsplan.php")
        response.raise_for_status()

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        last_updated = _parse_last_updated(html)
        dates = _extract_ajax_dates(html)
        days: List[Dict[str, Any]] = []

        if dates:
            for date_str in dates:
                day = _fetch_ajax_day(self, date_str)
                tag_id = _normalize_tag_id(date_str)
                info_container = soup.find(id=f"tag{tag_id}")
                infos = _parse_info_tables(info_container)
                if infos:
                    day["infos"] = infos
                days.append(day)
            mode = "ajax"
        else:
            mode = "non_ajax"
            for element in soup.select("[data-tag]"):
                date_tag = element.get("data-tag", "").strip()
                if not date_tag:
                    continue
                day = _parse_non_ajax_day(soup, date_tag)
                if day:
                    days.append(day)

        total = sum(len(day.get("substitutions", [])) for day in days)
        return {
            "success": True,
            "mode": mode,
            "last_updated": last_updated.isoformat() if last_updated else None,
            "days": days,
            "count": total,
            "raw_html": html if include_raw else None,
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch vertretungsplan: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Failed to parse vertretungsplan: {exc}"}
