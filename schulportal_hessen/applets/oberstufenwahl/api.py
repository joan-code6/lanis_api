"""Read and submit student-facing Oberstufenwahl forms.

The SPH module is server-rendered HTML. Its JavaScript turns the visible form
into a ``kurse`` JSON array before submitting it. This module exposes a stable,
sanitized representation to consumers and keeps the portal-specific wire format
in one place.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


_ELECTION_LINK_RE = re.compile(r"(?:^|/)oberstufenwahl\.php(?:\?|$)", re.I)
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _text(element: Any) -> str:
    return element.get_text(" ", strip=True) if element else ""


def _absolute(base_url: str, href: str) -> str:
    return urljoin(base_url, href or "")


def _parse_date(value: str) -> Optional[str]:
    match = _DATE_RE.search(value)
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return datetime.strptime(f"{day}.{month}.{year}", "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _participation_metadata(text: str) -> Dict[str, Optional[str]]:
    dates = [_parse_date(match.group(0)) for match in _DATE_RE.finditer(text)]
    dates = [value for value in dates if value]
    return {
        "participation_text": text,
        "starts_on": dates[0] if len(dates) > 0 else None,
        "ends_on": dates[1] if len(dates) > 1 else None,
    }


def parse_oberstufenwahl_overview(
    html: str, base_url: str = "https://start.schulportal.hessen.de/"
) -> Dict[str, Any]:
    """Parse the authenticated election list page without retaining raw HTML."""

    soup = BeautifulSoup(html, "html.parser")
    elections: List[Dict[str, Any]] = []
    seen: set[str] = set()

    anchors_by_id: Dict[str, List[Any]] = {}
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        absolute = _absolute(base_url, href)
        parsed = urlparse(absolute)
        if not _ELECTION_LINK_RE.search(parsed.path):
            continue
        election_id = parse_qs(parsed.query).get("w", [""])[0].strip()
        if not election_id:
            continue
        anchors_by_id.setdefault(election_id, []).append(anchor)

    for election_id, candidates in anchors_by_id.items():
        if election_id in seen:
            continue
        seen.add(election_id)
        # The title link may live in the navigation while the same election
        # ID is repeated by a button in the content panel. Prefer the title
        # text for the label and use the panel copy for dates below.
        anchor = next(
            (
                candidate
                for candidate in candidates
                if "btn" not in (candidate.get("class") or [])
            ),
            candidates[0],
        )
        panel_anchor = next(
            (
                candidate
                for candidate in candidates
                if candidate.find_parent(class_="panel")
            ),
            anchor,
        )

        container = panel_anchor.find_parent(class_="panel") or panel_anchor.parent
        container_text = _text(container)
        if len(container_text) < len(_text(anchor)) + 10 and container.parent:
            container_text = _text(container.parent)
        if not _DATE_RE.search(container_text):
            page_text = _text(soup)
            if len(anchors_by_id) == 1:
                container_text = page_text
        metadata = _participation_metadata(container_text)
        info_link = next(
            (
                _absolute(base_url, candidate.get("href", ""))
                for candidate in soup.select("a[href]")
                if "oberstufenkurswahl" in candidate.get("href", "").lower()
            ),
        )
        elections.append(
            {
                "id": election_id,
                "title": _text(anchor),
                "url": absolute,
                "info_url": info_link,
                **metadata,
            }
        )

    return {
        "success": True,
        "elections": elections,
        "election_count": len(elections),
    }


def _option(option: Any) -> Dict[str, Any]:
    # HTML omits value for the class selector in the live module. Browsers use
    # the option text as the value in that case, so mirror that behavior.
    text = _text(option)
    value = option.get("value")
    if value is None:
        value = text
    return {
        "value": str(value),
        "label": text,
        "disabled": option.has_attr("disabled"),
        "excludes": _as_json(option.get("data-hidetoo"), []),
        "course": option.get("data-fach") or str(value),
    }


def _label_for(control: Any) -> str:
    label = control.find_parent("label")
    if not label:
        return ""
    clone = BeautifulSoup(str(label), "html.parser")
    for nested in clone.find_all(["input", "select", "option"]):
        nested.decompose()
    return _text(clone)


def _parse_personal_fields(section: Any) -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    for control in section.select("[id^='bemerkung']"):
        if control.name not in {"input", "select", "textarea"}:
            continue
        field: Dict[str, Any] = {
            "id": control.get("id", ""),
            "kind": "select"
            if control.name == "select"
            else control.get("type", "text"),
            "label": _label_for(control),
            "required": True,
        }
        if control.name == "select":
            field["options"] = [
                _option(option) for option in control.find_all("option")
            ]
        fields.append(field)
    return fields


def _parse_control(
    control: Any, block_name: str, group_name: str = ""
) -> Dict[str, Any]:
    if control.name == "select":
        control_id = (
            control.get("id") or f"select:{block_name}:{control.get('data-nr', '')}"
        )
        return {
            "id": control_id,
            "kind": "select",
            "label": _label_for(control),
            "block": block_name,
            "group": group_name,
            "required": control.get("data-type") == "must",
            "order": _as_int(control.get("data-nr"), 0),
            "not_blocks": _as_json(control.get("data-not"), []),
            "options": [_option(option) for option in control.find_all("option")],
        }

    name = str(control.get("data-name") or "").strip()
    return {
        "id": f"checkbox:{block_name}:{name}",
        "kind": "checkbox",
        "label": _label_for(control) or name,
        "name": name,
        "block": block_name,
        "group": group_name,
        "min": _as_int(control.get("data-min"), 0),
        "max": _as_int(control.get("data-max"), 1),
        "not_blocks": _as_json(control.get("data-not"), []),
        "teacher_options": _as_json(control.get("data-auswahl"), []),
    }


def parse_oberstufenwahl_form(
    html: str,
    election_id: str = "",
    source_url: str = "https://start.schulportal.hessen.de/oberstufenwahl.php",
) -> Dict[str, Any]:
    """Parse a dynamic election form into a JSON-safe domain model."""

    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return {"success": False, "error": "Election form was not found"}

    heading = soup.find("h1")
    blocks: List[Dict[str, Any]] = []
    personal_fields: List[Dict[str, Any]] = []

    for block in form.select(".block"):
        title = block.find("h2")
        if not title:
            continue
        name = str(title.get("data-name") or _text(title)).strip()
        block_data: Dict[str, Any] = {
            "name": name,
            "min": _as_int(title.get("data-min"), 0),
            "max": _as_int(title.get("data-max"), 0),
            "first_only": str(title.get("data-firstonly") or "") == "1",
            "multi_type": title.get("data-multitype") or "",
            "description": " ".join(
                _text(node) for node in block.select(".alert, .form-text, .help-block")
            ),
            "controls": [],
            "groups": [],
        }

        if block_data["max"] == -1:
            personal_fields = _parse_personal_fields(block)
            block_data["fields"] = personal_fields
        else:
            for select in block.select("select[data-nr]"):
                block_data["controls"].append(_parse_control(select, name))
            for subblock in block.select(".subblock"):
                subheading = subblock.find("h3")
                group_name = str(
                    subheading.get("data-name") or _text(subheading)
                ).strip()
                group = {
                    "name": group_name,
                    "min": _as_int(subheading.get("data-min"), 0) if subheading else 0,
                    "max": _as_int(subheading.get("data-max"), 0) if subheading else 0,
                    "controls": [],
                }
                for checkbox in subblock.select("input[type='checkbox'][data-name]"):
                    parsed = _parse_control(checkbox, name, group_name)
                    group["controls"].append(parsed)
                    block_data["controls"].append(parsed)
                block_data["groups"].append(group)
            for checkbox in block.select("input[type='checkbox'][data-name]"):
                if not checkbox.find_parent(class_="subblock"):
                    block_data["controls"].append(_parse_control(checkbox, name))

        blocks.append(block_data)

    return {
        "success": True,
        "election": {
            "id": str(election_id),
            "title": _text(heading),
            "source_url": source_url,
        },
        "personal_fields": personal_fields,
        "blocks": blocks,
    }


def _selection_value(selections: Dict[str, Any], control_id: str) -> Any:
    return selections.get(control_id)


def validate_oberstufenwahl_submission(
    form: Dict[str, Any], submission: Dict[str, Any]
) -> List[str]:
    """Validate answers against the parsed form before any upstream POST."""

    fields = submission.get("fields") or {}
    selections = submission.get("selections") or {}
    errors: List[str] = []

    for field in form.get("personal_fields", []):
        if not str(fields.get(field.get("id"), "")).strip():
            errors.append(f"{field.get('label') or field.get('id')} ist erforderlich.")

    for block in form.get("blocks", []):
        controls = block.get("controls", [])
        select_controls = [
            control for control in controls if control.get("kind") == "select"
        ]
        select_values: List[str] = []
        for control in select_controls:
            value = _selection_value(selections, control["id"])
            if control.get("required") and not str(value or "").strip():
                errors.append(
                    f"Im Block {block.get('name')} muss eine Auswahl getroffen werden."
                )
            if value:
                if value in select_values:
                    errors.append(
                        f"Im Block {block.get('name')} müssen Alternativen gewählt werden."
                    )
                select_values.append(str(value))

        for group in block.get("groups", []):
            selected = [
                control
                for control in group.get("controls", [])
                if bool(_selection_value(selections, control["id"]))
            ]
            minimum = _as_int(group.get("min"), 0)
            maximum = _as_int(group.get("max"), 0)
            if len(selected) < minimum:
                errors.append(
                    f"In {group.get('name')} sind mindestens {minimum} Auswahl(en) nötig."
                )
            if maximum >= 0 and len(selected) > maximum:
                errors.append(
                    f"In {group.get('name')} sind höchstens {maximum} Auswahl(en) erlaubt."
                )

        grouped_ids = {
            control.get("id")
            for group in block.get("groups", [])
            for control in group.get("controls", [])
        }
        ungrouped = [
            control
            for control in controls
            if control.get("kind") == "checkbox"
            and control.get("id") not in grouped_ids
        ]
        if ungrouped:
            count = sum(
                bool(_selection_value(selections, control["id"]))
                for control in ungrouped
            )
            minimum = _as_int(block.get("min"), 0)
            maximum = _as_int(block.get("max"), 0)
            if count < minimum:
                errors.append(
                    f"Im Block {block.get('name')} sind mindestens {minimum} Auswahl(en) nötig."
                )
            if maximum >= 0 and count > maximum:
                errors.append(
                    f"Im Block {block.get('name')} sind höchstens {maximum} Auswahl(en) erlaubt."
                )

    excluded_by_block: Dict[str, set[str]] = {}
    for block in form.get("blocks", []):
        for control in block.get("controls", []):
            if control.get("kind") != "select":
                continue
            value = _selection_value(selections, control["id"])
            if value:
                selected_option = next(
                    (
                        option
                        for option in control.get("options", [])
                        if option.get("value") == str(value)
                    ),
                    {},
                )
                excluded_values = {
                    str(value),
                    str(selected_option.get("course") or ""),
                    *[str(item) for item in selected_option.get("excludes", [])],
                }
            else:
                excluded_values = set()
            for target in control.get("not_blocks", []):
                excluded_by_block.setdefault(target, set()).update(
                    item for item in excluded_values if item
                )

    for block in form.get("blocks", []):
        if block.get("name") not in excluded_by_block:
            continue
        for control in block.get("controls", []):
            if (
                control.get("kind") == "checkbox"
                and control.get("name") in excluded_by_block[block.get("name", "")]
            ):
                if bool(_selection_value(selections, control["id"])):
                    errors.append(
                        f"{control.get('name')} kann nicht gleichzeitig gewählt werden."
                    )

    return errors


def serialize_oberstufenwahl_submission(
    form: Dict[str, Any], submission: Dict[str, Any]
) -> List[Any]:
    """Build the exact ``kurse`` array emitted by ``abgabe.js``."""

    errors = validate_oberstufenwahl_submission(form, submission)
    if errors:
        raise ValueError(" ".join(errors))

    fields = submission.get("fields") or {}
    selections = submission.get("selections") or {}
    kurse: List[Any] = []
    personal = {
        "name": "Bemerkungen",
        "anmerk": [
            f"{field.get('label', '').strip()} # {str(fields.get(field.get('id'), '')).strip()}"
            for field in form.get("personal_fields", [])
        ],
    }
    kurse.append(personal)

    for block in form.get("blocks", []):
        selects = [
            control
            for control in block.get("controls", [])
            if control.get("kind") == "select"
        ]
        if selects:
            kurse.append(
                {
                    "name": block.get("name", ""),
                    "values": [
                        str(_selection_value(selections, control["id"]) or "")
                        for control in sorted(
                            selects,
                            key=lambda control: _as_int(control.get("order"), 0),
                        )
                    ],
                }
            )

        selected_names: set[str] = set()
        for control in block.get("controls", []):
            if control.get("kind") != "checkbox" or not bool(
                _selection_value(selections, control["id"])
            ):
                continue
            name = str(control.get("name") or "")
            if not name or name in selected_names:
                continue
            selected_names.add(name)
            value = _selection_value(selections, control["id"])
            if isinstance(value, dict) and value.get("teacher"):
                kurse.append(f"{name} # {value['teacher']}")
            else:
                kurse.append(name)

    return kurse


def wahlen_get_overview(self) -> Dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    try:
        response = self.session.get(f"{self.BASE_START_URL}/oberstufenwahl.php")
        response.raise_for_status()
        return parse_oberstufenwahl_overview(response.text, f"{self.BASE_START_URL}/")
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch elections: {exc}"}


def wahlen_get_form(self, election_id: str) -> Dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    election_id = str(election_id).strip()
    if not election_id:
        return {"success": False, "error": "Election ID is required"}
    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/oberstufenwahl.php",
            params={"a": "abgabe", "w": election_id},
        )
        response.raise_for_status()
        return parse_oberstufenwahl_form(
            response.text,
            election_id,
            response.url,
        )
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch election form: {exc}"}


def wahlen_submit(
    self,
    election_id: str,
    submission: Dict[str, Any],
    confirmed: bool = False,
) -> Dict[str, Any]:
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}
    if not confirmed:
        return {
            "success": False,
            "error": "Explicit submission confirmation is required",
        }
    form = wahlen_get_form(self, election_id)
    if not form.get("success"):
        return form
    validation_errors = validate_oberstufenwahl_submission(form, submission)
    if validation_errors:
        return {
            "success": False,
            "error": " ".join(validation_errors),
            "validation_errors": validation_errors,
        }
    kurse = serialize_oberstufenwahl_submission(form, submission)

    try:
        response = self.session.post(
            f"{self.BASE_START_URL}/oberstufenwahl.php",
            data={
                "a": "abgabe",
                "w": str(election_id).strip(),
                "kurse": json.dumps(kurse, ensure_ascii=False, separators=(",", ":")),
            },
        )
        response.raise_for_status()
        return {
            "success": True,
            "submitted": True,
            "status_code": response.status_code,
            "message": "Die Angaben wurden an das Schulportal übermittelt.",
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to submit election: {exc}"}
