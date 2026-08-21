"""MCP transport for the complete user-facing Schulportal Hessen REST API."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote
from weakref import WeakKeyDictionary

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import LanisAPIError, LanisClient

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=False,
    destructiveHint=False,
)
MUTATING = ToolAnnotations(
    readOnlyHint=False,
    openWorldHint=False,
    destructiveHint=False,
)
PRIVATE_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    openWorldHint=False,
    destructiveHint=True,
)
EXTERNAL_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    openWorldHint=True,
    destructiveHint=True,
)


@dataclass(frozen=True)
class ApiRoute:
    """A REST operation represented by one MCP tool."""

    method: str
    path: str
    authenticated: bool = True


# Source of truth for MCP-to-REST parity. Documentation pages and the nested app
# browser catch-all are transport surfaces rather than standalone school actions.
API_ROUTES: dict[str, ApiRoute] = {
    "lanis_health": ApiRoute("GET", "/health", False),
    "lanis_metrics_stats": ApiRoute("GET", "/metrics/stats", False),
    "lanis_login": ApiRoute("POST", "/login", False),
    "lanis_refresh_access_token": ApiRoute("POST", "/auth/refresh", False),
    "lanis_logout": ApiRoute("POST", "/logout"),
    "lanis_list_schools": ApiRoute("GET", "/school-list", False),
    "lanis_get_district_schools": ApiRoute(
        "GET", "/school-list/district/{district_id}", False
    ),
    "lanis_search_schools": ApiRoute("GET", "/school-list/search", False),
    "lanis_get_apps": ApiRoute("GET", "/apps"),
    "lanis_get_modules": ApiRoute("GET", "/modules"),
    "lanis_get_user": ApiRoute("GET", "/benutzer"),
    "lanis_get_calendar": ApiRoute("GET", "/kalender"),
    "lanis_get_calendar_events": ApiRoute("GET", "/kalender/events"),
    "lanis_get_calendar_event": ApiRoute("GET", "/kalender/event/{event_id}"),
    "lanis_get_substitution_plan": ApiRoute("GET", "/vertretungsplan"),
    "lanis_get_timetable": ApiRoute("GET", "/stundenplan"),
    "lanis_get_file_storage": ApiRoute("GET", "/dateispeicher"),
    "lanis_search_file_storage": ApiRoute("GET", "/dateispeicher/search"),
    "lanis_get_study_groups": ApiRoute("GET", "/lerngruppen"),
    "lanis_get_message_headers": ApiRoute("GET", "/nachrichten/headers"),
    "lanis_get_conversation": ApiRoute("GET", "/nachrichten/{conversation_id}"),
    "lanis_search_message_recipients": ApiRoute("GET", "/nachrichten/search"),
    "lanis_send_message": ApiRoute("POST", "/nachrichten/send"),
    "lanis_reply_to_message": ApiRoute("POST", "/nachrichten/reply"),
    "lanis_mark_message_read": ApiRoute("POST", "/nachrichten/mark-read"),
    "lanis_get_courses": ApiRoute("GET", "/meinunterricht"),
    "lanis_get_course": ApiRoute("GET", "/meinunterricht/course/{course_id}"),
    "lanis_download_course_file": ApiRoute("GET", "/meinunterricht/file/{file_hash}"),
    "lanis_get_course_entry": ApiRoute("GET", "/meinunterricht/entry"),
    "lanis_get_weekly_course_view": ApiRoute("GET", "/meinunterricht/weekly"),
    "lanis_get_submissions": ApiRoute("GET", "/meinunterricht/submissions"),
    "lanis_set_homework_done": ApiRoute("POST", "/meinunterricht/homework-done"),
    "lanis_dsb_login": ApiRoute("POST", "/dsb/login"),
    "lanis_get_dsb_plan_urls": ApiRoute("POST", "/dsb/plan-urls"),
    "lanis_get_dsb_plan": ApiRoute("POST", "/dsb/plan"),
    "lanis_semantic_search": ApiRoute("GET", "/search/semantic"),
    "lanis_get_app_launch_url": ApiRoute("GET", "/app/{app_name}"),
}


@dataclass
class SessionCredentials:
    """LANIS API credentials scoped to one MCP client session."""

    access_token: str | None = None
    refresh_token: str | None = None


class CredentialStore:
    """Keep login state isolated between stateful MCP client sessions."""

    def __init__(self) -> None:
        self._sessions: WeakKeyDictionary[object, SessionCredentials] = (
            WeakKeyDictionary()
        )
        self._fallback = self._new_credentials()

    @staticmethod
    def _new_credentials() -> SessionCredentials:
        return SessionCredentials(
            access_token=os.getenv("LANIS_ACCESS_TOKEN"),
            refresh_token=os.getenv("LANIS_REFRESH_TOKEN"),
        )

    def get(self, ctx: Context) -> SessionCredentials:
        try:
            session = ctx.session
            credentials = self._sessions.get(session)
            if credentials is None:
                credentials = self._new_credentials()
                self._sessions[session] = credentials
            return credentials
        except (TypeError, ValueError):
            return self._fallback

    def clear(self, ctx: Context) -> None:
        credentials = self.get(ctx)
        credentials.access_token = None
        credentials.refresh_token = None


_credentials = CredentialStore()


mcp = FastMCP(
    "Lanis — Schulportal Hessen",
    website_url="https://lanis.arg-server.de",
    instructions=(
        "Lanis lets users and their assistants interact with Schulportal Hessen "
        "(SPH). Lanis is unofficial and is not operated by Schulportal Hessen. "
        "Users may say 'Schulportal', 'Schulportal Hessen', or 'SPH' without "
        "knowing the Lanis name; treat those as requests for these tools. The tools cover school "
        "lookup, login, profile, apps, calendar, timetable, substitutions, files, "
        "study groups, messages, courses, homework, submissions, and DSBmobile. "
        "After lanis_login, authentication is retained for this MCP session and "
        "access tokens must not be requested again. Ask for confirmation before "
        "sending or replying to messages, marking messages read, changing homework "
        "state, or logging out. Never reveal passwords, access tokens, refresh "
        "tokens, or token-bearing launch URLs except as a private link when the user "
        "explicitly asks to open a Schulportal app. When a visual interface would help, "
        "direct the user to the companion UI at https://lanis.arg-server.de."
    ),
    json_response=True,
    stateless_http=False,
)


def _client(access_token: str | None = None) -> LanisClient:
    return LanisClient(access_token=access_token)


def _transport_access_token(ctx: Context) -> str | None:
    """Read an API token supplied as an MCP HTTP transport header, if present."""
    try:
        request = ctx.request_context.request
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        token = headers.get("X-Session-Token") or headers.get("x-session-token")
        if token:
            return token
        authorization = headers.get("Authorization") or headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip() or None
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _object_result(tool_name: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TypeError(
            f"{tool_name} expected the LANIS API to return a JSON object, "
            f"but received {type(result).__name__}"
        )
    return result


async def _refresh_credentials(
    ctx: Context, refresh_token: str | None = None
) -> dict[str, Any]:
    credentials = _credentials.get(ctx)
    token = refresh_token or credentials.refresh_token
    if not token:
        raise ValueError(
            "No refresh token is available. Run lanis_login again or provide "
            "refresh_token to lanis_refresh_access_token."
        )
    result = _object_result(
        "lanis_refresh_access_token",
        await _client().request(
            "POST",
            "/auth/refresh",
            authenticated=False,
            json={"refresh_token": token},
        ),
    )
    new_access_token = result.get("access_token")
    if not isinstance(new_access_token, str) or not new_access_token:
        raise TypeError(
            "The LANIS API refresh response did not contain an access token"
        )
    credentials.access_token = new_access_token
    credentials.refresh_token = token
    return result


async def _api_request(
    ctx: Context,
    tool_name: str,
    *,
    path: str | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = API_ROUTES[tool_name]
    credentials = _credentials.get(ctx)
    access_token = _transport_access_token(ctx) or credentials.access_token
    try:
        result = await _client(access_token).request(
            route.method,
            path or route.path,
            authenticated=route.authenticated,
            params=params,
            json=json,
            data=data,
        )
    except LanisAPIError as error:
        if (
            error.status_code != 401
            or not route.authenticated
            or not credentials.refresh_token
        ):
            raise
        await _refresh_credentials(ctx)
        result = await _client(credentials.access_token).request(
            route.method,
            path or route.path,
            authenticated=True,
            params=params,
            json=json,
            data=data,
        )
    return _object_result(tool_name, result)


async def _public_request(
    tool_name: str,
    *,
    path: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = API_ROUTES[tool_name]
    result = await _client().request(
        route.method,
        path or route.path,
        authenticated=False,
        params=params,
    )
    return _object_result(tool_name, result)


@mcp.tool(title="Schulportal API status", annotations=READ_ONLY)
async def lanis_health() -> dict[str, Any]:
    """Check whether the LANIS service for Schulportal Hessen is operational. REST: GET /health."""
    return await _public_request("lanis_health")


@mcp.tool(title="Schulportal API statistics", annotations=READ_ONLY)
async def lanis_metrics_stats() -> dict[str, Any]:
    """Get aggregate LANIS database and task-queue statistics. REST: GET /metrics/stats."""
    return await _public_request("lanis_metrics_stats")


@mcp.tool(title="Log in to Schulportal Hessen", annotations=MUTATING)
async def lanis_login(
    ctx: Context,
    school_id: Annotated[
        str,
        Field(
            description="The Schulportal Hessen school ID, found with lanis_search_schools."
        ),
    ],
    username: Annotated[
        str,
        Field(
            description="The user's Schulportal Hessen username without a school prefix."
        ),
    ],
    password: Annotated[
        str,
        Field(
            description="The user's Schulportal Hessen password; treat it as secret.",
            json_schema_extra={"writeOnly": True, "format": "password"},
        ),
    ],
) -> dict[str, Any]:
    """Log in to Schulportal Hessen and retain the returned tokens for this MCP session. REST: POST /login."""
    result = _object_result(
        "lanis_login",
        await _client().request(
            "POST",
            "/login",
            authenticated=False,
            json={
                "school_id": school_id,
                "username": username,
                "password": password,
            },
        ),
    )
    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise TypeError("The LANIS API login response did not contain both tokens")
    credentials = _credentials.get(ctx)
    credentials.access_token = access_token
    credentials.refresh_token = refresh_token
    return result


@mcp.tool(title="Refresh Schulportal login", annotations=MUTATING)
async def lanis_refresh_access_token(
    ctx: Context,
    refresh_token: Annotated[
        str | None,
        Field(
            description=(
                "A LANIS refresh token. Omit it after lanis_login because the MCP "
                "session retains it automatically."
            ),
            json_schema_extra={"writeOnly": True},
        ),
    ] = None,
) -> dict[str, Any]:
    """Refresh the Schulportal access token and retain it for this MCP session. REST: POST /auth/refresh."""
    return await _refresh_credentials(ctx, refresh_token)


@mcp.tool(title="Log out of Schulportal Hessen", annotations=PRIVATE_DESTRUCTIVE)
async def lanis_logout(ctx: Context) -> dict[str, Any]:
    """Invalidate the current Schulportal tokens and session after user confirmation. REST: POST /logout."""
    result = await _api_request(ctx, "lanis_logout")
    _credentials.clear(ctx)
    return result


@mcp.tool(title="List Hessian schools", annotations=READ_ONLY)
async def lanis_list_schools() -> dict[str, Any]:
    """List all Schulportal Hessen districts and schools without logging in. REST: GET /school-list."""
    return await _public_request("lanis_list_schools")


@mcp.tool(title="List schools in a district", annotations=READ_ONLY)
async def lanis_get_district_schools(
    district_id: Annotated[
        str, Field(description="A district ID returned by lanis_list_schools.")
    ],
) -> dict[str, Any]:
    """List Schulportal Hessen schools in one Hessian district. REST: GET /school-list/district/{district_id}."""
    path = f"/school-list/district/{quote(district_id, safe='')}"
    return await _public_request("lanis_get_district_schools", path=path)


@mcp.tool(title="Search Hessian schools", annotations=READ_ONLY)
async def lanis_search_schools(
    q: Annotated[
        str,
        Field(
            description="School name, town, or other text to search in the Hessian school directory."
        ),
    ],
) -> dict[str, Any]:
    """Search Schulportal Hessen schools and obtain the school_id needed for login. REST: GET /school-list/search?q=..."""
    return await _public_request("lanis_search_schools", params={"q": q})


@mcp.tool(title="List available Schulportal apps", annotations=READ_ONLY)
async def lanis_get_apps(ctx: Context) -> dict[str, Any]:
    """List Schulportal Hessen apps available to the current user. REST: GET /apps."""
    return await _api_request(ctx, "lanis_get_apps")


@mcp.tool(title="List enabled Schulportal modules", annotations=READ_ONLY)
async def lanis_get_modules(ctx: Context) -> dict[str, Any]:
    """List enabled Schulportal Hessen modules and launch metadata. REST: GET /modules."""
    return await _api_request(ctx, "lanis_get_modules")


@mcp.tool(title="Get Schulportal profile", annotations=READ_ONLY)
async def lanis_get_user(ctx: Context) -> dict[str, Any]:
    """Get the authenticated user's Schulportal Hessen profile. REST: GET /benutzer."""
    return await _api_request(ctx, "lanis_get_user")


@mcp.tool(title="Get Schulportal calendar", annotations=READ_ONLY)
async def lanis_get_calendar(ctx: Context) -> dict[str, Any]:
    """Get the Schulportal calendar overview, filters, and views. REST: GET /kalender."""
    return await _api_request(ctx, "lanis_get_calendar")


@mcp.tool(title="Get Schulportal calendar events", annotations=READ_ONLY)
async def lanis_get_calendar_events(
    ctx: Context,
    year: Annotated[
        int, Field(description="Calendar year, or 0 for the API default.")
    ] = 0,
    start: Annotated[str, Field(description="Calendar start/range selector.")] = "year",
    category: Annotated[str, Field(description="Optional category filter.")] = "",
    search: Annotated[str, Field(description="Optional text search filter.")] = "",
    target: Annotated[str, Field(description="Optional audience/target filter.")] = "",
    view_id: Annotated[
        str | None, Field(description="Optional calendar view ID.")
    ] = None,
) -> dict[str, Any]:
    """Get filtered Schulportal Hessen calendar events. REST: GET /kalender/events."""
    params: dict[str, Any] = {
        "year": year,
        "start": start,
        "category": category,
        "search": search,
        "target": target,
    }
    if view_id is not None:
        params["view_id"] = view_id
    return await _api_request(ctx, "lanis_get_calendar_events", params=params)


@mcp.tool(title="Get one Schulportal event", annotations=READ_ONLY)
async def lanis_get_calendar_event(
    ctx: Context,
    event_id: Annotated[str, Field(description="The calendar event ID.")],
    view_id: Annotated[
        str | None, Field(description="Optional calendar view ID.")
    ] = None,
) -> dict[str, Any]:
    """Get full details for one Schulportal Hessen calendar event. REST: GET /kalender/event/{event_id}."""
    params = {"view_id": view_id} if view_id is not None else None
    path = f"/kalender/event/{quote(event_id, safe='')}"
    return await _api_request(ctx, "lanis_get_calendar_event", path=path, params=params)


@mcp.tool(title="Get Schulportal substitution plan", annotations=READ_ONLY)
async def lanis_get_substitution_plan(
    ctx: Context,
    include_raw: Annotated[
        bool, Field(description="Whether to include the unprocessed source HTML.")
    ] = False,
) -> dict[str, Any]:
    """Get the user's Schulportal Hessen substitution plan. REST: GET /vertretungsplan."""
    return await _api_request(
        ctx,
        "lanis_get_substitution_plan",
        params={"include_raw": include_raw},
    )


@mcp.tool(title="Get Schulportal timetable", annotations=READ_ONLY)
async def lanis_get_timetable(ctx: Context) -> dict[str, Any]:
    """Get the authenticated user's Schulportal Hessen timetable. REST: GET /stundenplan."""
    return await _api_request(ctx, "lanis_get_timetable")


@mcp.tool(title="List Schulportal files", annotations=READ_ONLY)
async def lanis_get_file_storage(
    ctx: Context,
    folder_id: Annotated[
        int, Field(description="The folder ID to list; use 0 for the storage root.")
    ] = 0,
) -> dict[str, Any]:
    """List one folder in the user's Schulportal Hessen file storage. REST: GET /dateispeicher."""
    return await _api_request(
        ctx, "lanis_get_file_storage", params={"folder_id": folder_id}
    )


@mcp.tool(title="Search Schulportal files", annotations=READ_ONLY)
async def lanis_search_file_storage(
    ctx: Context,
    q: Annotated[
        str, Field(description="Text to search for in Schulportal file storage.")
    ],
) -> dict[str, Any]:
    """Search the user's Schulportal Hessen file storage. REST: GET /dateispeicher/search?q=..."""
    return await _api_request(ctx, "lanis_search_file_storage", params={"q": q})


@mcp.tool(title="Get Schulportal study groups", annotations=READ_ONLY)
async def lanis_get_study_groups(ctx: Context) -> dict[str, Any]:
    """Get the user's Schulportal Hessen LernGruppen overview. REST: GET /lerngruppen."""
    return await _api_request(ctx, "lanis_get_study_groups")


@mcp.tool(title="List Schulportal messages", annotations=READ_ONLY)
async def lanis_get_message_headers(
    ctx: Context,
    get_type: Annotated[
        str,
        Field(
            description=(
                "Schulportal message filter; common values include All, Unread, "
                "Sent, and visibleOnly."
            )
        ),
    ] = "All",
    last: Annotated[
        int, Field(description="Pagination offset passed unchanged to the API.")
    ] = 0,
) -> dict[str, Any]:
    """List Schulportal Hessen conversation headers without changing read state. REST: GET /nachrichten/headers."""
    return await _api_request(
        ctx,
        "lanis_get_message_headers",
        params={"get_type": get_type, "last": last},
    )


@mcp.tool(title="Read a Schulportal conversation", annotations=READ_ONLY)
async def lanis_get_conversation(
    ctx: Context,
    conversation_id: Annotated[
        str, Field(description="The Schulportal conversation uniqid.")
    ],
    last: Annotated[
        int, Field(description="Pagination offset passed unchanged to the API.")
    ] = 0,
) -> dict[str, Any]:
    """Read one Schulportal Hessen message conversation. REST: GET /nachrichten/{conversation_id}."""
    path = f"/nachrichten/{quote(conversation_id, safe='')}"
    return await _api_request(
        ctx, "lanis_get_conversation", path=path, params={"last": last}
    )


@mcp.tool(title="Search Schulportal recipients", annotations=READ_ONLY)
async def lanis_search_message_recipients(
    ctx: Context,
    q: Annotated[
        str, Field(description="Name or text used to find Schulportal recipient IDs.")
    ],
) -> dict[str, Any]:
    """Search eligible Schulportal Hessen message recipients. REST: GET /nachrichten/search?q=..."""
    return await _api_request(ctx, "lanis_search_message_recipients", params={"q": q})


@mcp.tool(title="Send a Schulportal message", annotations=EXTERNAL_DESTRUCTIVE)
async def lanis_send_message(
    ctx: Context,
    recipients: Annotated[
        list[str],
        Field(description="Recipient IDs returned by lanis_search_message_recipients."),
    ],
    subject: Annotated[str, Field(description="The exact message subject to send.")],
    body: Annotated[str, Field(description="The exact message body to send.")],
) -> dict[str, Any]:
    """Send a new Schulportal Hessen message after confirming recipients, subject, and body. REST: POST /nachrichten/send."""
    return await _api_request(
        ctx,
        "lanis_send_message",
        json={"recipients": recipients, "subject": subject, "body": body},
    )


@mcp.tool(title="Reply to a Schulportal message", annotations=EXTERNAL_DESTRUCTIVE)
async def lanis_reply_to_message(
    ctx: Context,
    conversation_id: Annotated[
        str, Field(description="The Schulportal conversation uniqid to reply to.")
    ],
    body: Annotated[str, Field(description="The exact reply body to send.")],
    to: Annotated[
        str, Field(description="Recipient selector: 'all' or a specific user ID.")
    ] = "all",
) -> dict[str, Any]:
    """Reply in a Schulportal Hessen conversation after confirming the target and body. REST: POST /nachrichten/reply."""
    return await _api_request(
        ctx,
        "lanis_reply_to_message",
        json={"conversation_id": conversation_id, "body": body, "to": to},
    )


@mcp.tool(title="Mark a Schulportal message read", annotations=PRIVATE_DESTRUCTIVE)
async def lanis_mark_message_read(
    ctx: Context,
    conversation_id: Annotated[
        str, Field(description="The Schulportal conversation uniqid to mark as read.")
    ],
) -> dict[str, Any]:
    """Mark one Schulportal Hessen conversation read after confirmation. REST: POST /nachrichten/mark-read."""
    return await _api_request(ctx, "lanis_mark_message_read", json=conversation_id)


@mcp.tool(title="List Schulportal courses", annotations=READ_ONLY)
async def lanis_get_courses(ctx: Context) -> dict[str, Any]:
    """Get the user's Schulportal Hessen Mein Unterricht overview. REST: GET /meinunterricht."""
    return await _api_request(ctx, "lanis_get_courses")


@mcp.tool(title="Get a Schulportal course", annotations=READ_ONLY)
async def lanis_get_course(
    ctx: Context,
    course_id: Annotated[
        str, Field(description="A course ID returned by lanis_get_courses.")
    ],
) -> dict[str, Any]:
    """Get one Schulportal course with entries, materials, and file hashes. REST: GET /meinunterricht/course/{course_id}."""
    path = f"/meinunterricht/course/{quote(course_id, safe='')}"
    return await _api_request(ctx, "lanis_get_course", path=path)


@mcp.tool(title="Download a Schulportal course file", annotations=READ_ONLY)
async def lanis_download_course_file(
    ctx: Context,
    file_hash: Annotated[
        str, Field(description="A file_hash returned in a course file record.")
    ],
    max_bytes: Annotated[
        int | None,
        Field(
            description=(
                "Optional maximum download size in bytes; omit for full REST API parity."
            ),
            ge=1,
        ),
    ] = None,
) -> dict[str, Any]:
    """Download a Schulportal course file and return its bytes as base64 plus metadata. REST: GET /meinunterricht/file/{file_hash}."""
    credentials = _credentials.get(ctx)
    access_token = _transport_access_token(ctx) or credentials.access_token
    try:
        return await _client(access_token).download_file(
            file_hash, access_token=access_token, max_bytes=max_bytes
        )
    except LanisAPIError as error:
        if error.status_code != 401 or not credentials.refresh_token:
            raise
        await _refresh_credentials(ctx)
        return await _client(credentials.access_token).download_file(
            file_hash,
            access_token=credentials.access_token,
            max_bytes=max_bytes,
        )


@mcp.tool(title="Get a Schulportal course entry", annotations=READ_ONLY)
async def lanis_get_course_entry(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="The entry URL returned by another Mein Unterricht tool."),
    ],
) -> dict[str, Any]:
    """Get detailed content for one Schulportal Hessen course entry. REST: GET /meinunterricht/entry?url=..."""
    return await _api_request(ctx, "lanis_get_course_entry", params={"url": url})


@mcp.tool(title="Get weekly Schulportal coursework", annotations=READ_ONLY)
async def lanis_get_weekly_course_view(ctx: Context) -> dict[str, Any]:
    """Get the user's weekly Schulportal Hessen Mein Unterricht view. REST: GET /meinunterricht/weekly."""
    return await _api_request(ctx, "lanis_get_weekly_course_view")


@mcp.tool(title="Get Schulportal submissions", annotations=READ_ONLY)
async def lanis_get_submissions(ctx: Context) -> dict[str, Any]:
    """Get the user's Schulportal Hessen course submissions. REST: GET /meinunterricht/submissions."""
    return await _api_request(ctx, "lanis_get_submissions")


@mcp.tool(title="Set Schulportal homework state", annotations=MUTATING)
async def lanis_set_homework_done(
    ctx: Context,
    course_id: Annotated[
        str, Field(description="The course containing the homework entry.")
    ],
    entry_id: Annotated[
        str, Field(description="The homework/course entry ID to update.")
    ],
    done: Annotated[
        bool, Field(description="True to mark done or false to clear the done state.")
    ] = True,
) -> dict[str, Any]:
    """Set or clear a Schulportal homework-done state after confirmation. REST: POST /meinunterricht/homework-done."""
    return await _api_request(
        ctx,
        "lanis_set_homework_done",
        data={
            "course_id": course_id,
            "entry_id": entry_id,
            "done": str(done).lower(),
        },
    )


@mcp.tool(title="Log in to DSBmobile", annotations=MUTATING)
async def lanis_dsb_login(
    ctx: Context,
    username: Annotated[
        str, Field(description="The separate DSBmobile username or school identifier.")
    ],
    password: Annotated[
        str,
        Field(
            description="The separate DSBmobile password; treat it as secret.",
            json_schema_extra={"writeOnly": True, "format": "password"},
        ),
    ],
) -> dict[str, Any]:
    """Authenticate the current Schulportal session to DSBmobile. REST: POST /dsb/login."""
    return await _api_request(
        ctx,
        "lanis_dsb_login",
        json={"username": username, "password": password},
    )


@mcp.tool(title="Get DSBmobile plan URLs", annotations=MUTATING)
async def lanis_get_dsb_plan_urls(
    ctx: Context,
    username: Annotated[
        str, Field(description="The separate DSBmobile username or school identifier.")
    ],
    password: Annotated[
        str,
        Field(
            description="The separate DSBmobile password; treat it as secret.",
            json_schema_extra={"writeOnly": True, "format": "password"},
        ),
    ],
) -> dict[str, Any]:
    """Authenticate to DSBmobile and list its substitution-plan iframe URLs. REST: POST /dsb/plan-urls."""
    return await _api_request(
        ctx,
        "lanis_get_dsb_plan_urls",
        json={"username": username, "password": password},
    )


@mcp.tool(title="Get a DSBmobile substitution plan", annotations=MUTATING)
async def lanis_get_dsb_plan(
    ctx: Context,
    username: Annotated[
        str | None,
        Field(description="Optional DSBmobile username when authentication is needed."),
    ] = None,
    password: Annotated[
        str | None,
        Field(
            description="Optional DSBmobile password when authentication is needed.",
            json_schema_extra={"writeOnly": True, "format": "password"},
        ),
    ] = None,
    plan_index: Annotated[
        int, Field(description="Plan iframe index to fetch when plan_url is omitted.")
    ] = 0,
    plan_url: Annotated[
        str | None, Field(description="Explicit DSB plan URL, overriding plan_index.")
    ] = None,
    include_raw: Annotated[
        bool, Field(description="Whether to include the unprocessed plan HTML.")
    ] = False,
) -> dict[str, Any]:
    """Fetch and parse a DSBmobile substitution plan by index or explicit URL. REST: POST /dsb/plan."""
    return await _api_request(
        ctx,
        "lanis_get_dsb_plan",
        json={
            "username": username,
            "password": password,
            "plan_index": plan_index,
            "plan_url": plan_url,
            "include_raw": include_raw,
        },
    )


@mcp.tool(title="Search across Schulportal content", annotations=READ_ONLY)
async def lanis_semantic_search(
    ctx: Context,
    q: Annotated[
        str,
        Field(
            description="Natural-language query across the user's Schulportal content."
        ),
    ],
    top_k: Annotated[
        int,
        Field(
            description="Maximum number of matches, from 1 through 100.", ge=1, le=100
        ),
    ] = 20,
) -> dict[str, Any]:
    """Semantically search messages, courses, calendar data, and modules when enabled by the API deployment. REST: GET /search/semantic."""
    return await _api_request(
        ctx, "lanis_semantic_search", params={"q": q, "top_k": top_k}
    )


@mcp.tool(title="Open a Schulportal SSO app", annotations=READ_ONLY)
async def lanis_get_app_launch_url(
    ctx: Context,
    app_name: Annotated[
        str, Field(description="An app/module name returned by lanis_get_modules.")
    ],
) -> dict[str, Any]:
    """Build the browser URL for a Schulportal SSO app without opening it. REST feature: GET /app/{app_name}."""
    credentials = _credentials.get(ctx)
    token = _transport_access_token(ctx) or credentials.access_token
    url = _client(token).app_launch_url(app_name, token)
    return {
        "url": url,
        "sensitive": True,
        "instructions": (
            "Present this as a private link only to the authenticated user who asked "
            "to open the app; never quote the token separately or reuse the URL."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lanis MCP server for interacting with Schulportal Hessen"
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
