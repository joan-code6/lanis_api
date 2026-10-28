"""Scoped agent access to the public fixtures, with no real tool handlers."""

import json
from urllib.parse import quote, urlencode

from starlette.requests import Request

from . import ACCESS_TOKEN, SCHOOL_ID, USERNAME, dispatch

DEMO_USER_ID = f"{SCHOOL_ID}:{USERNAME.casefold()}"


def is_demo_user(user_id):
    """Recognize only the reserved, canonical reviewer identity."""
    return user_id == DEMO_USER_ID


async def sample_request(path, *, params=None, method="GET", body=None):
    """Reuse the isolated REST dispatcher without HTTP, logs, or persistence."""
    payload = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": urlencode(params or {}).encode(),
            "headers": [
                (b"x-session-token", ACCESS_TOKEN.encode()),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
    )
    response = await dispatch(request)
    if response is None:
        return {"success": False, "error": "SAMPLE_NOT_AVAILABLE"}
    result = json.loads(response.body)
    if response.status_code >= 400:
        return {"success": False, "error": "SAMPLE_NOT_FOUND", "details": result}
    return result


async def call_tool(name, args):
    """Map validated MCP arguments to fictional reads and simulated writes."""
    reads = {
        "lanis_profile": "/benutzer",
        "lanis_modules": "/modules",
        "lanis_courses": "/meinunterricht",
        "lanis_timetable": "/stundenplan",
        "lanis_substitutions": "/vertretungsplan",
        "lanis_dsb_substitutions": "/dsb/school-plan",
        "lanis_calendar_views": "/kalender",
        "lanis_calendar": "/kalender/events",
        "lanis_messages": "/nachrichten/headers",
        "lanis_recipients": "/nachrichten/search",
        "lanis_attendance": "/meinunterricht/attendance",
        "lanis_study_groups": "/lerngruppen",
        "lanis_exams": "/lerngruppen",
        "lanis_video_rooms": "/videokonferenz",
        "lanis_files": "/dateispeicher",
        "lanis_search_files": "/dateispeicher/search",
        "lanis_distributions": "/dateiverteilung",
        "lanis_submissions": "/meinunterricht/submissions",
        "lanis_elections": "/wahlen",
        "lanis_preferences": "/settings/preferences",
    }
    if name in reads:
        params = dict(args)
        if "query" in params:
            params["q"] = params.pop("query")
        result = await sample_request(reads[name], params=params)
        if name == "lanis_submissions":
            for submission in result.get("submissions", []):
                submission["detail_ref"] = submission["id"]
        if name == "lanis_exams":
            return {
                "success": True,
                "exams": result.get("exams", []),
                "exam_count": result.get("exam_count", 0),
            }
        if name == "lanis_modules":
            from ..agent_mcp import PORTAL_MODULES

            return {
                "success": True,
                "modules": [
                    {
                        "name": m["name"],
                        "integrated": m["url"].rsplit("/", 1)[-1] in PORTAL_MODULES,
                        "url": m["url"],
                    }
                    for m in result["modules"]
                ],
            }
        if name == "lanis_messages":
            result["conversations"] = result["conversations"][args.get("offset", 0) :]
        if name == "lanis_calendar":
            result["events"] = [
                event
                for event in result["events"]
                if event["start"].startswith(str(args["year"]) + "-")
            ]
            result["count"] = len(result["events"])
        if name == "lanis_timetable":
            result.pop("exams", None)
            # Schoolwork content has its own permission; never leak it here.
            for day in result.get("days", []):
                for lesson in day.get("lessons", []):
                    lesson.pop("homework", None)
        return result
    if name in {"lanis_course", "lanis_grades"}:
        result = await sample_request(
            "/meinunterricht/course/" + quote(args["course_id"], safe="")
        )
        if name == "lanis_grades":
            return {
                "success": result.get("success", False),
                "course_id": args["course_id"],
                "marks": result.get("marks", []),
            }
        from ..agent_mcp import course_payload

        return course_payload(result, args.get("offset", 0), args.get("limit", 20))
    details = {
        "lanis_message": ("/nachrichten/", "conversation_id"),
        "lanis_open_message": ("/nachrichten/", "conversation_id"),
        "lanis_calendar_event": ("/kalender/event/", "event_id"),
        "lanis_submission": ("/meinunterricht/submissions/", "detail_ref"),
        "lanis_election": ("/wahlen/", "election_id"),
    }
    if name in details:
        prefix, key = details[name]
        result = await sample_request(prefix + quote(args[key], safe=""))
        if name == "lanis_submission" and result.get("success"):
            result["submission"]["detail_ref"] = args[key]
        return result
    if name in {"lanis_file", "lanis_distributed_file"}:
        return {
            "success": True,
            "filename": "beispiel.txt",
            "mime_type": "text/plain",
            "text": "Lanis Test Schule – Beispieldatei",
        }
    writes = {
        "lanis_send_message": ("/nachrichten/send", "POST"),
        "lanis_reply_message": ("/nachrichten/reply", "POST"),
        "lanis_set_homework": ("/meinunterricht/homework-done", "POST"),
        "lanis_upload_submission": ("/meinunterricht/submissions/upload", "POST"),
        "lanis_delete_submission_file": ("/meinunterricht/submissions/file", "DELETE"),
        "lanis_update_preferences": ("/settings/preferences", "PATCH"),
        "lanis_update_lesson": ("/settings/timetable/lessons", "PUT"),
        "lanis_submit_election": (
            "/wahlen/" + quote(args.get("election_id", ""), safe="") + "/submit",
            "POST",
        ),
    }
    if name in writes:
        if name == "lanis_submit_election" and not args.get("confirm"):
            return {"success": False, "error": "CONFIRMATION_REQUIRED"}
        path, method = writes[name]
        result = await sample_request(
            path, method=method, body=args.get("updates", args)
        )
        result["simulated"] = True
        return result
    return {"success": False, "error": "SAMPLE_NOT_AVAILABLE"}
