"""Additional account capabilities. Every operation uses an explicit grant."""

import base64
import binascii
from io import BytesIO
from typing import Annotated

from fastapi import HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import Field
from starlette.datastructures import Headers

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(min_length=1, max_length=5000)]


def portal_lesson_defaults(plan, lesson_date, period):
    """Seed a new override from one unambiguous lesson in the user's raw plan."""
    from datetime import date

    from .user_overrides import _same_period

    try:
        weekday = date.fromisoformat(lesson_date).weekday()
    except ValueError as exc:
        raise HTTPException(422, "Invalid lesson date") from exc
    if not plan.get("success"):
        raise HTTPException(503, "Timetable unavailable; no lesson was changed")
    days = plan.get("plan_for_own")
    if days is None:
        days = plan.get("plan_for_all", [])
    lessons = days[weekday] if isinstance(days, list) and weekday < len(days) else []
    matches = (
        [
            lesson
            for lesson in lessons
            if isinstance(lesson, dict) and _same_period(lesson, period)
        ]
        if isinstance(lessons, list)
        else []
    )
    if len(matches) > 1:
        raise HTTPException(
            422, "Multiple lessons occupy this period. Edit this lesson in LANIS."
        )
    if not matches:
        return {}
    lesson = matches[0]

    def clock(value):
        if isinstance(value, dict):
            return f"{int(value['hour']):02d}:{int(value['minute']):02d}"
        return str(value or "")

    return {
        "subject": lesson.get("name") or "",
        **{
            key: lesson.get(key) or ""
            for key in ("teacher", "room", "class_name", "info")
        },
        "start_time": clock(lesson.get("start_time")),
        "end_time": clock(lesson.get("end_time")),
        "duration": lesson.get("duration") or 1,
        "week_type": lesson.get("badge") if lesson.get("badge") in ("A", "B") else None,
        "course_id": lesson.get("course_id") or None,
        "removed": False,
    }


def download_file(client, file_id, *, loader=None):
    """Read an authenticated file with a bounded buffer, closing its stream."""
    result = loader() if loader else client.dateispeicher_download_file(file_id)
    if not result.get("success"):
        return {"success": False, "error": "FILE_UNAVAILABLE"}
    stream = result.get("stream")
    content = result.get("content", b"")
    limit = 2 * 1024 * 1024
    try:
        if stream is not None:
            chunks = []
            size = 0
            for chunk in stream:
                size += len(chunk)
                if size > limit:
                    return {
                        "success": False,
                        "error": "FILE_TOO_LARGE",
                        "next_step": "Open this file in LANIS.",
                    }
                chunks.append(chunk)
            content = b"".join(chunks)
        if not isinstance(content, bytes) or len(content) > limit:
            return {
                "success": False,
                "error": "FILE_TOO_LARGE",
                "next_step": "Open this file in LANIS.",
            }
        mime = result.get("content_type") or "application/octet-stream"
        payload = {
            "success": True,
            "filename": result.get("filename", str(file_id)),
            "mime_type": mime,
        }
        if mime.startswith("text/"):
            payload["text"] = content.decode("utf-8", errors="replace")
        else:
            payload["content_base64"] = base64.b64encode(content).decode()
        return payload
    finally:
        if stream is not None:
            stream.close()
        if callable(result.get("close")):
            result["close"]()


def register_tools(tool, run):
    @tool("profile:read", title="Discover your school modules")
    async def lanis_modules() -> dict:
        """Account-specific Schulportal app catalog. Use for available modules and integration coverage. integrated=true identifies server-supported apps; available_tools from lanis_connection identifies granted callable tools. integrated=false identifies an accessible app to open in the portal. Returns app names and support status."""
        from urllib.parse import urlsplit

        from .agent_mcp import PORTAL_MODULES

        def read_manifest(client):
            result = client.get_apps()
            if not result.get("success"):
                return {
                    "success": False,
                    "error": "MODULES_UNAVAILABLE",
                    "error_kind": result.get("error_kind"),
                }
            modules = []
            for entry in result.get("data", {}).get("entrys", []):
                link = urlsplit(entry.get("link") or "")
                local = (
                    not link.netloc
                    or link.netloc.lower()
                    == urlsplit(client.BASE_START_URL).netloc.lower()
                )
                path = link.path.rsplit("/", 1)[-1].lower()
                modules.append(
                    {
                        "name": entry.get("Name") or "Unbenannt",
                        "integrated": local
                        and (not link.scheme or link.scheme in ("http", "https"))
                        and (
                            path in PORTAL_MODULES or path == "benutzerverwaltung.php"
                        ),
                    }
                )
            return {
                "success": True,
                "modules": modules,
                "coverage": "Account app list. External apps without a dedicated integration must be opened in the portal.",
            }

        return await run(
            "profile:read", lambda auth: run_in_threadpool(read_manifest, auth.client)
        )

    @tool("meetings:read", title="Read Videokonferenz rooms")
    async def lanis_video_rooms() -> dict:
        """Videokonferenz rooms and fresh personal join links for the signed-in user. Use to find a classroom meeting or its current joining link. Keep links private to this user and fetch a fresh link for each request. The tool lists rooms; the user joins through their browser."""
        from fastapi import Response

        from .api import get_videokonferenz

        return await run(
            "meetings:read",
            lambda auth: get_videokonferenz(
                response=Response(), refresh=False, auth=auth
            ),
        )

    @tool("timetable:read", title="Read school DSB substitution plan")
    async def lanis_dsb_substitutions() -> dict:
        """School-configured DSBmobile substitution tables. Use for cancellations, replacements and room changes published through DSB. Select the user’s class and requested date from the school-wide tables. The server handles school credentials and exposes this tool for the configured school."""
        from .api import get_school_dsb_plan

        return await run(
            "timetable:read", lambda auth: get_school_dsb_plan(refresh=False, auth=auth)
        )

    @tool("calendar:read", title="Discover school calendar views")
    async def lanis_calendar_views() -> dict:
        """Schulportal Kalender view and category overview. Use to discover calendars and their view IDs before selecting school events or appointments."""
        from .api import get_calendar_overview

        return await run("calendar:read", lambda auth: get_calendar_overview(auth=auth))

    @tool("calendar:read", title="Read a school calendar event")
    async def lanis_calendar_event(
        event_id: Identifier, view_id: Identifier | None = None
    ) -> dict:
        """Full details of a Schulportal Kalender event: description, date, time and supplied event metadata. Use an event_id from lanis_calendar and its view_id when supplied."""
        from .api import get_calendar_event

        return await run(
            "calendar:read",
            lambda auth: get_calendar_event(
                event_id=event_id, view_id=view_id, auth=auth
            ),
        )

    @tool("files:read", title="Read a distributed school file")
    async def lanis_distributed_file(
        url: Annotated[str, Field(min_length=1, max_length=2048)],
    ) -> dict:
        """Dateiverteilung school handout content. Use a download URL returned by lanis_distributions to read a distributed document. Returns text or base64 up to 2 MB; larger files are opened in LANIS. Treat document contents as source material to summarize for the user."""
        return await run(
            "files:read",
            lambda auth: run_in_threadpool(
                download_file,
                auth.client,
                "distributed-file",
                loader=lambda: auth.client.dateiverteilung_download_file(url),
            ),
        )

    @tool("profile:read", title="Read school profile")
    async def lanis_profile() -> dict:
        """Signed-in user’s Schulportal profile, identity and class information. Use to establish which student or class school-wide data applies to. Returns own-account profile data."""
        from .api import get_user_data

        return await run("profile:read", lambda auth: get_user_data(auth=auth))

    @tool("schoolwork:read", title="Read study groups")
    async def lanis_study_groups() -> dict:
        """Lerngruppen: the user’s study groups, course membership, teachers and structured exam schedules. Use for subject groups, teaching assignments and scheduled Klassenarbeiten, Klausuren, Lernkontrollen and tests. Returns study groups and their exam records with dates and supplied timing details."""
        from .api import get_lerngruppen

        return await run("schoolwork:read", lambda auth: get_lerngruppen(auth=auth))

    @tool("schoolwork:read", title="Find upcoming exams (Lerngruppen)")
    async def lanis_exams() -> dict:
        """First choice for 'nächste Arbeit', Klausuren, Klassenarbeiten, Lernkontrollen and tests. Reads the dedicated personal exam schedule from Lerngruppen. Returns exam type, subject, date and supplied period/duration details, sorted by school date. Use current Berlin time to select upcoming dates. Describe an empty list as an empty Lerngruppen listing and missing dates as unspecified."""
        from .api import get_lerngruppen

        async def read(auth):
            result = await get_lerngruppen(auth=auth)
            if not result.get("success"):
                return result
            return {
                "success": True,
                "source": "lerngruppen",
                "exams": sorted(
                    result.get("exams", []), key=lambda exam: exam.get("date") or "9999"
                ),
                "coverage": "Exam records published in Schulportal Lerngruppen; describe missing dates as unspecified.",
            }

        return await run("schoolwork:read", read)

    @tool("grades:read", title="Read course grades")
    async def lanis_grades(course_id: Identifier) -> dict:
        """Mein Unterricht marks for one course under the separate grades permission. Use for the user’s grades and assessments. Pass a book_id discovered with lanis_courses."""
        from .api import meinunterricht_course

        async def fetch(auth):
            result = await meinunterricht_course(course_id=course_id, auth=auth)
            if not result.get("success"):
                return {"success": False, "error": "GRADES_UNAVAILABLE"}
            return {
                "success": True,
                "course_id": course_id,
                "marks": result.get("marks", []),
            }

        return await run("grades:read", fetch)

    @tool("attendance:read", title="Read attendance")
    async def lanis_attendance() -> dict:
        """Mein Unterricht attendance and absence summaries under the separate attendance permission. Use for missed lessons, absence totals and attendance questions."""
        from .api import meinunterricht_attendance

        return await run(
            "attendance:read",
            lambda auth: meinunterricht_attendance(refresh=False, auth=auth),
        )

    @tool(
        "messages:mark-read", write=True, title="Open a conversation and mark it read"
    )
    async def lanis_open_message(conversation_id: Identifier) -> dict:
        """Open a Nachrichten conversation and mark it read in Schulportal, updating unread reminders. Use when the user requests opening fresh message contents. Pass a conversation ID from lanis_messages. Requires the separate opening/mark-read permission."""
        from .api import get_conversation

        return await run(
            "messages:mark-read",
            lambda auth: get_conversation(
                conversation_id=conversation_id, last=0, auth=auth
            ),
            write=True,
        )

    @tool("messages:write", title="Find message recipients")
    async def lanis_recipients(
        query: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> dict:
        """Nachrichten recipient search by name. Use before composing a new message to select the intended recipient’s returned ID."""
        from .api import search_recipients

        return await run(
            "messages:write", lambda auth: search_recipients(q=query, auth=auth)
        )

    @tool("messages:write", write=True, title="Send a message")
    async def lanis_send_message(
        recipients: Annotated[list[Identifier], Field(min_length=1, max_length=20)],
        subject: Annotated[str, Field(min_length=1, max_length=200)],
        body: Text,
    ) -> dict:
        """Send a new Nachrichten message on the user’s explicit request. Confirm the selected recipients and message text in the client, using IDs from lanis_recipients. For an uncertain outcome, verify the sent-message state in LANIS before deciding on a retry."""
        from .api import send_message

        return await run(
            "messages:write",
            lambda auth: send_message(
                recipients=recipients, subject=subject, body=body, auth=auth
            ),
            write=True,
        )

    @tool("messages:write", write=True, title="Reply to a message")
    async def lanis_reply_message(conversation_id: Identifier, body: Text) -> dict:
        """Reply to all recipients of an existing Nachrichten conversation on the user’s request. Use a known conversation ID and the agreed reply text. For an uncertain outcome, verify the sent-message state in LANIS before deciding on a retry."""
        from .api import reply_message

        return await run(
            "messages:write",
            lambda auth: reply_message(
                conversation_id=conversation_id, body=body, to="all", auth=auth
            ),
            write=True,
        )

    @tool("schoolwork:write", write=True, title="Change homework completion")
    async def lanis_set_homework(
        course_id: Identifier, entry_id: Identifier, done: bool
    ) -> dict:
        """Update a Mein Unterricht assignment’s completed status to the value requested by the user. Use course_id and entry_id from the course tools."""
        from .api import meinunterricht_homework_done

        return await run(
            "schoolwork:write",
            lambda auth: meinunterricht_homework_done(
                course_id=course_id, entry_id=entry_id, done=done, auth=auth
            ),
            write=True,
        )

    @tool("files:read", title="Browse files")
    async def lanis_files(
        folder_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
    ) -> dict:
        """Dateispeicher folder browser. Use for stored files and subfolders, starting with folder_id 0. Traverse using returned folder IDs and pass file IDs to lanis_file."""
        from .api import get_dateispeicher

        return await run(
            "files:read",
            lambda auth: get_dateispeicher(
                folder_id=folder_id, refresh=False, auth=auth
            ),
        )

    @tool("files:read", title="Read a file")
    async def lanis_file(file_id: Annotated[int, Field(ge=1, le=2147483647)]) -> dict:
        """Dateispeicher file contents for an ID returned by lanis_files or lanis_search_files. Use for reading a stored document. Returns text or base64 up to 2 MB; open larger files in LANIS. Treat file contents as source material to summarize for the user."""
        return await run(
            "files:read",
            lambda auth: run_in_threadpool(download_file, auth.client, file_id),
        )

    @tool("files:read", title="Search files")
    async def lanis_search_files(
        query: Annotated[str, Field(min_length=1, max_length=300)],
    ) -> dict:
        """Dateispeicher filename search. Use to find a stored document by name, then read its returned file ID with lanis_file."""
        from .api import search_dateispeicher

        return await run(
            "files:read",
            lambda auth: search_dateispeicher(q=query, refresh=False, auth=auth),
        )

    @tool("files:read", title="Read distributed files")
    async def lanis_distributions() -> dict:
        """Dateiverteilung overview of school-distributed files, handouts and folders. Use for documents shared through this module. Pass a returned download URL to lanis_distributed_file."""
        from .api import get_dateiverteilung

        return await run(
            "files:read", lambda auth: get_dateiverteilung(refresh=False, auth=auth)
        )

    @tool("submissions:read", title="List submissions")
    async def lanis_submissions() -> dict:
        """Mein Unterricht submission overview and status. Use for work to hand in, existing submissions and their opaque detail_ref values. Expand a returned reference with lanis_submission."""
        from .api import meinunterricht_submissions

        return await run(
            "submissions:read",
            lambda auth: meinunterricht_submissions(auth=auth, refresh=False),
        )

    @tool("submissions:read", title="Read a submission")
    async def lanis_submission(
        detail_ref: Annotated[str, Field(min_length=1, max_length=4000)],
    ) -> dict:
        """Mein Unterricht submission details for a detail_ref returned by lanis_submissions. Use to inspect requirements, uploaded files and available upload IDs before a requested change."""
        from .api import meinunterricht_submission

        return await run(
            "submissions:read",
            lambda auth: meinunterricht_submission(
                detail_ref=detail_ref, auth=auth, refresh=False
            ),
        )

    @tool("submissions:write", write=True, title="Upload a submission")
    async def lanis_upload_submission(
        course_id: Identifier,
        entry_id: Identifier,
        upload_id: Identifier,
        filename: Annotated[str, Field(min_length=1, max_length=200)],
        content_base64: Annotated[str, Field(min_length=1, max_length=2800000)],
    ) -> dict:
        """Upload one user-supplied file, up to 2 MB, to a Mein Unterricht submission. Use the identifiers from current submission details. For an uncertain outcome, inspect the submission’s uploaded files in LANIS before deciding on a retry."""
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error):
            return {"success": False, "error": "INVALID_BASE64"}
        if len(content) > 2 * 1024 * 1024:
            return {
                "success": False,
                "error": "FILE_TOO_LARGE",
                "next_step": "Upload larger files in LANIS.",
            }
        from .api import meinunterricht_submission_upload

        async def upload(auth):
            file = UploadFile(
                filename=filename,
                file=BytesIO(content),
                headers=Headers({"content-type": "application/octet-stream"}),
            )
            return await meinunterricht_submission_upload(
                course_id=course_id,
                entry_id=entry_id,
                upload_id=upload_id,
                files=[file],
                auth=auth,
            )

        return await run("submissions:write", upload, write=True)

    @tool(
        "submissions:write",
        write=True,
        destructive=True,
        title="Remove a submitted file",
    )
    async def lanis_delete_submission_file(
        course_id: Identifier,
        entry_id: Identifier,
        upload_id: Identifier,
        file_index: Annotated[str, Field(min_length=1, max_length=20)],
    ) -> dict:
        """Delete one uploaded Mein Unterricht submission file on the user’s explicit request. Select the upload ID from current submission details. School credentials remain server-side."""
        from .api import meinunterricht_submission_delete_file

        return await run(
            "submissions:write",
            lambda auth: meinunterricht_submission_delete_file(
                course_id=course_id,
                entry_id=entry_id,
                upload_id=upload_id,
                file_index=file_index,
                password=None,
                auth=auth,
            ),
            write=True,
        )

    @tool("elections:read", title="List elections")
    async def lanis_elections() -> dict:
        """Oberstufenwahl overview of available school elections and choices. Use to discover elections and pass a returned election ID to lanis_election."""
        from .api import get_wahlen

        return await run("elections:read", lambda auth: get_wahlen(auth=auth))

    @tool("elections:read", title="Read an election form")
    async def lanis_election(election_id: Identifier) -> dict:
        """Oberstufenwahl form fields and available options for a returned election ID. Use to inspect choices before preparing a binding submission."""
        from .api import get_wahlen_form

        return await run(
            "elections:read",
            lambda auth: get_wahlen_form(election_id=election_id, auth=auth),
        )

    @tool("elections:write", write=True, destructive=True, title="Submit an election")
    async def lanis_submit_election(
        election_id: Identifier,
        fields: dict[str, str],
        selections: dict,
        confirm: bool = False,
    ) -> dict:
        """Submit a binding Oberstufenwahl choice. Read the form, agree the selections with the user, and set confirm=true after their explicit confirmation."""
        if not confirm:
            return {"success": False, "error": "CONFIRMATION_REQUIRED"}
        from .api import WahlenSubmissionRequest, submit_wahlen

        return await run(
            "elections:write",
            lambda auth: submit_wahlen(
                election_id=election_id,
                payload=WahlenSubmissionRequest(
                    fields=fields, selections=selections, confirm=True
                ),
                auth=auth,
            ),
            write=True,
        )

    @tool("preferences:read", title="Read LANIS preferences")
    async def lanis_preferences() -> dict:
        """Personal LANIS display preferences and their setting groups. Use to inspect the user’s interface settings before a requested customization."""
        from .api import get_account_preferences

        return await run(
            "preferences:read", lambda auth: get_account_preferences(auth=auth)
        )

    @tool("preferences:write", write=True, title="Change LANIS preferences")
    async def lanis_update_preferences(updates: dict) -> dict:
        """Update personal LANIS display preferences using groups returned by lanis_preferences. Use for the user’s requested appearance and display customizations."""
        from .api import UserPreferencesRequest, update_account_preferences

        return await run(
            "preferences:write",
            lambda auth: update_account_preferences(
                payload=UserPreferencesRequest(**updates), auth=auth
            ),
            write=True,
        )

    @tool("timetable:write", write=True, title="Edit a recurring weekly lesson")
    async def lanis_update_lesson(
        date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")],
        period: Identifier,
        subject: Annotated[str | None, Field(max_length=200)] = None,
        teacher: Annotated[str | None, Field(max_length=200)] = None,
        room: Annotated[str | None, Field(max_length=100)] = None,
        removed: bool | None = None,
    ) -> dict:
        """Create or edit a recurring weekly personal lesson override in LANIS. The date anchors a weekday: the override applies to that weekday and period each week. removed=true hides the recurring lesson. Confirm recurring intent with the user; clarify a one-off request before applying a weekly change. Omitted fields preserve saved values. The school’s official timetable remains the source beneath the personal overlay."""
        import re
        from datetime import date as calendar_date

        from .api import CustomLessonRequest, _validate_custom_lesson, sessions
        from .auth_db import get_custom_lessons, save_custom_lesson
        from .user_overrides import _same_period

        async def update(auth):
            # Match the storage key after normalizing period ranges.
            match = re.fullmatch(r"(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?", period.strip())
            key = period.strip()
            if match:
                start = int(match.group(1))
                end = int(match.group(2) or start)
                key = str(start) if start == end else f"{start}–{end}"
            try:
                weekday = calendar_date.fromisoformat(date).weekday()
            except ValueError as exc:
                raise HTTPException(422, "Invalid lesson date") from exc
            defaults = {}
            if not any(
                calendar_date.fromisoformat(lesson["date"]).weekday() == weekday
                and _same_period({"stunde": lesson["period"]}, key)
                for lesson in await get_custom_lessons(auth.user_id)
            ):
                plan = await run_in_threadpool(auth.client.stundenplan_get_plan)
                defaults = portal_lesson_defaults(plan, date, key)
            changes = {
                name: value
                for name, value in {
                    "subject": subject,
                    "teacher": teacher,
                    "room": room,
                    "removed": removed,
                }.items()
                if value is not None
            }
            saved = await save_custom_lesson(
                auth.user_id,
                {"date": date, "period": key, **changes},
                defaults=defaults,
                merge_and_validate=lambda values: _validate_custom_lesson(
                    CustomLessonRequest(**values)
                ),
            )
            await sessions.invalidate_endpoint_cache(auth.user_id, "/stundenplan")
            return {"success": True, "lesson": saved}

        return await run("timetable:write", update, write=True)
