from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _table_headers(section: Any) -> list[str]:
    header_row = section.select_one("thead tr")
    if not header_row:
        return []
    return [
        _normalize_header(cell.get_text(" ", strip=True))
        for cell in header_row.find_all(["th", "td"], recursive=False)
    ]


def _cell_by_header(headers: list[str], cells: list[Any], *keys: str) -> Any | None:
    normalized_keys = {_normalize_header(key) for key in keys}
    for index, header in enumerate(headers):
        if header in normalized_keys and index < len(cells):
            return cells[index]
    return None


def _cell_with(cells: list[Any], selector: str) -> Any | None:
    return next((cell for cell in cells if cell.select_one(selector)), None)


def _text_without_small(cell: Any | None) -> tuple[str, str | None]:
    if cell is None:
        return "", None
    clone = BeautifulSoup(str(cell), "html.parser")
    small = clone.find("small")
    system_id = small.get_text(" ", strip=True) if small else ""
    if small:
        small.decompose()
    if system_id.startswith("(") and system_id.endswith(")"):
        system_id = system_id[1:-1]
    return clone.get_text(" ", strip=True), system_id or None


def _parse_exam_date(text: str) -> str | None:
    match = _DATE_RE.search(text)
    if not match:
        return None
    date_parts = match.group(0).split(".")
    if len(date_parts) != 3:
        return match.group(0)
    return f"{date_parts[2]}-{date_parts[1]}-{date_parts[0]}"


def _parse_teacher_group(group: Any) -> dict[str, Any]:
    button = group.select_one("button.btn.btn-primary, button.dropdown-toggle, button")
    krz = button.get_text(" ", strip=True) if button else ""
    anchor = group.select_one(
        "ul.dropdown-menu li a, .dropdown-menu a, a[href^='mailto:']"
    )
    text = anchor.get_text(" ", strip=True) if anchor else ""
    href = anchor.get("href", "") if anchor else ""
    email_match = _EMAIL_RE.search(f"{text} {href}")
    email = email_match.group(0) if email_match else None

    recipient_id = None
    for message_anchor in group.select("a[href]"):
        values = parse_qs(urlparse(message_anchor.get("href", "")).query).get("to[]")
        if not values:
            continue
        try:
            # Schulportal currently appends a stray closing brace to this query
            # value (for example ``bC0xMjg2NTY=}``). Keep only the Base64 token.
            token_match = re.match(r"[A-Za-z0-9+/]+={0,2}", values[0])
            if not token_match:
                continue
            encoded_id = token_match.group(0)
            encoded_id += "=" * (-len(encoded_id) % 4)
            decoded_id = base64.b64decode(encoded_id, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if re.fullmatch(r"[a-z]-\d+", decoded_id, re.IGNORECASE):
            recipient_id = decoded_id
            break

    name_text = text
    if email:
        name_text = name_text.replace(email, "").strip(" ,")

    first_name = ""
    last_name = ""
    if "," in name_text:
        parts = [part.strip() for part in name_text.split(",", 1)]
        last_name = parts[0]
        first_name = parts[1] if len(parts) > 1 else ""
    elif name_text:
        name_parts = name_text.split()
        if len(name_parts) > 1:
            first_name = " ".join(name_parts[1:])
            last_name = name_parts[0]
        else:
            last_name = name_text

    return {
        "krz": krz,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "recipient_id": recipient_id,
    }


def parse_lerngruppen_html(html: str) -> dict[str, Any]:
    """Parse study groups and exams from a ``lerngruppen.php`` response."""
    soup = BeautifulSoup(html, "html.parser")
    exams_section = soup.find(id="klausuren")
    courses_section = soup.find(id="LGs")

    exams: list[dict[str, Any]] = []
    if exams_section:
        exam_headers = _table_headers(exams_section)
        for row in exams_section.select("tbody tr[data-type='klausur']"):
            cells = row.find_all("td", recursive=False)
            course_id = row.get("data-lerngruppe", "")
            exam_id = row.get("data-id", "")

            date_cell = _cell_by_header(exam_headers, cells, "Datum", "Termin")
            if date_cell is None:
                date_cell = next(
                    (
                        cell
                        for cell in cells
                        if _DATE_RE.search(cell.get_text(" ", strip=True))
                    ),
                    None,
                )

            course_cell = _cell_by_header(
                exam_headers, cells, "Kurs", "Kursname", "Lerngruppe"
            ) or _cell_with(cells, "small")
            course_name, course_sys_id = _text_without_small(course_cell)

            type_cell = _cell_by_header(exam_headers, cells, "Art", "Typ")
            hours_cell = _cell_by_header(
                exam_headers, cells, "Stunden", "Unterrichtsstunden"
            )
            duration_cell = _cell_by_header(
                exam_headers, cells, "Dauer", "Bearbeitungszeit"
            )

            # The legacy portal has no reliable header markup, but its exam cells
            # have consistently been date, course, type, hours, duration.
            if not exam_headers:
                type_cell = type_cell or (cells[2] if len(cells) > 2 else None)
                hours_cell = hours_cell or (cells[3] if len(cells) > 3 else None)
                duration_cell = duration_cell or (cells[4] if len(cells) > 4 else None)

            exams.append(
                {
                    "id": exam_id,
                    "course_id": course_id,
                    "course_name": course_name or None,
                    "course_sys_id": course_sys_id,
                    "date": _parse_exam_date(
                        date_cell.get_text(" ", strip=True) if date_cell else ""
                    ),
                    "type": type_cell.get_text(" ", strip=True) if type_cell else "",
                    "duration_label": (
                        duration_cell.get_text(" ", strip=True)
                        if duration_cell
                        else None
                    ),
                    "hours": hours_cell.get_text(" ", strip=True)
                    if hours_cell
                    else None,
                }
            )

    groups: list[dict[str, Any]] = []
    if courses_section:
        course_headers = _table_headers(courses_section)
        for row in courses_section.select("tbody tr[data-id]"):
            course_id = row.get("data-id")
            if not course_id:
                continue
            cells = row.find_all("td", recursive=False)

            name_cell = _cell_by_header(
                course_headers, cells, "Kursname", "Kurs", "Lerngruppe"
            ) or _cell_with(cells, "small")
            teacher_cell = _cell_by_header(
                course_headers, cells, "Lehrkraft", "Lehrkräfte", "Lehrer"
            ) or _cell_with(cells, ".btn-group, .dropdown-menu")
            semester_cell = _cell_by_header(
                course_headers, cells, "Halbjahr", "Schulhalbjahr", "Semester"
            )

            course_name, course_sys_id = _text_without_small(name_cell)
            if name_cell is None:
                name_cell = next(
                    (
                        cell
                        for cell in cells
                        if cell is not teacher_cell
                        and cell is not semester_cell
                        and cell.get_text(" ", strip=True)
                    ),
                    None,
                )
                course_name, course_sys_id = _text_without_small(name_cell)

            if semester_cell is None:
                semester_cell = next(
                    (
                        cell
                        for cell in cells
                        if cell is not name_cell
                        and cell is not teacher_cell
                        and re.search(
                            r"(?:halbjahr|semester|schuljahr|\b20\d{2}/\d{2,4}\b)",
                            cell.get_text(" ", strip=True),
                            re.IGNORECASE,
                        )
                    ),
                    None,
                )

            teachers: list[dict[str, Any]] = []
            if teacher_cell:
                teacher_groups = teacher_cell.select(".btn-group")
                if not teacher_groups and teacher_cell.select_one(
                    "button, .dropdown-menu"
                ):
                    teacher_groups = [teacher_cell]
                teachers = [_parse_teacher_group(group) for group in teacher_groups]

            groups.append(
                {
                    "id": course_id,
                    "semester": (
                        semester_cell.get_text(" ", strip=True) if semester_cell else ""
                    ),
                    "course_name": course_name,
                    "course_sys_id": course_sys_id,
                    "teachers": teachers,
                    "exams": [
                        exam for exam in exams if exam.get("course_id") == course_id
                    ],
                }
            )

    return {
        "success": True,
        "groups": groups,
        "group_count": len(groups),
        "exams": exams,
        "exam_count": len(exams),
    }


def lerngruppen_get_overview(self) -> dict[str, Any]:
    """Fetch study groups (lerngruppen.php) and exam data for the logged-in user.

    Returns:
        Dict with group and exam lists.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/lerngruppen.php")
        response.raise_for_status()

        return parse_lerngruppen_html(response.text)
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch lerngruppen: {exc}"}
    except (AttributeError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to parse lerngruppen: {exc}"}
