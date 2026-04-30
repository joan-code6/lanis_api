from typing import Dict, Any
import json


def meinunterricht_get_overview(self) -> Dict[str, Any]:
    """
    Fetch "mein Unterricht" overview page with current entries

    Returns:
        Dict with success status and parsed course data including:
        - Current entries (recent class activities)
        - Course folders (all courses)
        - Attendance records

    Example:
        >>> api.meinunterricht_get_overview()
        {'success': True, 'entries': [...], 'html': '...'}
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(f"{self.BASE_START_URL}/meinunterricht.php")
        response.raise_for_status()

        html = response.text

        # Parse the HTML to extract course entries with BeautifulSoup
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {
                "success": False,
                "error": "BeautifulSoup4 is required for HTML parsing. Install with: pip install beautifulsoup4",
            }

        soup = BeautifulSoup(html, "html.parser")
        entries = []

        # Find all entry rows
        for row in soup.find_all("tr", {"data-book": True}):
            entry = {
                "entry_id": row.get("data-entry"),
                "book_id": row.get("data-book"),
                "name": "",
                "course_link": "",
                "teacher_full_name": "",
                "teacher_short": "",
                "teacher_message_link": "",
                "thema": "",
                "datum": "",
                "homework": "",
                "homework_done": False,
            }

            # Extract course name and link
            name_span = row.find("span", {"class": "name"})
            if name_span:
                entry["name"] = name_span.get_text(separator="\n")
                course_link = name_span.find_parent("a")
                if course_link:
                    entry["course_link"] = course_link.get("href", "")

            # Extract teacher information
            teacher_button = row.find("button", {"class": "btn-primary"})
            if teacher_button:
                # Short name from button text
                teacher_short = teacher_button.get_text(separator="\n")
                entry["teacher_short"] = teacher_short.replace("↓", "").strip()

                # Full name from title attribute
                entry["teacher_full_name"] = teacher_button.get("title", "")

                # Message link from dropdown menu
                teacher_dropdown = teacher_button.find_next_sibling("ul")
                if teacher_dropdown:
                    message_link = teacher_dropdown.find(
                        "a", title="Nachricht schreiben"
                    )
                    if message_link:
                        entry["teacher_message_link"] = message_link.get("href", "")

            # Extract topic (thema)
            thema_tag = row.find("b", {"class": "thema"})
            if thema_tag:
                entry["thema"] = thema_tag.get_text(separator="\n")

            # Extract date
            datum_span = row.find("span", {"class": "datum"})
            if datum_span:
                entry["datum"] = datum_span.get_text(separator="\n")

            # Extract homework
            homework_div = row.find("div", {"class": "homework"})
            if homework_div:
                real_homework = homework_div.find("div", {"class": "realHomework"})
                if real_homework:
                    entry["homework"] = real_homework.get_text(separator="\n")

                # Check if homework is done
                done_span = homework_div.find("span", {"class": "done"})
                if done_span and "hidden" not in done_span.get("class", []):
                    entry["homework_done"] = True

            entries.append(entry)

        return {"success": True, "entries": entries, "entry_count": len(entries)}

    except Exception as e:
        return {"success": False, "error": f"Failed to fetch mein Unterricht: {str(e)}"}


def meinunterricht_get_course(self, course_id: str) -> Dict[str, Any]:
    """
    Fetch detailed view of a specific course/class folder

    Args:
        course_id: The course book ID (from data-book attribute)

    Returns:
        Dict with success status and parsed course details including:
        - Course name and semester
        - Teacher information
        - All historical entries with dates, topics, homework
        - Attendance data (decrypted)
        - Performance records (Leistungen)
        - Exams (Leistungskontrollen)

    Example:
        >>> api.meinunterricht_get_course("1194")
        {'success': True, 'course_name': 'Informatik', 'entries': [...]}
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
            return {
                "success": False,
                "error": f"Failed to initialize encryption: {str(e)}",
            }

    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/meinunterricht.php",
            params={"a": "sus_view", "id": course_id},
        )
        response.raise_for_status()

        html = response.text

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {
                "success": False,
                "error": "BeautifulSoup4 is required. Install with: pip install beautifulsoup4",
            }

        soup = BeautifulSoup(html, "html.parser")

        # Extract course name and semester
        course_title = soup.find("h1", {"data-book": course_id})
        course_name = ""
        semester = ""
        if course_title:
            course_name = course_title.get_text(separator="\n").split("\n")[0].strip()
            semester_tag = course_title.find("span", {"class": "label-info"})
            if semester_tag:
                semester = semester_tag.get_text(separator="\n")

        # Extract teacher info
        teacher_button = soup.find("button", {"class": "btn-primary"})
        teacher_short = ""
        teacher_full = ""
        if teacher_button:
            teacher_short = (
                teacher_button.get_text(separator="\n").replace("↓", "").strip()
            )
            teacher_full = teacher_button.get("title", "")

        # Extract historical entries
        entries = []
        history_table = soup.find("table", {"class": "table-hover"})
        if history_table:
            for row in history_table.find_all("tr", {"data-entry": True}):
                entry = {
                    "entry_id": row.get("data-entry"),
                    "date": "",
                    "hours": "",
                    "thema": "",
                    "homework": "",
                    "homework_done": False,
                    "attendance": "",
                    "files": [],
                }

                # Extract date and hours
                date_cell = row.find("td")
                if date_cell:
                    date_text = date_cell.get_text(separator="\n")
                    parts = date_text.split("\n")
                    if len(parts) >= 1:
                        entry["date"] = parts[0].strip()
                    if len(parts) >= 2:
                        entry["hours"] = parts[1].strip()

                # Extract topic (thema)
                thema_tag = row.find("b")
                if thema_tag:
                    entry["thema"] = thema_tag.get_text(separator="\n")

                # Extract homework
                homework_span = row.find("span", {"class": "homework"})
                if homework_span:
                    markup = row.find("span", {"class": "markup"})
                    if markup:
                        entry["homework"] = markup.get_text(separator="\n")

                    # Check if homework is done
                    done_span = homework_span.find("span", {"class": "done"})
                    if done_span and "hidden" not in done_span.get("class", []):
                        entry["homework_done"] = True

                # Extract and decrypt attendance
                encoded_tag = row.find("encoded")
                if encoded_tag:
                    encrypted_attendance = encoded_tag.get_text(separator="\n")
                    try:
                        decrypted = self.cryptor.decrypt(encrypted_attendance)
                        # Parse the decrypted HTML to get clean attendance text
                        attendance_soup = BeautifulSoup(decrypted, "html.parser")
                        # Remove hidden elements
                        for hidden in attendance_soup.find_all(class_="hidden"):
                            hidden.decompose()
                        entry["attendance"] = attendance_soup.get_text(separator="\n")
                    except Exception as e:
                        entry["attendance"] = f"[Decryption failed: {str(e)}]"

                # Extract files
                files_div = row.find("div", {"class": "files"})
                if files_div:
                    for link in files_div.find_all("a"):
                        file_info = {
                            "name": link.get_text(separator="\n"),
                            "url": link.get("href", ""),
                        }
                        entry["files"].append(file_info)

                entries.append(entry)

        # Extract exams (Leistungskontrollen)
        exams = []
        klausuren_tab = soup.find("div", {"id": "klausuren"})
        if klausuren_tab:
            for li in klausuren_tab.find_all("li"):
                exams.append(li.get_text(separator="\n"))

        # Extract attendance summary
        attendance_summary = {}
        attendance_table = soup.find("div", {"id": "attendanceTable"})
        if attendance_table:
            for row in attendance_table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    att_type = cells[0].get_text(separator="\n")
                    att_hours = cells[1].get_text(separator="\n")
                    attendance_summary[att_type] = att_hours

        return {
            "success": True,
            "course_id": course_id,
            "course_name": course_name,
            "semester": semester,
            "teacher_short": teacher_short,
            "teacher_full": teacher_full,
            "entries": entries,
            "entry_count": len(entries),
            "exams": exams,
            "attendance_summary": attendance_summary,
        }

    except Exception as e:
        return {"success": False, "error": f"Failed to fetch course details: {str(e)}"}


def meinunterricht_get_entry_details(self, url: str) -> Dict[str, Any]:
    """
    Fetch details for a specific entry/link from mein Unterricht

    This method can fetch any linked page like external apps, forms, or detailed views.

    Args:
        url: The URL/path to fetch (e.g., "index.php?a=f&e=l4", "dateiverteilung.php", etc.)
             Can be relative path or full URL

    Returns:
        Dict with success status and content (HTML or JSON depending on response)

    Example:
        >>> api.meinunterricht_get_entry_details("index.php?a=f&e=l4")
        {'success': True, 'url': '...', 'content': '...'}
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        # Handle relative URLs
        if not url.startswith("http"):
            if not url.startswith("/"):
                url = "/" + url
            full_url = f"{self.BASE_START_URL}{url}"
        else:
            full_url = url

        response = self.session.get(full_url)
        response.raise_for_status()

        # Try to parse as JSON first
        try:
            content = response.json()
            content_type = "json"
        except json.JSONDecodeError:
            content = response.text
            content_type = "html"

        return {
            "success": True,
            "url": full_url,
            "content_type": content_type,
            "content": content,
        }

    except Exception as e:
        return {"success": False, "error": f"Failed to fetch entry details: {str(e)}"}


def meinunterricht_get_weekly_view(self) -> Dict[str, Any]:
    """
    Fetch weekly view of class entries

    Returns:
        Dict with success status and weekly entries HTML
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/meinunterricht.php", params={"a": "sus_week"}
        )
        response.raise_for_status()

        return {"success": True, "html": response.text}

    except Exception as e:
        return {"success": False, "error": f"Failed to fetch weekly view: {str(e)}"}


def meinunterricht_get_submissions(self) -> Dict[str, Any]:
    """
    Fetch student submissions/assignments (Abgaben)

    Returns:
        Dict with success status and submissions HTML
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/meinunterricht.php", params={"a": "sus_abgaben"}
        )
        response.raise_for_status()

        return {"success": True, "html": response.text}

    except Exception as e:
        return {"success": False, "error": f"Failed to fetch submissions: {str(e)}"}


def meinunterricht_set_homework_done(
    self, course_id: str, entry_id: str, done: bool = True
) -> Dict[str, Any]:
    """
    Mark or unmark homework as done for a specific entry

    Args:
        course_id: The course/book ID (from data-book attribute)
        entry_id: The entry ID (from data-entry attribute)
        done: True to mark as done, False to unmark

    Returns:
        Dict with success status

    Example:
        >>> api.meinunterricht_set_homework_done("1831", "52", True)  # Mark as done
        >>> api.meinunterricht_set_homework_done("1831", "52", False) # Unmark
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.post(
            f"{self.BASE_START_URL}/meinunterricht.php",
            data={
                "a": "sus_homeworkDone",
                "id": course_id,
                "entry": entry_id,
                "b": "done" if done else "undone",
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        response.raise_for_status()

        return {
            "success": response.text.strip() == "1",
            "course_id": course_id,
            "entry_id": entry_id,
            "done": done,
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to set homework done status: {str(e)}",
        }
