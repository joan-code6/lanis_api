"""Public, stateless reviewer sandbox. Never delegates to real account handlers."""

import copy
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from starlette.responses import HTMLResponse, JSONResponse, Response

SCHOOL_ID = "9999"
USERNAME = "Vornahme.Nachname"
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
    result["success"] = True
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
            "Mia Keller", "Vornahme Nachname"
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
        vorname="Vornahme",
        firstname="Vornahme",
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
                {"detail": "Use Vornahme.Nachname for Lanis Test Schule"},
                status_code=401,
            )
        if path == "/auth/refresh" and body.get("refresh_token") == REFRESH_TOKEN:
            return JSONResponse({"access_token": ACCESS_TOKEN, "expires_in": 3600})
    if not is_demo:
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
        if path == "/settings/preferences":
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
        elif path == "/meinunterricht/entry":
            url = request.query_params.get("url", "")
            result = next(
                (
                    v
                    for k, v in data.items()
                    if k.startswith(path + "?url=") and k.split("=")[-1] in url
                ),
                data[path],
            )
        elif path.startswith("/meinunterricht/submissions/"):
            submission_id = path.rsplit("/", 1)[-1]
            result = {
                "success": True,
                "submission": next(
                    iter(data["/meinunterricht/submissions"]["submissions"]), {}
                ),
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
