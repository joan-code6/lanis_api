"""Public, stateless reviewer sandbox. Never delegates to real account handlers."""

import copy
import json
import re
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from starlette.responses import HTMLResponse, JSONResponse, Response

SCHOOL_ID = "9999"
USERNAME = "Vorname.Nachname"
# Public markers, deliberately not JWTs and not valid outside this dispatcher.
ACCESS_TOKEN = "lanis-public-reviewer-demo-access"
REFRESH_TOKEN = "lanis-public-reviewer-demo-refresh"
SCHOOL = {"id": SCHOOL_ID, "name": "Lanis Test Schule", "location": "Demo"}
DISTRICT = {"id": "lanis-demo", "name": "Lanis Test Schule", "schools": [SCHOOL]}
_FIXTURES = json.loads(Path(__file__).with_name("responses.json").read_text())


def school_directory(data):
    result = copy.deepcopy(data)
    districts = result.setdefault("districts", [])
    # Never expose an upstream institution under the reserved sandbox ID.
    for district in districts:
        district["schools"] = [
            s for s in district.get("schools", []) if str(s.get("id")) != SCHOOL_ID
        ]
    districts[:] = [d for d in districts if d.get("id") != DISTRICT["id"]]
    districts.append(copy.deepcopy(DISTRICT))
    result.setdefault("success", True)
    return result


def sample_data():
    """Move fictional dates by whole weeks so the timetable keeps its weekdays."""
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    generated = date.fromisoformat(_FIXTURES["generated_on"])
    current_monday = today - timedelta(days=today.weekday())
    generated_monday = generated - timedelta(days=generated.weekday())
    shift = current_monday - generated_monday

    def transform(value):
        if isinstance(value, dict):
            return {k: transform(v) for k, v in value.items()}
        if isinstance(value, list):
            return [transform(v) for v in value]
        if not isinstance(value, str):
            return value
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:T|$)", value):
            try:
                value = (date.fromisoformat(value[:10]) + shift).isoformat() + value[
                    10:
                ]
            except ValueError:
                pass
        value = value.replace("mia.keller", USERNAME).replace(
            "Mia Keller", "Vorname Nachname"
        )
        value = value.replace("Elisabeth-Selbert-Schule", SCHOOL["name"])
        # Every downloadable fixture stays inside this sandbox, even with arbitrary URL params.
        if value.startswith("/files/"):
            return "/meinunterricht/file/demo"
        if value.startswith("https://"):
            if "download" in value:
                return "/dateispeicher/file/demo"
            return urlsplit(value).path
        return value

    data = transform(_FIXTURES["responses"])
    data["/benutzer"]["data"].update(
        school_id=SCHOOL_ID,
        username=USERNAME,
        vorname="Vorname",
        firstname="Vorname",
        nachname="Nachname",
        lastname="Nachname",
        email="reviewer@example.invalid",
    )
    supported = {
        "Mein Unterricht",
        "Nachrichten",
        "Kalender",
        "Dateispeicher",
        "Vertretungsplan",
        "DSBmobile",
        "Stundenplan",
        "Lerngruppen",
    }
    for module in data["/modules"]["modules"]:
        module["proxy_app"] = module["name"] not in supported
    data["/apps"] = {
        "success": True,
        "data": {
            "error": "",
            "folders": [],
            "till": 3600,
            "entrys": [
                {
                    "Name": m["name"],
                    "Farbe": m["color"],
                    "Logo": m["logo"],
                    "Ordner": m["folders"],
                    "link": m["url"],
                    "target": "_self",
                }
                for m in data["/modules"]["modules"]
            ],
        },
    }
    data["/videokonferenz"] = {
        "success": True,
        "available": True,
        "count": 1,
        "open_count": 1,
        "rooms": [
            {
                "id": "demo-room",
                "name": "Mathematik 9c",
                "teachers": ["MV"],
                "status": "open",
                "status_label": "Raum betreten",
                "can_join": True,
                "join_url": "/app/Videokonferenz",
                "links": [],
            }
        ],
    }
    data["/dateiverteilung"] = {
        "success": True,
        "available": True,
        "distribution_count": 1,
        "file_count": 1,
        "unread_count": 1,
        "distributions": [
            {
                "id": "demo-letter",
                "title": "Elternbrief zur Projektwoche",
                "description": "Informationen zur Projektwoche",
                "source": "Schulleitung",
                "created_at": today.isoformat(),
                "unread": True,
                "files": [
                    {
                        "id": "demo-file",
                        "name": "Elternbrief.txt",
                        "size": "1 KB",
                        "download_url": "/dateiverteilung/file",
                    }
                ],
                "links": [],
            }
        ],
    }
    data["/wahlen"] = {
        "success": True,
        "elections": [
            {
                "id": "demo-election",
                "title": "Projektwoche",
                "participation_text": "Wähle dein Wunschprojekt",
            }
        ],
        "election_count": 1,
    }
    data["/wahlen/demo-election"] = {
        "success": True,
        "election": data["/wahlen"]["elections"][0],
        "personal_fields": [],
        "blocks": [
            {
                "name": "Projekt",
                "min": 1,
                "max": 1,
                "controls": [
                    {
                        "id": "project",
                        "kind": "select",
                        "label": "Wunschprojekt",
                        "block": "Projekt",
                        "options": [
                            {"value": "science", "label": "Naturwissenschaften"},
                            {"value": "art", "label": "Kunst"},
                        ],
                    }
                ],
                "groups": [],
            }
        ],
    }
    data["/cache/status"] = {"success": True, "sync": None}
    return data


def timetable_view(data, params):
    """Adapt the frontend lesson fixture to the API's pure timetable resolver."""
    from ..timetable_substitutions import (
        dsb_substitutions,
        native_substitutions,
        periods,
        resolve_timetable,
    )

    template = data["/stundenplan"]
    plan = []
    for day in template["days"]:
        lessons = []
        for lesson in day["lessons"]:
            lesson_periods = periods(lesson.get("period"))
            lessons.append(
                {
                    **copy.deepcopy(lesson),
                    "name": lesson["subject"],
                    "stunde": lesson_periods[0]
                    if lesson_periods
                    else lesson.get("period"),
                    "duration": len(lesson_periods) or 1,
                    "badge": lesson.get("week_type"),
                }
            )
        plan.append(lessons)
    raw = {
        **template,
        "days": [day["name"] for day in template["days"]],
        "plan_for_own": plan,
        "plan_for_all": copy.deepcopy(plan),
        "week_badge": template.get("active_week"),
    }
    show_substitutions = params.get("show_substitutions", "true").lower() not in {
        "false",
        "0",
    }
    changes = (
        (
            native_substitutions(data["/vertretungsplan"])
            + dsb_substitutions(data["/dsb/school-plan"]["tables"])
        )
        if show_substitutions
        else []
    )
    result = resolve_timetable(
        raw,
        changes,
        "9C",
        datetime.now(ZoneInfo("Europe/Berlin")).date(),
        params.get("view_mode", "rolling"),
        params.get("plan_mode", "personal"),
        params.get("week_type"),
    )
    result["substitution_sources"] = (
        [{"name": "Schulportal", "error": False}, {"name": "DSB", "error": False}]
        if show_substitutions
        else []
    )
    return result


def tokens():
    return {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "school_id": SCHOOL_ID,
        "username": USERNAME,
        "encryption_ready": True,
        "is_first_school_user": False,
        "expires_in": 3600,
    }


async def dispatch(request):
    """Return None only for non-demo requests; a demo token can never fall through."""
    path, method = request.url.path, request.method
    token = request.headers.get("X-Session-Token") or request.query_params.get("token")
    bearer = request.headers.get("Authorization", "")
    is_demo = (
        token in {ACCESS_TOKEN, REFRESH_TOKEN} or bearer == f"Bearer {ACCESS_TOKEN}"
    )
    if path in {"/login", "/auth/refresh"} and method == "POST":
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        if path == "/login" and str(body.get("school_id", "")).strip() == SCHOOL_ID:
            if str(body.get("username", "")).strip().casefold() == USERNAME.casefold():
                return JSONResponse(tokens(), headers={"Cache-Control": "no-store"})
            return JSONResponse(
                {"detail": "Use Vorname.Nachname for Lanis Test Schule"},
                status_code=401,
            )
        if path == "/auth/refresh" and body.get("refresh_token") == REFRESH_TOKEN:
            return JSONResponse({"access_token": ACCESS_TOKEN, "expires_in": 3600})
    if not is_demo:
        return None
    # Browser management uses normal OAuth consent and grant ownership checks.
    # The public browser marker is never accepted as an MCP bearer token.
    if token == ACCESS_TOKEN and (
        path == "/agents" or re.fullmatch(r"/agents/(?:authorization/)?[^/]+", path)
    ):
        return None
    headers = {"Cache-Control": "no-store"}
    if path.startswith(("/admin", "/mcp", "/oauth", "/agents")):
        return JSONResponse(
            {"detail": "Unavailable in the reviewer sandbox"},
            status_code=403,
            headers=headers,
        )
    data = sample_data()
    if method == "GET":
        if path.startswith("/app/"):
            return HTMLResponse(
                '<!doctype html><html lang="de"><title>Lanis Test Schule</title>'
                "<h1>Lanis Test Schule</h1><p>Diese Beispiel-App ist im Testkonto verfügbar. "
                "Aktionen werden nur simuliert.</p></html>",
                headers=headers,
            )
        if (
            path.startswith(
                (
                    "/dateispeicher/file/",
                    "/meinunterricht/file/",
                    "/meinunterricht/submissions/file/",
                )
            )
            or path == "/dateiverteilung/file"
        ):
            return Response(
                "Lanis Test Schule – Beispieldatei\n",
                media_type="text/plain",
                headers={
                    **headers,
                    "Content-Disposition": 'attachment; filename="beispiel.txt"',
                },
            )
        if path == "/stundenplan/view":
            for key, allowed in {
                "view_mode": {"rolling", "week"},
                "plan_mode": {"personal", "all"},
                "week_type": {"A", "B"},
            }.items():
                if (
                    key in request.query_params
                    and request.query_params[key] not in allowed
                ):
                    return JSONResponse(
                        {"detail": "Invalid " + key}, status_code=422, headers=headers
                    )
            result = timetable_view(data, request.query_params)
        elif path == "/settings/preferences":
            from ..auth_db import DEFAULT_USER_PREFERENCES

            result = {
                "success": True,
                "stored": False,
                "preferences": copy.deepcopy(DEFAULT_USER_PREFERENCES),
            }
        elif path == "/dateispeicher":
            result = data.get(
                path + "?folder_id=" + request.query_params.get("folder_id", "0"),
                data[path],
            )
        elif path == "/dateispeicher/search":
            query = request.query_params.get("q", "").casefold()
            nodes = [v for k, v in data.items() if k.startswith("/dateispeicher?")]
            result = {
                "success": True,
                "query": query,
                "results": {
                    kind: [
                        f
                        for n in nodes
                        for f in n[kind]
                        if query in f["name"].casefold()
                    ]
                    for kind in ("files", "folders")
                },
            }
        elif path == "/nachrichten/search":
            query = request.query_params.get("q", "").casefold()
            recipients = [
                {
                    "id": teacher["recipient_id"],
                    "name": teacher["first_name"] + " " + teacher["last_name"],
                    "username": teacher["krz"],
                    "type": "Lehrkraft",
                }
                for group in data["/lerngruppen"]["groups"]
                for teacher in group["teachers"]
                if teacher.get("recipient_id")
            ]
            result = {
                "success": True,
                "results": [r for r in recipients if query in r["name"].casefold()],
            }
        elif path == "/meinunterricht/entry":
            url = request.query_params.get("url", "")
            identifiers = set(re.split(r"[^A-Za-z0-9_-]+", unquote(url)))
            entry = next(
                (
                    entry
                    for key, course in data.items()
                    if key.startswith("/meinunterricht/course/")
                    for entry in course["entries"]
                    if entry["entry_id"] in identifiers
                ),
                None,
            )
            if not url:
                result = data[path]
            elif entry is None:
                return JSONResponse(
                    {"detail": "Sample entry not found"},
                    status_code=404,
                    headers=headers,
                )
            else:
                result = data.get(
                    path + "?url=" + entry["entry_id"],
                    {
                        "success": True,
                        "entry": {
                            "id": entry["entry_id"],
                            "title": entry["thema"],
                            "content": "<p>"
                            + escape(entry.get("content") or entry["thema"])
                            + "</p>",
                            "date": entry["date"],
                            "attachments": copy.deepcopy(entry.get("files", [])),
                        },
                    },
                )
        elif path.startswith("/meinunterricht/submissions/"):
            submission_id = path.rsplit("/", 1)[-1]
            submission = next(
                (
                    item
                    for item in data["/meinunterricht/submissions"]["submissions"]
                    if item["id"] == submission_id
                ),
                None,
            )
            if submission is None:
                return JSONResponse(
                    {"detail": "Sample submission not found"},
                    status_code=404,
                    headers=headers,
                )
            result = {
                "success": True,
                "submission": copy.deepcopy(submission),
                "id": submission_id,
                "files": [],
            }
        elif path == "/account/export":
            result = {
                "schema_version": 1,
                "account": {
                    "school_id": SCHOOL_ID,
                    "username": USERNAME,
                    "credential_stored": False,
                },
                "sample_data": data,
            }
        elif path == "/search/semantic":
            query = request.query_params.get("q", "").casefold()
            matches = [
                {
                    "id": c["book_id"],
                    "category": "courses",
                    "title": c["name"],
                    "subtitle": c["thema"],
                    "href": "/courses/" + c["book_id"],
                    "icon": "AcademicCapIcon",
                    "score": 1.0,
                }
                for c in data["/meinunterricht"]["entries"]
                if query in (c["name"] + " " + c["thema"]).casefold()
            ]
            result = {
                "success": True,
                "query": query,
                "results": matches,
                "count": len(matches),
            }
        elif path == "/analytics/identity":
            result = {"account_ref": "public-reviewer-demo"}
        else:
            result = data.get(path)
        if result is not None:
            return JSONResponse(result, headers=headers)
    # Restrict simulated actions to the consumer API surface. No external calls,
    # persistence, notifications, telemetry, or shared mutable demo state.
    if method in {"POST", "PUT", "PATCH", "DELETE"} and (
        path.startswith(
            (
                "/nachrichten/",
                "/meinunterricht/",
                "/settings/",
                "/notifications/",
                "/dashboard/notifications/",
                "/wahlen/",
                "/whatsapp/",
                "/analytics/",
            )
        )
        or path
        in {
            "/logout",
            "/account",
            "/feedback",
            "/dsb/login",
            "/dsb/plan",
            "/dsb/plan-urls",
        }
    ):
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        result = data.get(path, {"success": True})
        result = {
            **result,
            "success": True,
            "submitted": True,
            "message_id": "demo-message",
            "sent_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
            "details": {"back": True, "id": "demo-reply"},
        }
        if path == "/logout":
            result = {"status": "logged_out"}
        if path == "/settings/preferences":
            from ..auth_db import DEFAULT_USER_PREFERENCES, _merge_nested_preferences

            result["preferences"] = _merge_nested_preferences(
                DEFAULT_USER_PREFERENCES, body
            )
            result["stored"] = True
        elif path == "/notifications/preferences":
            result["preferences"] = {**data[path]["preferences"], **body}
        elif path == "/settings/timetable/lessons" and method == "PUT":
            result["lesson"] = {**body, "is_custom": True}
        elif path == "/settings/class-links" and method == "PUT":
            result["link"] = {**body, "overridden": True}
        elif path == "/feedback":
            result["id"] = "demo-feedback"
        elif path == "/account":
            result.update(deleted=True, upstream_sph_data_deleted=False)
        return JSONResponse(result, headers=headers)
    return JSONResponse(
        {"detail": "No sample data for this endpoint"}, status_code=404, headers=headers
    )
