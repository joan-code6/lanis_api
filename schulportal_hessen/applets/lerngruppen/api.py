from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _cell_by_header(headers: List[str], cells: List[Any], key: str) -> Optional[Any]:
    if key not in headers:
        return None
    index = headers.index(key)
    if index >= len(cells):
        return None
    return cells[index]


def _parse_exam_date(text: str) -> Optional[str]:
    match = _DATE_RE.search(text)
    if not match:
        return None
    date_parts = match.group(0).split(".")
    if len(date_parts) != 3:
        return match.group(0)
    return f"{date_parts[2]}-{date_parts[1]}-{date_parts[0]}"


def _parse_teacher_group(group: Any) -> Dict[str, Any]:
    button = group.select_one("button.btn.btn-primary")
    krz = button.get_text(" ", strip=True) if button else ""
    anchor = group.select_one("ul.dropdown-menu li a")
    text = anchor.get_text(" ", strip=True) if anchor else ""
    email_match = _EMAIL_RE.search(text)
    email = email_match.group(0) if email_match else None

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
    }


def lerngruppen_get_overview(self) -> Dict[str, Any]:
    """Fetch study groups (lerngruppen.php) and exam data for the logged-in user.

    Returns:
        Dict with group and exam lists.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/lerngruppen.php")
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        exams_section = soup.find(id="klausuren")
        courses_section = soup.find(id="LGs")

        exams: List[Dict[str, Any]] = []
        if exams_section:
            exam_headers = [th.get_text(" ", strip=True) for th in exams_section.select("thead tr th")]
            for row in exams_section.select("tbody tr"):
                if row.get("data-type") != "klausur":
                    continue
                cells = row.find_all("td")
                course_id = row.get("data-lerngruppe", "")
                exam_id = row.get("data-id", "")

                date_cell = _cell_by_header(exam_headers, cells, "Datum")
                date_iso = _parse_exam_date(date_cell.get_text(" ", strip=True) if date_cell else "")

                course_cell = _cell_by_header(exam_headers, cells, "Kurs")
                course_sys_id = ""
                course_name = ""
                if course_cell:
                    small = course_cell.find("small")
                    if small:
                        course_sys_id = small.get_text(" ", strip=True)
                        small.extract()
                    course_name = course_cell.get_text(" ", strip=True)
                    if course_sys_id.startswith("(") and course_sys_id.endswith(")"):
                        course_sys_id = course_sys_id[1:-1]

                type_cell = _cell_by_header(exam_headers, cells, "Art")
                duration_cell = _cell_by_header(exam_headers, cells, "Dauer")
                hours_cell = _cell_by_header(exam_headers, cells, "Stunden")

                exams.append(
                    {
                        "id": exam_id,
                        "course_id": course_id,
                        "course_name": course_name or None,
                        "course_sys_id": course_sys_id or None,
                        "date": date_iso,
                        "type": type_cell.get_text(" ", strip=True) if type_cell else "",
                        "duration_label": duration_cell.get_text(" ", strip=True) if duration_cell else None,
                        "hours": hours_cell.get_text(" ", strip=True) if hours_cell else None,
                    }
                )

        groups: List[Dict[str, Any]] = []
        if courses_section:
            course_headers = [th.get_text(" ", strip=True) for th in courses_section.select("thead tr th")]
            for row in courses_section.select("tbody tr"):
                course_id = row.get("data-id")
                if not course_id:
                    continue
                cells = row.find_all("td")

                semester_cell = _cell_by_header(course_headers, cells, "Halbjahr")
                name_cell = _cell_by_header(course_headers, cells, "Kursname")
                teacher_cell = _cell_by_header(course_headers, cells, "Lehrkraft")

                course_sys_id = ""
                course_name = ""
                if name_cell:
                    small = name_cell.find("small")
                    if small:
                        course_sys_id = small.get_text(" ", strip=True)
                        small.extract()
                    course_name = name_cell.get_text(" ", strip=True)
                    if course_sys_id.startswith("(") and course_sys_id.endswith(")"):
                        course_sys_id = course_sys_id[1:-1]

                teachers: List[Dict[str, Any]] = []
                if teacher_cell:
                    for group in teacher_cell.select("div.btn-group"):
                        teachers.append(_parse_teacher_group(group))

                group_exams = [exam for exam in exams if exam.get("course_id") == course_id]

                groups.append(
                    {
                        "id": course_id,
                        "semester": semester_cell.get_text(" ", strip=True) if semester_cell else "",
                        "course_name": course_name or "",
                        "course_sys_id": course_sys_id or None,
                        "teachers": teachers,
                        "exams": group_exams,
                    }
                )

        return {
            "success": True,
            "groups": groups,
            "group_count": len(groups),
            "exams": exams,
            "exam_count": len(exams),
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch lerngruppen: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Failed to parse lerngruppen: {exc}"}
