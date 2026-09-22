"""Student submission parsing and mutation helpers for Mein Unterricht.

The Schulportal exposes submissions as HTML pages and form actions.  This
module keeps that portal-specific shape out of the public API layer and
returns small, JSON-serialisable records instead.
"""

import base64
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from schulportal_hessen.tools.cryptor import Cryptor


def _make_absolute_url(base_url: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return url if url.startswith("http") else urljoin(f"{base_url}/", url)


def _encode_ref(url: str, base_url: str) -> str:
    absolute = _make_absolute_url(base_url, url)
    parsed = urlparse(absolute)
    relative = parsed.path.lstrip("/")
    if parsed.query:
        relative = f"{relative}?{parsed.query}"
    return base64.urlsafe_b64encode(relative.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_ref(ref: str, base_url: str) -> str:
    if not ref or len(ref) > 4096:
        raise ValueError("Invalid submission reference")
    padding = "=" * (-len(ref) % 4)
    try:
        relative = base64.urlsafe_b64decode(f"{ref}{padding}").decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid submission reference") from exc
    if not relative or not relative.split("?", 1)[0] in {
        "meinunterricht.php",
        "dateiverteilung.php",
    }:
        raise ValueError("Invalid submission reference")
    return _make_absolute_url(base_url, relative)


def _query_value(url: str, key: str) -> str:
    return parse_qs(urlparse(url).query).get(key, [""])[0].strip()


def _is_submission_url(url: str) -> bool:
    return parse_qs(urlparse(url).query).get("a", [""])[0].casefold() == "sus_abgabe"


def _submission_count(container: Any) -> tuple[str | None, int | None]:
    for node in container.select("span.label, span.badge"):
        value = node.get_text(" ", strip=True)
        match = re.search(r"(\d+)\s+Datei(?:en)?\b", value, re.IGNORECASE)
        if match:
            return value, int(match.group(1))
    return None, None


def _submission_status(container: Any) -> str:
    for node in container.select("span.label"):
        text = node.get_text(" ", strip=True).casefold()
        if "aktuell möglich" in text or (
            "möglich" in text and "nicht" not in text
        ):
            return "open"
        if "nicht möglich" in text or "nicht mehr" in text:
            return "closed"
    return "open" if container.select_one(".btn-warning") else "closed"


def _button_copy(button: Any) -> Any:
    """Return a detached button copy without badges/date metadata."""
    from bs4 import BeautifulSoup

    copy = BeautifulSoup(str(button), "html.parser").find()
    if copy is None:
        return button
    for metadata in copy.select("small, span.badge"):
        metadata.decompose()
    return copy


def _extract_upload_from_group(group: Any, base_url: str) -> dict[str, Any] | None:
    open_button = group.select_one(".btn-warning")
    closed_button = group.select_one(".btn-default")
    button = open_button or closed_button
    link = group.select_one("ul.dropdown-menu li a[href]")
    if button is None or link is None:
        return None

    href = link.get("href", "").strip()
    if not href:
        return None
    absolute_url = _make_absolute_url(base_url, href)
    if not _is_submission_url(absolute_url):
        return None

    badge = button.select_one("span.badge")
    uploaded_raw = badge.get_text(" ", strip=True) if badge else None
    uploaded_count = None
    if uploaded_raw:
        match = re.search(r"\d+", uploaded_raw)
        uploaded_count = int(match.group(0)) if match else None

    small = button.select_one("small")
    date_text = small.get_text(" ", strip=True) if small else None
    name = _button_copy(button).get_text(" ", strip=True)
    if not name:
        name = link.get_text(" ", strip=True)

    status = "open" if open_button is not None else "closed"
    return {
        "id": _encode_ref(absolute_url, base_url),
        "detail_ref": _encode_ref(absolute_url, base_url),
        "title": name,
        "status": status,
        "date_text": date_text,
        "uploaded": uploaded_raw,
        "uploaded_count": uploaded_count,
        "course_id": _query_value(absolute_url, "b"),
        "entry_id": _query_value(absolute_url, "e"),
    }


def _extract_submission_link(link: Any, base_url: str) -> dict[str, Any] | None:
    absolute_url = _make_absolute_url(base_url, link.get("href", ""))
    if not absolute_url or not _is_submission_url(absolute_url):
        return None

    container = link.find_parent("tr") or link.parent or link
    uploaded_raw, uploaded_count = _submission_count(container)
    small = container.select_one("small")
    title_node = link.select_one("b") or link
    title = title_node.get_text(" ", strip=True) or "Abgabe"
    return {
        "id": _encode_ref(absolute_url, base_url),
        "detail_ref": _encode_ref(absolute_url, base_url),
        "title": title,
        "status": _submission_status(container),
        "date_text": small.get_text(" ", strip=True) if small else None,
        "uploaded": uploaded_raw,
        "uploaded_count": uploaded_count,
        "course_id": _query_value(absolute_url, "b"),
        "entry_id": _query_value(absolute_url, "e"),
    }


def extract_entry_uploads(row: Any, base_url: str) -> list[dict[str, Any]]:
    """Extract upload actions attached to one course-history row."""
    uploads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in row.select("div.btn-group"):
        upload = _extract_upload_from_group(group, base_url)
        if upload and upload["id"] not in seen:
            uploads.append(upload)
            seen.add(upload["id"])
    return uploads


def _nearest_course_name(element: Any) -> str:
    for parent in element.parents:
        if parent.name not in {"tr", "div", "li", "section"}:
            continue
        course_link = parent.select_one("a[href*='sus_view']")
        if course_link:
            name = course_link.select_one(".name") or course_link
            return name.get_text(" ", strip=True)
    return ""


def parse_submission_summaries(html: str, base_url: str) -> list[dict[str, Any]]:
    """Parse the global ``sus_abgaben`` page into summary records."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    records: dict[str, dict[str, Any]] = {}

    for group in soup.select("div.btn-group"):
        upload = _extract_upload_from_group(group, base_url)
        if upload:
            upload["course_name"] = _nearest_course_name(group)
            records[upload["id"]] = upload

    # Some portal versions render the global page as a table of direct links
    # instead of button groups.  Keep the parser tolerant of both layouts,
    # while requiring the exact submission action so navigation links to
    # ``sus_abgaben`` are never treated as assignments.
    for link in soup.select("a[href]"):
        upload = _extract_submission_link(link, base_url)
        if upload is None:
            continue
        ref = upload["id"]
        if ref in records:
            continue
        upload["course_name"] = _nearest_course_name(link)
        records[ref] = upload

    return list(records.values())


def _sibling_label(icon: Any, class_name: str) -> str | None:
    for sibling in icon.next_siblings:
        if getattr(sibling, "name", None) is None:
            continue
        if sibling.name == "span" and class_name in sibling.get("class", []):
            return sibling.get_text(" ", strip=True)
        if sibling.name not in {"span", "i"}:
            break
    return None


def _file_record(link: Any, base_url: str, public: bool = False) -> dict[str, Any] | None:
    href = link.get("href", "").strip()
    match = re.search(r"(?:^|[?&])f=(\d+)", href)
    if not href or not match:
        return None
    parent = link.find_parent("li") or link.parent
    small = parent.select_one("small") if parent else None
    person = parent.select_one("span.label-info") if parent else None
    return {
        "name": link.get_text(" ", strip=True),
        "index": match.group(1),
        "time": small.get_text(" ", strip=True) if small else None,
        "comment": None,
        "person": person.get_text(" ", strip=True) if person else None,
        "download_ref": _encode_ref(_make_absolute_url(base_url, href), base_url),
        "public": public,
    }


def parse_submission_detail(
    html: str, base_url: str, detail_ref: str, source_url: str | None = None
) -> dict[str, Any]:
    """Parse one upload page, including its form and existing files."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    form = None
    for candidate in soup.select("form"):
        names = {field.get("name") for field in candidate.select("input[name]")}
        if {"b", "e", "id"}.issubset(names):
            form = candidate
            break

    reference_query = parse_qs(urlparse(source_url or "").query)
    form_values = {}
    for name in ("b", "e", "id"):
        field = form.select_one(f"input[name='{name}']") if form else None
        form_values[name] = (
            field.get("value", "")
            if field is not None
            else reference_query.get(name, [""])[0]
        )

    groups = soup.select("#content div.row div.col-md-12")
    requirements = next(
        (
            group
            for group in groups
            if group.select_one("span.editable, i.fa-trash-o, i.fa-file")
        ),
        soup,
    )
    editable = requirements.select("span.editable")
    start = editable[0].get_text(" ", strip=True).replace(" ab", "") if editable else None
    deadline_node = requirements.select_one("b span.editable")
    deadline = (
        deadline_node.get_text(" ", strip=True).replace(" spätestens", "")
        if deadline_node
        else None
    )

    checks = requirements.select("i.fa-check-square-o")
    multiple_files = _sibling_label(checks[0], "label-success") == "erlaubt" if len(checks) > 0 else None
    multiple_attempts = _sibling_label(checks[1], "label-success") == "erlaubt" if len(checks) > 1 else None

    eye = requirements.select_one("i.fa-eye, i.fa-eye-slash")
    visibility = None
    if eye:
        visibility = next(
            (
                sibling.get_text(" ", strip=True)
                for sibling in eye.next_siblings
                if getattr(sibling, "name", None) == "span"
            ),
            None,
        )

    deletion_icon = requirements.select_one("i.fa-trash-o")
    automatic_deletion = _sibling_label(deletion_icon, "label-info") if deletion_icon else None
    file_labels = requirements.select("i.fa-file + span.label-warning")
    allowed_file_types = file_labels[0].get_text(" ", strip=True).split(", ") if file_labels else []
    max_file_size = file_labels[1].get_text(" ", strip=True) if len(file_labels) > 1 else None
    alert = requirements.select_one("div.alert.alert-info")

    own_files: list[dict[str, Any]] = []
    public_files: list[dict[str, Any]] = []
    file_groups = soup.select(
        "#content div.row div.col-md-7, "
        "#content div.row div.col-md-5, "
        "#content div.row div.col-md-12"
    )
    file_records: dict[str, dict[str, Any]] = {}
    for group in file_groups:
        is_public = bool(group.select_one("span.label-info") and "col-md-5" in group.get("class", []))
        for link in group.select("ul li a[href*='f=']"):
            record = _file_record(link, base_url, public=is_public)
            if record:
                existing = file_records.get(record["index"])
                if existing is None or (is_public and not existing.get("public")):
                    file_records[record["index"]] = record
    for record in file_records.values():
        (public_files if record.get("public") else own_files).append(record)

    can_upload = form is not None and bool(form_values["b"])
    title_node = soup.select_one("#content h1")
    course_node = soup.select_one("a[href*='a=sus_view']")
    title = title_node.get_text(" ", strip=True) if title_node else "Abgabe"
    course_name = course_node.get_text(" ", strip=True) if course_node else ""
    uploaded_count = len(own_files)

    return {
        "success": True,
        "id": detail_ref,
        "detail_ref": detail_ref,
        "course_id": form_values["b"],
        "entry_id": form_values["e"],
        "upload_id": form_values["id"],
        "title": title,
        "course_name": course_name,
        "status": "open" if can_upload else "closed",
        "date_text": deadline,
        "uploaded": f"{uploaded_count} Datei" if uploaded_count == 1 else f"{uploaded_count} Dateien",
        "uploaded_count": uploaded_count,
        "start": start,
        "deadline": deadline,
        "automatic_deletion": automatic_deletion,
        "allows_multiple_files": multiple_files,
        "allows_multiple_attempts": multiple_attempts,
        "visibility": visibility,
        "allowed_file_types": allowed_file_types,
        "max_file_size": max_file_size,
        "additional_text": alert.get_text(" ", strip=True) if alert else None,
        "own_files": own_files,
        "public_files": public_files,
        "can_upload": can_upload,
        "can_delete": bool(own_files),
    }


def parse_upload_statuses(html: str) -> list[dict[str, str | None]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    statuses: list[dict[str, str | None]] = []
    for item in soup.select("#content li"):
        name_node = item.select_one("b")
        status_node = item.select_one("span.label")
        if not name_node or not status_node:
            continue
        full_text = item.get_text(" ", strip=True)
        name = name_node.get_text(" ", strip=True)
        status = status_node.get_text(" ", strip=True)
        message = full_text.replace(name, "", 1).replace(status, "", 1).strip(" :–-") or None
        statuses.append({"name": name, "status": status, "message": message})
    return statuses


def _ensure_cryptor(client: Any) -> Cryptor:
    if not client.cryptor:
        client.cryptor = Cryptor(client.session)
    if not client.cryptor.authenticated and not client.cryptor.authenticate():
        raise ValueError("Failed to initialize encryption")
    return client.cryptor


def meinunterricht_get_submissions(self) -> dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/meinunterricht.php", params={"a": "sus_abgaben"}
        )
        response.raise_for_status()
        return {
            "success": True,
            "submissions": parse_submission_summaries(response.text, self.BASE_START_URL),
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch submissions: {exc}"}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to fetch submissions: {exc}"}


def meinunterricht_get_submission(self, detail_ref: str) -> dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    try:
        url = _decode_ref(detail_ref, self.BASE_START_URL)
        response = self.session.get(url)
        response.raise_for_status()
        return parse_submission_detail(
            response.text, self.BASE_START_URL, detail_ref, source_url=url
        )
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch submission: {exc}"}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to fetch submission: {exc}"}


def meinunterricht_upload_files(
    self, course_id: str, entry_id: str, upload_id: str, files: list[dict[str, Any]]
) -> dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    if not files or len(files) > 5:
        return {"success": False, "error": "Between one and five files are required"}
    try:
        multipart = []
        for index, file in enumerate(files, start=1):
            stream = file.get("stream")
            if stream is not None:
                stream.seek(0)
            multipart.append(
                (
                    f"file{index}",
                    (
                        file.get("filename") or f"upload-{index}",
                        stream if stream is not None else file.get("content", b""),
                        file.get("content_type") or "application/octet-stream",
                    ),
                )
            )
        response = self.session.post(
            f"{self.BASE_START_URL}/meinunterricht.php",
            data={"a": "sus_abgabe", "b": course_id, "e": entry_id, "id": upload_id},
            files=multipart,
            headers={"Accept": "*/*"},
        )
        response.raise_for_status()
        statuses = parse_upload_statuses(response.text)
        if not statuses and response.text.strip() != "1":
            return {"success": False, "error": "Schulportal returned no upload status"}
        return {
            "success": True,
            "files": statuses,
            "all_succeeded": bool(statuses) and all(
                item["status"] == "erfolgreich" for item in statuses
            ),
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to upload files: {exc}"}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to upload files: {exc}"}


def meinunterricht_delete_uploaded_file(
    self,
    course_id: str,
    entry_id: str,
    upload_id: str,
    file_index: str,
    password: str,
) -> dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    if not password:
        return {"success": False, "error": "Password is required"}
    try:
        encrypted_password = _ensure_cryptor(self).encrypt(password)
        response = self.session.post(
            f"{self.BASE_START_URL}/meinunterricht.php",
            params={"a": "sus_abgabe"},
            data={
                "a": "sus_abgabe",
                "d": "delete",
                "b": course_id,
                "e": entry_id,
                "id": upload_id,
                "f": file_index,
                "pw": encrypted_password,
            },
            headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        code = response.text.strip()
        messages = {
            "-1": "Wrong password",
            "-2": "File deletion is not allowed",
            "0": "Unknown deletion error",
            "1": "File deleted successfully",
        }
        return {
            "success": code == "1",
            "code": code,
            "message": messages.get(code, "Unknown deletion result"),
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to delete file: {exc}"}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to delete file: {exc}"}


def meinunterricht_download_submission_file(self, file_ref: str) -> dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    try:
        url = _decode_ref(file_ref, self.BASE_START_URL)
        return self.meinunterricht_download_file(url)
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to download submission file: {exc}"}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to download submission file: {exc}"}
