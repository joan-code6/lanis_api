"""Integration tests for the FastAPI server.

Requires a running instance of `api:app`. Configure the target URL and credentials
via environment variables:
- LANIS_API_URL (default: http://localhost:8000)
- LANIS_API_SCHOOL_ID
- LANIS_API_USERNAME
- LANIS_API_PASSWORD

Run with: pytest api-tests.py
"""

import os
from typing import Dict

import pytest
import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.environ.get("LANIS_API_URL", "http://localhost:8000").rstrip("/")
SCHOOL_ID = os.environ.get("LANIS_API_SCHOOL_ID")
USERNAME = os.environ.get("LANIS_API_USERNAME")
PASSWORD = os.environ.get("LANIS_API_PASSWORD")


@pytest.fixture(scope="session")
def base_url() -> str:
	return API_BASE_URL


@pytest.fixture(scope="session")
def require_credentials() -> Dict[str, str]:
	if not (SCHOOL_ID and USERNAME and PASSWORD):
		pytest.skip("Set LANIS_API_SCHOOL_ID, LANIS_API_USERNAME, LANIS_API_PASSWORD for login tests")
	return {"school_id": SCHOOL_ID, "username": USERNAME, "password": PASSWORD}


@pytest.fixture(scope="session")
def session_token(base_url: str, require_credentials: Dict[str, str]):
	resp = requests.post(f"{base_url}/login", json=require_credentials, timeout=15)
	resp.raise_for_status()
	body = resp.json()
	token = body["token"]
	yield token
	try:
		requests.post(f"{base_url}/logout", headers={"X-Session-Token": token}, timeout=10)
	except requests.RequestException:
		pass


def test_health_ok(base_url: str) -> None:
	resp = requests.get(f"{base_url}/health", timeout=10)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /health API Response ===")
	print(body)
	print("=============================\n")
	assert body == {"status": "ok"}


def test_login_and_encryption_ready(base_url: str, require_credentials: Dict[str, str]) -> None:
	resp = requests.post(f"{base_url}/login", json=require_credentials, timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /login API Response ===")
	print(body)
	print("=============================\n")
	assert body["token"]
	assert body["school_id"] == require_credentials["school_id"]
	assert body["username"] == require_credentials["username"]
	assert isinstance(body["encryption_ready"], bool)
	requests.post(f"{base_url}/logout", headers={"X-Session-Token": body['token']}, timeout=10)


def test_apps_requires_valid_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/apps", headers=headers, timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /apps API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True


def test_modules_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/modules", headers=headers, timeout=15)
	resp.raise_for_status()
	data = resp.json()
	print("\n=== /modules API Response ===")
	print(data)
	print("=============================\n")
	assert data.get("success") is True
	assert isinstance(data.get("modules"), list)


def test_invalid_token_rejected(base_url: str) -> None:
	resp = requests.get(f"{base_url}/apps", headers={"X-Session-Token": "invalid"}, timeout=10)
	print("\n=== /apps with invalid token Response ===")
	print(resp.text)
	print("=============================\n")
	assert resp.status_code == 401


def test_multiple_sessions_independent(base_url: str, require_credentials: Dict[str, str]) -> None:
	resp1 = requests.post(f"{base_url}/login", json=require_credentials, timeout=15)
	resp2 = requests.post(f"{base_url}/login", json=require_credentials, timeout=15)
	resp1.raise_for_status()
	resp2.raise_for_status()
	body1 = resp1.json()
	body2 = resp2.json()
	print("\n=== First /login API Response ===")
	print(body1)
	print("=============================\n")
	print("\n=== Second /login API Response ===")
	print(body2)
	print("=============================\n")
	assert body1["token"] != body2["token"]
	try:
		headers1 = {"X-Session-Token": body1["token"]}
		headers2 = {"X-Session-Token": body2["token"]}
		apps1 = requests.get(f"{base_url}/apps", headers=headers1, timeout=15)
		apps2 = requests.get(f"{base_url}/apps", headers=headers2, timeout=15)
		apps1.raise_for_status()
		apps2.raise_for_status()
		body_apps1 = apps1.json()
		body_apps2 = apps2.json()
		print("\n=== First /apps API Response ===")
		print(body_apps1)
		print("=============================\n")
		print("\n=== Second /apps API Response ===")
		print(body_apps2)
		print("=============================\n")
		assert body_apps1.get("success") is True
		assert body_apps2.get("success") is True
	finally:
		requests.post(f"{base_url}/logout", headers={"X-Session-Token": body1["token"]}, timeout=10)
		requests.post(f"{base_url}/logout", headers={"X-Session-Token": body2["token"]}, timeout=10)
