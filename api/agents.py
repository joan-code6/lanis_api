"""OAuth 2.1 / PKCE authorization surface for remote LANIS MCP clients."""

import base64
import hashlib
import json
import os
import re
import secrets
import time
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import agent_store as store
from . import auth_db

router = APIRouter(tags=["Agents"])
SCOPES = {
    "schoolwork:read": {
        "title": "Unterricht & Hausaufgaben",
        "description": "Kurse, Unterrichtseinträge und Hausaufgaben lesen.",
    },
    "timetable:read": {
        "title": "Stundenplan & Vertretungen",
        "description": "Deinen Stundenplan und Vertretungen lesen.",
    },
    "calendar:read": {
        "title": "Kalender",
        "description": "Schultermine und Veranstaltungen lesen.",
    },
    "messages:read": {
        "title": "Nachrichten",
        "description": "Nachrichtenübersichten und angefragte Unterhaltungen lesen.",
    },
}
DEFAULT_SCOPES = "schoolwork:read timetable:read calendar:read"
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
    if not await store.rate_limit("register:" + request.client.host, 30, 3600):
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
    data["redirect_uris"] = [validate_redirect(uri) for uri in body.redirect_uris]
    client_id = secrets.token_urlsafe(24)
    async with store.database(True) as db:
        await store.prune(db)
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
    code_challenge: str,
    state: str,
    response_type: str = "code",
    code_challenge_method: str = "S256",
    scope: str = DEFAULT_SCOPES,
    resource: str = "",
):
    if not await store.rate_limit("authorize:" + request.client.host, 120, 3600):
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
        ui_url() + "/agents/authorize?" + urlencode({"request": request_id}),
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


@router.get("/agents/authorization/{request_id}")
async def authorization_details(request_id: str, auth: Annotated[Any, Depends(owner)]):
    async with store.database() as db:
        data = await pending(db, request_id)
    return JSONResponse(
        {
            "client_name": data["client_name"],
            "client_id": data["client_id"],
            "redirect_origin": urlsplit(data["redirect_uri"]).netloc,
            "scopes": [{"id": s, **SCOPES[s]} for s in data["scopes"]],
            "username": auth.username,
            "school_id": auth.school_id,
            "verified": False,
        },
        headers=HEADERS,
    )


class Decision(BaseModel):
    approve: bool
    scopes: list[str] = Field(default_factory=list, max_length=20)
    name: str = Field(default="", max_length=100)


@router.post("/agents/authorization/{request_id}")
async def decide(request_id: str, body: Decision, auth: Annotated[Any, Depends(owner)]):
    from .account_data import account_lifecycle_lock

    lock = await account_lifecycle_lock(auth.user_id)
    async with lock:
        # Recheck after taking the deletion lock.
        if not await auth_db.get_refresh_token_by_user_id(auth.user_id):
            raise HTTPException(401, "Log in again before approving a connection.")
        async with store.database(True) as db:
            data = await pending(db, request_id)
            scopes = list(dict.fromkeys(body.scopes))
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
    if not await store.rate_limit("token:" + request.client.host, 120, 60):
        return oauth_error(
            "slow_down", "Too many token requests. Retry in one minute.", 429
        )
    form = await request.form()
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
        if row[
            "expires"
        ] <= time.time() or not await auth_db.get_refresh_token_by_user_id(
            row["user_id"]
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
        if form.get("scope"):
            grant = await store.one(
                db, "SELECT data FROM agent_grants WHERE id=?", (row["grant_id"],)
            )
            if set(str(form["scope"]).split()) != set(
                json.loads(grant["data"])["scopes"]
            ):
                return oauth_error(
                    "invalid_scope",
                    "Permissions cannot change during refresh; reconnect to change access.",
                )
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
        },
        headers=HEADERS,
    )


class ConnectionUpdate(BaseModel):
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
        if (
            not body.scopes
            or set(body.scopes) - set(data["scopes"])
            or not body.name.strip()
        ):
            raise HTTPException(
                400,
                "Permissions can only be reduced. Reconnect to grant additional access.",
            )
        data.update(name=body.name.strip(), scopes=list(dict.fromkeys(body.scopes)))
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
