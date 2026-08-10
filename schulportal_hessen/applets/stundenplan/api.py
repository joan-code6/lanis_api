from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


Time = Dict[str, int]
TimeSlot = Tuple[Time, Time]


def _parse_time_range(text: str) -> Optional[TimeSlot]:
    """Parse a portal time range, accepting both hyphens and en dashes."""
    match = re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", text)
    if not match:
        return None

    start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
    if not (
        0 <= start_hour <= 23
        and 0 <= end_hour <= 23
        and 0 <= start_minute <= 59
        and 0 <= end_minute <= 59
    ):
        return None
    return (
        {"hour": start_hour, "minute": start_minute},
        {"hour": end_hour, "minute": end_minute},
    )


def _parse_time_slots(tbody: Any) -> List[Optional[TimeSlot]]:
    """Return one entry per lesson row, preserving malformed time rows.

    Keeping ``None`` placeholders is important: filtering malformed values out
    would shift every subsequent lesson to an earlier time slot.
    """
    slots: List[Optional[TimeSlot]] = []
    for row in tbody.find_all("tr", recursive=False):
        node = row.find(class_="VonBis")
        if node:
            slots.append(_parse_time_range(node.get_text(" ", strip=True)))
    return slots


def _make_subject_id(
    name: str,
    room: str,
    teacher: str,
    badge: str,
    day: int,
    start_time: Time,
) -> str:
    seed = "|".join(
        (
            name,
            room,
            teacher,
            badge,
            str(day),
            str(start_time.get("hour")),
            str(start_time.get("minute")),
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _extract_room_text(block: Any) -> str:
    """Get the unlabelled room text without corrupting overlapping words."""
    copy = BeautifulSoup(str(block), "html.parser")
    # Remove parents before looking for their children. Calling ``decompose``
    # on both a parent badge and a nested label leaves a destroyed Tag object
    # in BeautifulSoup's selector result on some supported bs4 releases.
    for node in copy.select(".badge"):
        node.decompose()
    for node in copy.select("b, small"):
        node.decompose()
    return " ".join(copy.stripped_strings).strip()


def _parse_single_hour(
    cell: Any,
    lesson_index: int,
    time_slots: List[Optional[TimeSlot]],
    day_index: int,
    period: Optional[int] = None,
) -> List[Dict[str, Any]]:
    subjects: List[Dict[str, Any]] = []
    try:
        duration = max(int(cell.get("rowspan", "1") or "1"), 1)
    except (TypeError, ValueError):
        duration = 1

    start_index = lesson_index - 1
    end_index = start_index + duration - 1
    if start_index < 0 or end_index >= len(time_slots):
        return subjects

    start_slot = time_slots[start_index]
    end_slot = time_slots[end_index]
    if start_slot is None or end_slot is None:
        return subjects
    start_time = start_slot[0]
    end_time = end_slot[1]
    for block in cell.select(".stunde"):
        name_node = block.find("b")
        teacher_node = block.find("small")
        badge_node = block.find(class_="badge")
        name = name_node.get_text(" ", strip=True) if name_node else ""
        teacher = teacher_node.get_text(" ", strip=True) if teacher_node else ""
        badge = badge_node.get_text(" ", strip=True) if badge_node else ""
        room = _extract_room_text(block)

        subject_id = str(block.get("data-mix", "")).strip()
        if not subject_id:
            subject_id = _make_subject_id(
                name, room, teacher, badge, day_index, start_time
            )

        subjects.append(
            {
                "id": f"{subject_id}-{day_index}-{start_time['hour']}-{start_time['minute']}",
                "name": name or None,
                "room": room or None,
                "teacher": teacher or None,
                "badge": badge or None,
                "duration": duration,
                "start_time": start_time.copy(),
                "end_time": end_time.copy(),
                "stunde": period if period is not None else lesson_index,
            }
        )
    return subjects


def _parse_room_plan(tbody: Any) -> List[List[Dict[str, Any]]]:
    rows = tbody.find_all("tr", recursive=False)
    if not rows:
        return []

    thead = tbody.parent.find("thead", recursive=False) if tbody.parent else None
    header = thead.find("tr", recursive=False) if thead else None
    header_cells = (header or rows[0]).find_all(["td", "th"], recursive=False)
    day_count = max(len(header_cells) - 1, 0)
    result: List[List[Dict[str, Any]]] = [[] for _ in range(day_count)]
    if not day_count:
        return result

    time_slots = _parse_time_slots(tbody)
    occupied = [[False] * day_count for _ in rows]
    lesson_index = 0

    # Older fixtures/pages put the header inside tbody; the live portal uses
    # a sibling thead. Only skip the first tbody row when it is actually a
    # header, otherwise period zero would be dropped and all times shifted.
    first_data_row = 0 if rows[0].select_one(".VonBis") else 1
    for row_index, row in enumerate(rows[first_data_row:], start=first_data_row):
        if row.select_one(".VonBis"):
            lesson_index += 1
        cells = row.find_all(["td", "th"], recursive=False)
        label_node = row.select_one(".print-show b") or row.select_one(".print-show")
        label_match = re.search(r"\d+", label_node.get_text(" ", strip=True)) if label_node else None
        period = int(label_match.group()) if label_match else None
        # The first cell is always the lesson/time column. Rowspans only occur
        # in day columns, so locate each remaining cell in the next free day.
        actual_day = 0
        for cell in cells[1:]:
            while actual_day < day_count and occupied[row_index][actual_day]:
                actual_day += 1
            if actual_day >= day_count:
                break
            try:
                row_span = max(int(cell.get("rowspan", "1") or "1"), 1)
            except (TypeError, ValueError):
                row_span = 1
            for offset in range(row_span):
                if row_index + offset < len(occupied):
                    occupied[row_index + offset][actual_day] = True
            result[actual_day].extend(
                _parse_single_hour(cell, lesson_index, time_slots, actual_day, period)
            )
            actual_day += 1
    return result


def _parse_rows(tbody: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for row in tbody.find_all("tr", recursive=False):
        time_cell = row.find(class_="VonBis")
        if not time_cell:
            continue
        parsed = _parse_time_range(time_cell.get_text(" ", strip=True))
        if not parsed:
            continue
        label_node = row.select_one(".print-show b") or row.select_one(".print-show")
        result.append(
            {
                "type": "lesson",
                "start_time": parsed[0],
                "end_time": parsed[1],
                "label": label_node.get_text(" ", strip=True) if label_node else "",
                "lesson_index": len(result) + 1,
            }
        )
    return result


def _day_labels(tbody: Any) -> List[str]:
    thead = tbody.parent.find("thead", recursive=False) if tbody.parent else None
    header = thead.find("tr", recursive=False) if thead else tbody.find("tr", recursive=False)
    if not header:
        return []
    cells = header.find_all(["td", "th"], recursive=False)[1:]
    return [cell.get_text(" ", strip=True) for cell in cells]


def parse_timetable_html(html: str) -> Dict[str, Any]:
    """Parse a ``stundenplan.php`` response without making a request."""
    soup = BeautifulSoup(html, "html.parser")
    tbody_all = soup.select_one("#all tbody")
    if not tbody_all:
        return {"success": False, "error": "Timetable table not found"}

    tbody_own = soup.select_one("#own tbody")
    badge_node = soup.select_one("#aktuelleWoche")
    return {
        "success": True,
        "days": _day_labels(tbody_all),
        "plan_for_all": _parse_room_plan(tbody_all),
        "plan_for_own": _parse_room_plan(tbody_own) if tbody_own else None,
        "hours": _parse_rows(tbody_all),
        "week_badge": badge_node.get_text(" ", strip=True) if badge_node else None,
    }


def stundenplan_get_plan(self) -> Dict[str, Any]:
    """Fetch and parse the authenticated user's School Portal timetable."""
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/stundenplan.php")
        response.raise_for_status()
        return parse_timetable_html(response.text)
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch stundenplan: {exc}"}
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to parse stundenplan: {exc}"}
