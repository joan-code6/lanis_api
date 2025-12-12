"""
FastAPI wrapper around `functions` to expose Schulportal Hessen endpoints.

Features:
- Per-API-user session tokens so multiple users with different credentials can work concurrently.
- Thin wrappers around the existing `SchulportalHessenAPI` methods.
- Uses a small in-memory session manager with an inactivity TTL to prevent leaked sessions.

Run locally:
	uvicorn api:app --reload
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from functions.base import SchulportalHessenAPI

from fastapi.middleware.cors import CORSMiddleware



SESSION_TTL_SECONDS = 60 * 60  # expire inactive sessions after 60 minutes


class LoginRequest(BaseModel):
	school_id: str = Field(..., description="Schul-ID (e.g. 1234)")
	username: str = Field(..., description="Username without school prefix")
	password: str = Field(..., description="User password")


class LoginResponse(BaseModel):
	token: str
	school_id: str
	username: str
	encryption_ready: bool


@dataclass
class SessionData:
	client: SchulportalHessenAPI
	created_at: datetime
	last_used: datetime
	username: str
	school_id: str


class SessionManager:
	"""Thread-safe in-memory token store for multiple API users."""

	def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
		self._sessions: Dict[str, SessionData] = {}
		self._lock = asyncio.Lock()
		self._ttl = ttl_seconds

	async def _purge_expired(self) -> None:
		now = datetime.utcnow()
		async with self._lock:
			expired = [token for token, data in self._sessions.items() if now - data.last_used > timedelta(seconds=self._ttl)]
			for token in expired:
				data = self._sessions.pop(token)
				data.client.close()

	async def create_session(self, school_id: str, username: str, password: str) -> str:
		token = uuid.uuid4().hex
		client = SchulportalHessenAPI()

		login_result = await run_in_threadpool(client.login, school_id, username, password)
		if not login_result.get("success"):
			client.close()
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=login_result)

		async with self._lock:
			self._sessions[token] = SessionData(
				client=client,
				created_at=datetime.utcnow(),
				last_used=datetime.utcnow(),
				username=username,
				school_id=school_id,
			)
		return token

	async def get_client(self, token: str) -> SchulportalHessenAPI:
		await self._purge_expired()
		async with self._lock:
			data = self._sessions.get(token)
			if not data:
				raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session token")
			data.last_used = datetime.utcnow()
			return data.client

	async def drop_session(self, token: str) -> None:
		async with self._lock:
			data = self._sessions.pop(token, None)
		if data:
			await run_in_threadpool(data.client.logout)
			data.client.close()

	async def shutdown(self) -> None:
		async with self._lock:
			sessions = list(self._sessions.items())
			self._sessions.clear()
		for _, data in sessions:
			await run_in_threadpool(data.client.logout)
			data.client.close()


sessions = SessionManager()
app = FastAPI(title="Schulportal Hessen API", version="0.1.0")


async def client_dependency(x_session_token: str = Header(..., alias="X-Session-Token")) -> SchulportalHessenAPI:
	return await sessions.get_client(x_session_token)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # includes X-Session-Token
)


@app.on_event("shutdown")
async def _cleanup_sessions() -> None:
	await sessions.shutdown()


@app.get("/health")
async def health() -> Dict[str, str]:
	return {"status": "ok"}


@app.post("/login", response_model=LoginResponse)
async def login_endpoint(payload: LoginRequest) -> LoginResponse:
	token = await sessions.create_session(payload.school_id, payload.username, payload.password)
	# Fetch a fresh client to read encryption state for the response
	client = await sessions.get_client(token)
	encryption_ready = bool(getattr(client, "cryptor", None) and client.cryptor.authenticated)
	return LoginResponse(
		token=token,
		school_id=payload.school_id,
		username=payload.username,
		encryption_ready=encryption_ready,
	)


@app.post("/logout")
async def logout_endpoint(x_session_token: str = Header(..., alias="X-Session-Token")) -> Dict[str, str]:
	await sessions.drop_session(x_session_token)
	return {"status": "logged_out"}


@app.get("/apps")
async def get_apps(client: SchulportalHessenAPI = Depends(client_dependency)) -> Dict[str, object]:
	return await run_in_threadpool(client.get_apps)


@app.get("/modules")
async def get_modules(client: SchulportalHessenAPI = Depends(client_dependency)) -> Dict[str, object]:
	modules = await run_in_threadpool(client.get_available_modules)
	return {"success": True, "modules": modules}


@app.get("/benutzer")
async def get_user_data(client: SchulportalHessenAPI = Depends(client_dependency)) -> Dict[str, object]:
	return await run_in_threadpool(client.benutzer_get_data)


@app.get("/nachrichten/headers")
async def get_message_headers(
	get_type: str = "All",
	last: int = 0,
	client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
	return await run_in_threadpool(client.nachrichten_get_headers, get_type, last)


@app.get("/nachrichten/{conversation_id}")
async def get_conversation(
	conversation_id: str,
	last: int = 0,
	client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
	return await run_in_threadpool(client.nachrichten_get_conversation, conversation_id, last)


@app.get("/nachrichten/search")
async def search_recipients(
	q: str,
	client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
	return await run_in_threadpool(client.nachrichten_search_recipients, q)


@app.post("/nachrichten/send")
async def send_message(
	message: Dict[str, object],
	client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
	return await run_in_threadpool(client.nachrichten_send_message, message)


@app.get("/meinunterricht")
async def meinunterricht_overview(client: SchulportalHessenAPI = Depends(client_dependency)) -> Dict[str, object]:
	return await run_in_threadpool(client.meinunterricht_get_overview)


@app.get("/meinunterricht/course/{course_id}")
async def meinunterricht_course(
	course_id: str,
	client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
	return await run_in_threadpool(client.meinunterricht_get_course, course_id)


@app.get("/meinunterricht/entry")
async def meinunterricht_entry(
	url: str,
	client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
	return await run_in_threadpool(client.meinunterricht_get_entry_details, url)


@app.get("/meinunterricht/weekly")
async def meinunterricht_weekly(client: SchulportalHessenAPI = Depends(client_dependency)) -> Dict[str, object]:
	return await run_in_threadpool(client.meinunterricht_get_weekly_view)


@app.get("/meinunterricht/submissions")
async def meinunterricht_submissions(client: SchulportalHessenAPI = Depends(client_dependency)) -> Dict[str, object]:
	return await run_in_threadpool(client.meinunterricht_get_submissions)


# Convenience alias for uvicorn CLI discovery
__all__ = ["app"]
