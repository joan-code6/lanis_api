"""OAuth 2.1 / PKCE authorization surface for remote LANIS MCP clients."""

import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import agent_store as store
from . import auth_db

router = APIRouter(tags=["Agents"])
# One source of truth for OAuth, presets, tool discovery and Custom controls.
SCOPES = {
    "meetings:read": {
        "title": "Videokonferenzen lesen",
        "description": "Räume und persönliche Beitrittslinks",
        "group": "Videokonferenz",
        "sensitive": True,
    },
    "schoolwork:read": {
        "title": "Unterricht lesen",
        "description": "Kurse und Hausaufgaben",
        "group": "Unterricht",
    },
    "schoolwork:write": {
        "title": "Hausaufgaben ändern",
        "description": "Erledigt-Status ändern",
        "group": "Unterricht",
    },
    "timetable:read": {
        "title": "Stundenplan lesen",
        "description": "Stunden und Vertretungen",
        "group": "Stundenplan",
    },
    "timetable:write": {
        "title": "Stundenplan ändern",
        "description": "Eigene Stunden bearbeiten",
        "group": "Stundenplan",
    },
    "calendar:read": {
        "title": "Kalender lesen",
        "description": "Schultermine",
        "group": "Kalender",
    },
    "messages:read": {
        "title": "Nachrichten lesen",
        "description": "Übersicht und bereits geladene Inhalte",
        "group": "Nachrichten",
    },
    "messages:mark-read": {
        "title": "Öffnen & als gelesen markieren",
        "description": "Neue Inhalte laden und als gelesen markieren",
        "group": "Nachrichten",
    },
    "messages:write": {
        "title": "Nachrichten senden",
        "description": "Senden und antworten",
        "group": "Nachrichten",
    },
    "files:read": {
        "title": "Dateien lesen",
        "description": "Dateispeicher und Dateiverteilung",
        "group": "Dateien",
    },
    "submissions:read": {
        "title": "Abgaben lesen",
        "description": "Aufgaben und Abgabedetails",
        "group": "Abgaben",
    },
    "submissions:write": {
        "title": "Abgaben ändern",
        "description": "Dateien hochladen und entfernen",
        "group": "Abgaben",
    },
    "elections:read": {
        "title": "Wahlen lesen",
        "description": "Formulare und Optionen",
        "group": "Wahlen",
    },
    "elections:write": {
        "title": "Wahlen abgeben",
        "description": "Auswahl verbindlich absenden",
        "group": "Wahlen",
    },
    "profile:read": {
        "title": "Profil lesen",
        "description": "Schulprofil und Module",
        "group": "Profil",
    },
    "grades:read": {
        "title": "Noten lesen",
        "description": "Leistungen eines Kurses",
        "group": "Sensible Daten",
        "sensitive": True,
    },
    "attendance:read": {
        "title": "Fehlzeiten lesen",
        "description": "Anwesenheit und Fehlstunden",
        "group": "Sensible Daten",
        "sensitive": True,
    },
    "preferences:read": {
        "title": "Einstellungen lesen",
        "description": "Persönliche LANIS-Einstellungen",
        "group": "Einstellungen",
    },
    "preferences:write": {
        "title": "Einstellungen ändern",
        "description": "Persönliche LANIS-Einstellungen anpassen",
        "group": "Einstellungen",
    },
}
RESTRICTED_SCOPES = [
    key
    for key, value in SCOPES.items()
    if key.endswith(":read") and not value.get("sensitive")
]
# Clients request the complete capability ceiling; the person chooses the grant.
DEFAULT_SCOPES = " ".join(SCOPES)


def access_presets(requested):
    return {
        "restricted": [s for s in RESTRICTED_SCOPES if s in requested],
        "full": list(requested) if set(SCOPES).issubset(requested) else None,
    }


def access_mode(scopes):
    if set(scopes) == set(SCOPES):
        return "full"
    if set(scopes).issubset(RESTRICTED_SCOPES):
        return "restricted"
    return "custom"


async def active_account(db, user_id):
    # This lookup MUST use the caller's transaction. The credential helper also
    # cleans expired sessions on another connection, which would self-deadlock.
    return await store.one(
        db,
        "SELECT 1 FROM refresh_tokens WHERE user_id=? AND expires_at>? LIMIT 1",
        (user_id, datetime.now(timezone.utc).replace(tzinfo=None)),
    )


HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


def api_url():
    return os.getenv(
        "LANIS_AGENT_ISSUER", os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    ).rstrip("/")


def ui_url():
    return os.getenv("LANIS_AGENT_UI_ORIGIN", "http://localhost:5173").rstrip("/")


def oauth_error(error, description, status=400):
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers=HEADERS,
    )


def validate_redirect(value):
    try:
        u = urlsplit(value)
        port = u.port
        del port
    except ValueError:
        raise HTTPException(400, "Invalid redirect URI")
    if (
        u.scheme not in ("https", "http")
        or not u.hostname
        or u.username
        or u.password
        or u.fragment
        or (u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"))
    ):
        raise HTTPException(
            400, "Redirect URI must use HTTPS (HTTP is only allowed on loopback)."
        )
    return value


async def owner(request: Request):
    from .api import local_auth_dependency

    token = request.headers.get("X-Session-Token")
    if not token:
        raise HTTPException(401, "Log in to LANIS to manage agent connections.")
    return await local_auth_dependency(token)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def resource_metadata():
    return {
        "resource": api_url() + "/mcp",
        "authorization_servers": [api_url()],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_metadata():
    base = api_url()
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(SCOPES),
    }


class Registration(BaseModel):
    client_name: str = Field(default="External agent", min_length=1, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = ["authorization_code", "refresh_token"]
    response_types: list[str] = ["code"]


@router.post("/oauth/register", status_code=201)
async def register(body: Registration, request: Request):
    host = request.client.host if request.client else "unknown"
    if not await store.rate_limit("register:" + host, 300, 3600):
        return oauth_error("slow_down", "Too many registrations. Try again later.", 429)
    if (
        body.token_endpoint_auth_method != "none"
        or set(body.grant_types) - {"authorization_code", "refresh_token"}
        or body.response_types != ["code"]
    ):
        return oauth_error(
            "invalid_client_metadata",
            "Use a public PKCE client, code response type, and authorization_code/refresh_token grants.",
        )
    data = body.model_dump()
    data["client_name"] = body.client_name.strip() or "External agent"
    if any(len(uri) > 2048 for uri in body.redirect_uris):
        return oauth_error("invalid_redirect_uri", "Redirect URI is too long.")
    try:
        data["redirect_uris"] = [validate_redirect(uri) for uri in body.redirect_uris]
    except (HTTPException, ValueError):
        return oauth_error(
            "invalid_redirect_uri",
            "Redirect URI must use HTTPS (HTTP is only allowed on loopback).",
        )
    client_id = secrets.token_urlsafe(24)
    async with store.database(True) as db:
        await store.prune(db)
        row = await store.one(db, "SELECT COUNT(*) AS count FROM agent_clients")
        if row["count"] >= 10000:
            # Unused registrations cannot monopolize capacity for 90 days.
            # Pending requests retain their own client/redirect snapshot.
            await db.execute(
                "DELETE FROM agent_clients WHERE id IN ("
                "SELECT id FROM agent_clients "
                "WHERE id NOT IN (SELECT client_id FROM agent_tokens) "
                "ORDER BY expires, id LIMIT 1000)"
            )
            row = await store.one(db, "SELECT COUNT(*) AS count FROM agent_clients")
        if row["count"] >= 10000:
            return oauth_error(
                "temporarily_unavailable",
                "Registration capacity reached; retry later.",
                503,
            )
        await db.execute(
            "INSERT INTO agent_clients VALUES (?, ?, ?)",
            (client_id, json.dumps(data), time.time() + 90 * 86400),
        )
    return JSONResponse(
        {"client_id": client_id, "client_id_issued_at": int(time.time()), **data},
        status_code=201,
        headers=HEADERS,
    )


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    client_id: str,
    redirect_uri: str,
    code_challenge: str = "",
    state: str = "",
    response_type: str = "code",
    code_challenge_method: str = "S256",
    scope: str = DEFAULT_SCOPES,
    resource: str = "",
):
    host = request.client.host if request.client else "unknown"
    if not await store.rate_limit("authorize:" + host, 120, 3600):
        return oauth_error(
            "slow_down", "Too many authorization requests. Try again later.", 429
        )
    if (
        response_type != "code"
        or code_challenge_method != "S256"
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)
    ):
        return oauth_error(
            "invalid_request", "Authorization code flow with S256 PKCE is required."
        )
    if not state or len(state) > 2048:
        return oauth_error(
            "invalid_request", "A bounded, nonempty state value is required."
        )
    scopes = list(dict.fromkeys(scope.split()))
    if not scopes or set(scopes) - SCOPES.keys():
        return oauth_error("invalid_scope", "Request one or more advertised scopes.")
    if resource and resource != api_url() + "/mcp":
        return oauth_error(
            "invalid_target", "The resource must be the LANIS MCP endpoint."
        )
    async with store.database(True) as db:
        await store.prune(db)
        client = await store.one(
            db, "SELECT data FROM agent_clients WHERE id = ?", (client_id,)
        )
        if (
            not client
            or redirect_uri not in json.loads(client["data"])["redirect_uris"]
        ):
            return oauth_error(
                "invalid_request", "Unknown client or unregistered redirect URI."
            )
        request_id = secrets.token_urlsafe(32)
        data = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "challenge": code_challenge,
            "state": state,
            "scopes": scopes,
            "client_name": json.loads(client["data"])["client_name"],
            "resource": api_url() + "/mcp",
        }
        await db.execute(
            "INSERT INTO agent_requests VALUES (?, ?, ?, NULL)",
            (store.digest(request_id), json.dumps(data), time.time() + 600),
        )
    # Only an opaque request ID reaches the browser. No callbacks or tokens in UI query parameters.
    return RedirectResponse(
        ui_url()
        + "/agents/authorize?"
        + urlencode({"request": request_id, "issuer": api_url()}),
        status_code=303,
        headers=HEADERS,
    )


async def pending(db, request_id):
    row = await store.one(
        db,
        "SELECT * FROM agent_requests WHERE id = ? AND expires > ?",
        (store.digest(request_id), time.time()),
    )
    if not row:
        raise HTTPException(
            410,
            "Diese Anfrage ist abgelaufen oder wurde bereits beantwortet. Starte die Verbindung in deinem Agenten erneut.",
        )
    return json.loads(row["data"])


def consent_identity(data):
    """Only operator-pinned client IDs and exact callbacks establish identity.

    Dynamic registration (including a familiar name or callback hostname) is
    never itself verification. Invalid configuration fails closed.
    """
    try:
        trusted = json.loads(os.environ.get("LANIS_AGENT_TRUSTED_CLIENTS", "{}"))
        entry = trusted.get(data["client_id"], {})
        name = entry.get("name")
        if (
            isinstance(name, str)
            and name.strip()
            and data["redirect_uri"] in entry.get("redirect_uris", [])
            and isinstance(entry.get("redirect_uris"), list)
        ):
            return name.strip(), True
    except (ValueError, TypeError, AttributeError):
        pass
    return data["client_name"], False


@router.get("/agents/authorization/{request_id}")
async def authorization_details(request_id: str, auth: Annotated[Any, Depends(owner)]):
    async with store.database() as db:
        data = await pending(db, request_id)
    client_name, verified = consent_identity(data)
    return JSONResponse(
        {
            "client_name": client_name,
            "client_id": data["client_id"],
            "redirect_origin": urlsplit(data["redirect_uri"]).netloc,
            "scopes": [{"id": s, **SCOPES[s]} for s in data["scopes"]],
            "username": auth.username,
            "school_id": auth.school_id,
            "verified": verified,
            "presets": access_presets(data["scopes"]),
        },
        headers=HEADERS,
    )


class Decision(BaseModel):
    approve: bool
    mode: Literal["restricted", "full", "custom"] = "custom"
    scopes: list[str] = Field(default_factory=list, max_length=20)
    name: str = Field(default="", max_length=100)


@router.post("/agents/authorization/{request_id}")
async def decide(request_id: str, body: Decision, auth: Annotated[Any, Depends(owner)]):
    # Authenticated browser requests already hold a shared lifecycle lease in
    # middleware. Do not try to upgrade it to an exclusive lock here. SQLite
    # serializes the account check, consent consumption and grant insertion.
    async with store.database(True) as db:
        if not await active_account(db, auth.user_id):
            raise HTTPException(401, "Log in again before approving a connection.")
        data = await pending(db, request_id)
        scopes = list(dict.fromkeys(body.scopes))
        if body.approve and body.mode != "custom":
            selected = access_presets(data["scopes"])[body.mode]
            if selected is None:
                raise HTTPException(
                    400,
                    "The client did not request full access. Reconnect with all scopes.",
                )
            scopes = selected
        if body.approve and (not scopes or set(scopes) - set(data["scopes"])):
            raise HTTPException(
                400, "Choose at least one of the requested permissions."
            )
        await db.execute(
            "DELETE FROM agent_requests WHERE id = ?", (store.digest(request_id),)
        )
        query = {"state": data["state"]}
        if not body.approve:
            query["error"] = "access_denied"
        else:
            grant_id, code = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
            grant = {
                "name": body.name.strip() or data["client_name"],
                "client_name": data["client_name"],
                "client_id": data["client_id"],
                "redirect_origin": urlsplit(data["redirect_uri"]).netloc,
                "scopes": scopes,
                "requested_scopes": data["scopes"],
            }
            await db.execute(
                "INSERT INTO agent_grants(id,user_id,data,created) VALUES(?,?,?,?)",
                (grant_id, auth.user_id, json.dumps(grant), time.time()),
            )
            await db.execute(
                "INSERT INTO agent_tokens VALUES(?,?,?,?,?,?,0,?)",
                (
                    store.digest(code),
                    auth.user_id,
                    grant_id,
                    data["client_id"],
                    "code",
                    time.time() + 120,
                    json.dumps(data),
                ),
            )
            query["code"] = code
        separator = "&" if "?" in data["redirect_uri"] else "?"
    return JSONResponse(
        {"redirect_url": data["redirect_uri"] + separator + urlencode(query)},
        headers=HEADERS,
    )


async def issue_tokens(db, row):
    access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    now = time.time()
    # A refresh family has an absolute lifetime, even when rotated.
    family_expires = json.loads(row["data"]).get("family_expires", now + 90 * 86400)
    grant = await store.one(
        db, "SELECT data FROM agent_grants WHERE id = ?", (row["grant_id"],)
    )
    for token, kind, expiry in (
        (access, "access", min(now + 3600, family_expires)),
        (refresh, "refresh", family_expires),
    ):
        await db.execute(
            "INSERT INTO agent_tokens VALUES(?,?,?,?,?,?,0,?)",
            (
                store.digest(token),
                row["user_id"],
                row["grant_id"],
                row["client_id"],
                kind,
                expiry,
                json.dumps({"family_expires": family_expires}),
            ),
        )
    return JSONResponse(
        {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": max(0, int(min(3600, family_expires - now))),
            "scope": " ".join(json.loads(grant["data"])["scopes"]),
        },
        headers=HEADERS,
    )


@router.post("/oauth/token")
async def token(request: Request):
    host = request.client.host if request.client else "unknown"
    if not await store.rate_limit("token-ip:" + host, 2000, 60):
        return oauth_error(
            "slow_down", "Too many token requests. Retry in one minute.", 429
        )
    form = await request.form()
    client_id = str(form.get("client_id", ""))
    kind = {"authorization_code": "code", "refresh_token": "refresh"}.get(
        form.get("grant_type")
    )
    if not kind:
        return oauth_error(
            "unsupported_grant_type", "Use authorization_code or refresh_token."
        )
    raw = str(form.get("code" if kind == "code" else "refresh_token", ""))
    async with store.database(True) as db:
        row = await store.one(
            db,
            "SELECT t.* FROM agent_tokens t JOIN agent_grants g ON g.id=t.grant_id WHERE t.id=? AND t.kind=? AND g.revoked=0",
            (store.digest(raw), kind),
        )
        if not row or row["client_id"] != form.get("client_id"):
            return oauth_error(
                "invalid_grant",
                "Invalid token or client. Reconnect LANIS from your agent.",
            )
        if row["used"]:
            await db.execute(
                "UPDATE agent_grants SET revoked=1 WHERE id=?", (row["grant_id"],)
            )
            return oauth_error(
                "invalid_grant", "Token replay detected. Reconnect LANIS."
            )
        if row["expires"] <= time.time() or not await active_account(
            db, row["user_id"]
        ):
            return oauth_error(
                "invalid_grant",
                "Authorization expired. Sign in to LANIS and reconnect.",
            )
        data = json.loads(row["data"])
        if form.get("resource") and form["resource"] != api_url() + "/mcp":
            return oauth_error("invalid_target", "Use the LANIS MCP resource.")
        if kind == "code":
            verifier = str(form.get("code_verifier", ""))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            if (
                not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                or not secrets.compare_digest(challenge, data["challenge"])
                or form.get("redirect_uri") != data["redirect_uri"]
            ):
                return oauth_error(
                    "invalid_grant",
                    "The verifier or redirect URI does not match this authorization.",
                )
        # Only proof of a valid code/refresh token may charge this shared
        # client bucket. Invalid callers are limited by their source IP above.
        if not await store.rate_limit(
            "token-client:" + store.digest(client_id), 120, 60, db=db
        ):
            return oauth_error(
                "slow_down",
                "Too many token requests for this client. Retry in one minute.",
                429,
            )
        # The user's current grant is authoritative. Ignore a client-resubmitted
        # scope ceiling and report the actual issued scope without elevating it.
        await db.execute("UPDATE agent_tokens SET used=1 WHERE id=?", (row["id"],))
        return await issue_tokens(db, row)


@router.post("/oauth/revoke")
async def revoke_token(request: Request):
    form = await request.form()
    async with store.database(True) as db:
        row = await store.one(
            db,
            "SELECT grant_id FROM agent_tokens WHERE id=? AND client_id=?",
            (store.digest(str(form.get("token", ""))), str(form.get("client_id", ""))),
        )
        if row:
            await db.execute(
                "UPDATE agent_grants SET revoked=1 WHERE id=?", (row["grant_id"],)
            )
    return JSONResponse({}, headers=HEADERS)


@router.get("/agents")
async def list_agents(auth: Annotated[Any, Depends(owner)]):
    return JSONResponse(
        {
            "connections": await store.connections(auth.user_id),
            "scopes": SCOPES,
            "mcp_url": api_url() + "/mcp",
            "presets": access_presets(list(SCOPES)),
        },
        headers=HEADERS,
    )


class ConnectionUpdate(BaseModel):
    mode: Literal["restricted", "full", "custom"] = "custom"
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(max_length=20)


@router.patch("/agents/{grant_id}")
async def update_agent(
    grant_id: str, body: ConnectionUpdate, auth: Annotated[Any, Depends(owner)]
):
    async with store.database(True) as db:
        row = await store.one(
            db,
            "SELECT data FROM agent_grants WHERE id=? AND user_id=? AND revoked=0",
            (grant_id, auth.user_id),
        )
        if not row:
            raise HTTPException(404, "Connection not found.")
        data = json.loads(row["data"])
        ceiling = data.get("requested_scopes", data["scopes"])
        scopes = list(dict.fromkeys(body.scopes))
        if body.mode != "custom":
            scopes = access_presets(ceiling)[body.mode]
        if not scopes or set(scopes) - set(ceiling) or not body.name.strip():
            raise HTTPException(
                400,
                "Choose permissions requested by this client; reconnect for additional capabilities.",
            )
        data.update(name=body.name.strip(), scopes=scopes)
        await db.execute(
            "UPDATE agent_grants SET data=? WHERE id=?", (json.dumps(data), grant_id)
        )
    return {"success": True}


@router.delete("/agents/{grant_id}")
async def disconnect(grant_id: str, auth: Annotated[Any, Depends(owner)]):
    async with store.database(True) as db:
        await db.execute(
            "UPDATE agent_grants SET revoked=1 WHERE id=? AND user_id=?",
            (grant_id, auth.user_id),
        )
        await db.execute(
            "DELETE FROM agent_tokens WHERE grant_id=? AND user_id=?",
            (grant_id, auth.user_id),
        )
    return {"success": True}


async def authenticate(raw):
    async with store.database() as db:
        row = await store.one(
            db,
            "SELECT g.*, t.expires FROM agent_tokens t JOIN agent_grants g ON g.id=t.grant_id WHERE t.id=? AND t.kind='access' AND t.used=0 AND g.revoked=0 AND t.expires>?",
            (store.digest(raw), time.time()),
        )
    if not row or not await auth_db.get_refresh_token_by_user_id(row["user_id"]):
        return None
    return dict(id=row["id"], user_id=row["user_id"], **json.loads(row["data"]))
