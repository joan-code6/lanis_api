from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


def _parse_time_range(text: str) -> Optional[Tuple[Dict[str, int], Dict[str, int]]]:
    parts = [part.strip() for part in text.split("-")]
    if len(parts) != 2:
        return None

    def to_dict(value: str) -> Dict[str, int]:
        hh_mm = value.split(":")
        if len(hh_mm) != 2:
            return {"hour": 0, "minute": 0}
        return {"hour": int(hh_mm[0]), "minute": int(hh_mm[1])}

    return to_dict(parts[0]), to_dict(parts[1])


def _parse_time_slots(tbody: Any) -> List[Tuple[Dict[str, int], Dict[str, int]]]:
    slots: List[Tuple[Dict[str, int], Dict[str, int]]] = []
    for node in tbody.select(".VonBis"):
        parsed = _parse_time_range(node.get_text(" ", strip=True))
        if parsed:
            slots.append(parsed)
    return slots


def _make_subject_id(name: str, room: str, day: int, start_time: Dict[str, int]) -> str:
    seed = f"{name}|{room}|{day}|{start_time.get('hour')}|{start_time.get('minute')}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _extract_room_text(row: Any, name: str, teacher: str, badge: str) -> str:
    text = " ".join(row.stripped_strings)
    for value in (name, teacher, badge):
        if value:
            text = text.replace(value, " ")
    return " ".join(text.split()).strip()


def _parse_single_hour(
    cell: Any,
    row_index: int,
    time_slots: List[Tuple[Dict[str, int], Dict[str, int]]],
    timeslot_offset: bool,
    day_index: int,
) -> List[Dict[str, Any]]:
    subjects: List[Dict[str, Any]] = []
    duration = int(cell.get("rowspan", "1") or "1")

    for block in cell.select(".stunde"):
        name = ""
        name_tag = block.find("b")
        if name_tag:
            name = name_tag.get_text(" ", strip=True)

        teacher = ""
        teacher_tag = block.find("small")
        if teacher_tag:
            teacher = teacher_tag.get_text(" ", strip=True)

        badge = ""
        badge_tag = block.find(class_="badge")
        if badge_tag:
            badge = badge_tag.get_text(" ", strip=True)

        room = _extract_room_text(block, name, teacher, badge)

        if timeslot_offset:
            start_index = row_index
        else:
            start_index = row_index - 1

        end_index = start_index + duration - 1
        if start_index < 0 or end_index >= len(time_slots):
            start_time = {"hour": 0, "minute": 0}
            end_time = {"hour": 0, "minute": 0}
        else:
            start_time = time_slots[start_index][0]
            end_time = time_slots[end_index][1]

        data_mix = block.get("data-mix", "")
        subject_id = data_mix.strip()
        if not subject_id:
            subject_id = _make_subject_id(name, room, day_index, start_time)

        subjects.append(
            {
                "id": f"{subject_id}-{day_index}-{start_time['hour']}-{start_time['minute']}",
                "name": name or None,
                "room": room or None,
                "teacher": teacher or None,
                "badge": badge or None,
                "duration": duration,
                "start_time": start_time,
                "end_time": end_time,
                "stunde": row_index,
            }
        )

    return subjects


def _parse_room_plan(tbody: Any) -> List[List[Dict[str, Any]]]:
    rows = tbody.find_all("tr", recursive=False)
    if not rows:
        return []

    first_row_cells = rows[0].find_all(["td", "th"], recursive=False)
    day_count = max(len(first_row_cells) - 1, 0)
    if day_count == 0:
        return []

    result: List[List[Dict[str, Any]]] = [[] for _ in range(day_count)]
    time_slots = _parse_time_slots(tbody)
    already_parsed = [[False for _ in range(day_count)] for _ in range(len(rows) + 1)]

    timeslot_offset = first_row_cells[0].get_text(" ", strip=True) != ""

    for row_index, row in enumerate(rows):
        if row_index == 0:
            continue
        cells = row.find_all(["td", "th"], recursive=False)
        for col_index, cell in enumerate(cells):
            if col_index == 0:
                continue
            row_span = int(cell.get("rowspan", "1") or "1")
            actual_day = col_index - 1
            while actual_day < day_count and already_parsed[row_index][actual_day]:
                actual_day += 1
            if actual_day >= day_count:
                continue
            for offset in range(row_span):
                if row_index + offset < len(already_parsed):
                    already_parsed[row_index + offset][actual_day] = True
            result[actual_day].extend(
                _parse_single_hour(
                    cell,
                    row_index,
                    time_slots,
                    timeslot_offset,
                    actual_day,
                )
            )

    return result


def _parse_rows(tbody: Any) -> List[Dict[str, Any]]:
    rows = tbody.find_all("tr", recursive=False)
    result: List[Dict[str, Any]] = []

    for row_index, row in enumerate(rows):
        if row_index == 0:
            continue
        time_cell = row.find(class_="VonBis")
        if not time_cell:
            continue
        parsed = _parse_time_range(time_cell.get_text(" ", strip=True))
        if not parsed:
            continue

        label_node = row.select_one(".print-show b") or row.select_one(".print-show")
        label = label_node.get_text(" ", strip=True) if label_node else ""

        result.append(
            {
                "type": "lesson",
                "start_time": parsed[0],
                "end_time": parsed[1],
                "label": label,
                "lesson_index": row_index,
            }
        )

    return result


def stundenplan_get_plan(self) -> Dict[str, Any]:
    """Fetch the timetable (stundenplan.php) for the logged-in user.

    Returns the timetable for all classes and, when available, the
    personalized timetable for the logged-in user.

    Returns:
        Dict with timetable data and hour metadata.

    Example:
        >>> api.stundenplan_get_plan()
        {"success": True, "plan_for_all": [...], "hours": [...]}
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/stundenplan.php")
        response.raise_for_status()

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        tbody_all = soup.select_one("#all tbody")
        if not tbody_all and response.headers.get("Location"):
            redirect = response.headers.get("Location")
            response = self.session.get(f"{self.BASE_START_URL}/{redirect}")
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            tbody_all = soup.select_one("#all tbody")
        tbody_own = soup.select_one("#own tbody")

        if not tbody_all:
            return {"success": False, "error": "Timetable table not found"}

        plan_all = _parse_room_plan(tbody_all)
        plan_own = _parse_room_plan(tbody_own) if tbody_own else None
        week_badge = ""
        badge_node = soup.select_one("#aktuelleWoche")
        if badge_node:
            week_badge = badge_node.get_text(" ", strip=True)

        hours = _parse_rows(tbody_all)

        return {
            "success": True,
            "plan_for_all": plan_all,
            "plan_for_own": plan_own,
            "hours": hours,
            "week_badge": week_badge or None,
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch stundenplan: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Failed to parse stundenplan: {exc}"}
