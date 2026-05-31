"""Integration tests for the FastAPI server.

Requires a running instance of `api:app`. Configure the target URL and credentials
via environment variables:
- LANIS_API_URL (default: http://localhost:8000)
- LANIS_API_SCHOOL_ID
- LANIS_API_USERNAME
- LANIS_API_PASSWORD

Optional message reply test env vars:
- LANIS_API_CONVERSATION_ID
- LANIS_API_REPLY_TEXT

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
DSB_USERNAME = os.environ.get("LANIS_DSB_USERNAME")
DSB_PASSWORD = os.environ.get("LANIS_DSB_PASSWORD")
CONVERSATION_ID = os.environ.get("LANIS_API_CONVERSATION_ID")
REPLY_TEXT = os.environ.get("LANIS_API_REPLY_TEXT", "Automated reply test")


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


@pytest.fixture(scope="session")
def require_conversation_id() -> str:
	if not CONVERSATION_ID:
		pytest.skip("Set LANIS_API_CONVERSATION_ID to test message replies")
	return CONVERSATION_ID


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
	modules = data.get("modules")
	assert isinstance(modules, list)
	for module in modules:
		assert isinstance(module.get("usable"), bool)
		assert isinstance(module.get("usage"), list)
		if module.get("usable"):
			assert len(module.get("usage")) > 0


def test_messages_headers_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/nachrichten/headers", headers=headers, timeout=20)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /nachrichten/headers API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True
	assert "conversations" in body
	conversations = body.get("conversations")
	assert isinstance(conversations, list)
	if conversations:
		conv = conversations[0]
		assert "unread" in conv
		assert "read" in conv
		assert isinstance(conv.get("read"), bool)


def test_messages_reply_with_session(
	base_url: str, session_token: str, require_conversation_id: str
) -> None:
	headers = {"X-Session-Token": session_token}
	payload = {"conversation_id": require_conversation_id, "body": REPLY_TEXT}
	resp = requests.post(
		f"{base_url}/nachrichten/reply", json=payload, headers=headers, timeout=20
	)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /nachrichten/reply API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True


def test_calendar_overview_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/kalender", headers=headers, timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /kalender API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True
	assert isinstance(body.get("categories"), list)
	assert isinstance(body.get("calendar"), dict)


def test_calendar_events_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/kalender/events", headers=headers, timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /kalender/events API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True
	assert isinstance(body.get("events"), list)


def test_vertretungsplan_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/vertretungsplan", headers=headers, timeout=20)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /vertretungsplan API Response ===")
	print(body)
	print("=============================\n")
	assert "success" in body


def test_stundenplan_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/stundenplan", headers=headers, timeout=20)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /stundenplan API Response ===")
	print(body)
	print("=============================\n")
	assert "success" in body


def test_dateispeicher_root_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/dateispeicher", headers=headers, timeout=20)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /dateispeicher API Response ===")
	print(body)
	print("=============================\n")
	assert "success" in body


def test_dateispeicher_search_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(
		f"{base_url}/dateispeicher/search?q=test", headers=headers, timeout=20
	)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /dateispeicher/search API Response ===")
	print(body)
	print("=============================\n")
	assert "success" in body


def test_lerngruppen_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	resp = requests.get(f"{base_url}/lerngruppen", headers=headers, timeout=20)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /lerngruppen API Response ===")
	print(body)
	print("=============================\n")
	assert "success" in body


def test_meinunterricht_course_details_with_session(base_url: str, session_token: str) -> None:
	headers = {"X-Session-Token": session_token}
	overview_resp = requests.get(f"{base_url}/meinunterricht", headers=headers, timeout=20)
	overview_resp.raise_for_status()
	overview = overview_resp.json()
	assert overview.get("success") is True
	entries = overview.get("entries", [])
	if not entries:
		pytest.skip("No Mein Unterricht entries available for details endpoint test")

	course_id = entries[0].get("book_id")
	if not course_id:
		pytest.skip("No usable course id found in Mein Unterricht overview")

	resp = requests.get(
		f"{base_url}/meinunterricht/course/{course_id}/details",
		headers=headers,
		timeout=20,
	)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /meinunterricht/course/{course_id}/details API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True
	assert str(body.get("course_id")) == str(course_id)
	assert isinstance(body.get("grades"), list)
	assert isinstance(body.get("exams"), list)
	assert isinstance(body.get("upcoming_exams"), list)
	assert isinstance(body.get("attendance_summary"), dict)
	assert isinstance(body.get("additional_sections"), list)


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


def test_school_list_get_all(base_url: str) -> None:
	"""Test fetching all schools"""
	resp = requests.get(f"{base_url}/school-list", timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /school-list API Response ===")
	print(f"Success: {body.get('success')}")
	print(f"Districts count: {len(body.get('districts', []))}")
	if body.get('districts'):
		first_district = body['districts'][0]
		print(f"Sample district: {first_district.get('name')} (ID: {first_district.get('id')})")
		print(f"Schools in first district: {len(first_district.get('schools', []))}")
	print("=============================\n")
	assert body.get("success") is True
	assert isinstance(body.get("districts"), list)
	assert len(body.get("districts", [])) > 0
	# Verify structure of first district
	district = body["districts"][0]
	assert "id" in district
	assert "name" in district
	assert "schools" in district
	assert isinstance(district["schools"], list)
	# Verify structure of first school
	if district["schools"]:
		school = district["schools"][0]
		assert "id" in school
		assert "name" in school
		assert "location" in school


def test_school_list_get_by_district(base_url: str) -> None:
	"""Test fetching schools for a specific district"""
	# First get all districts to find a valid ID
	all_schools = requests.get(f"{base_url}/school-list", timeout=15)
	all_schools.raise_for_status()
	all_data = all_schools.json()
	
	if not all_data.get("districts"):
		pytest.skip("No districts available for testing")
	
	district_id = all_data["districts"][0]["id"]
	resp = requests.get(f"{base_url}/school-list/district/{district_id}", timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /school-list/district/{district_id} API Response ===")
	print(f"Success: {body.get('success')}")
	if body.get('district'):
		print(f"District: {body['district'].get('name')} (ID: {body['district'].get('id')})")
		print(f"Schools count: {len(body['district'].get('schools', []))}")
	print("=============================\n")
	assert body.get("success") is True
	assert "district" in body
	assert body["district"]["id"] == district_id
	assert isinstance(body["district"]["schools"], list)


def test_school_list_search_by_name(base_url: str) -> None:
	"""Test searching for schools by name"""
	resp = requests.get(f"{base_url}/school-list/search?q=Goethe", timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /school-list/search?q=Goethe API Response ===")
	print(f"Success: {body.get('success')}")
	print(f"Query: {body.get('query')}")
	print(f"Results count: {body.get('count')}")
	if body.get('results'):
		for i, result in enumerate(body['results'][:3]):
			print(f"  {i+1}. {result['school'].get('name')} in {result['district_name']}")
	print("=============================\n")
	assert body.get("success") is True
	assert body.get("query") == "Goethe"
	assert isinstance(body.get("results"), list)
	assert body.get("count") >= 0
	# Should find at least one school with "Goethe" in the name
	assert body.get("count") > 0
	# Verify structure of first result
	if body["results"]:
		result = body["results"][0]
		assert "district_id" in result
		assert "district_name" in result
		assert "school" in result
		assert "id" in result["school"]
		assert "name" in result["school"]
		assert "location" in result["school"]


def test_school_list_search_no_results(base_url: str) -> None:
	"""Test searching for schools with no results"""
	resp = requests.get(f"{base_url}/school-list/search?q=NonexistentSchoolXYZ123", timeout=15)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /school-list/search with no results API Response ===")


def test_dsb_login_and_plan_urls(base_url: str, session_token: str) -> None:
	if not (DSB_USERNAME and DSB_PASSWORD):
		pytest.skip("Set LANIS_DSB_USERNAME and LANIS_DSB_PASSWORD for DSB tests")

	headers = {"X-Session-Token": session_token}
	login_resp = requests.post(
		f"{base_url}/dsb/login",
		headers=headers,
		json={"username": DSB_USERNAME, "password": DSB_PASSWORD},
		timeout=15,
	)
	login_resp.raise_for_status()
	login_body = login_resp.json()
	print("\n=== /dsb/login API Response ===")
	print(login_body)
	print("=============================\n")
	assert login_body.get("success") is True

	urls_resp = requests.post(
		f"{base_url}/dsb/plan-urls",
		headers=headers,
		json={"username": DSB_USERNAME, "password": DSB_PASSWORD},
		timeout=15,
	)
	urls_resp.raise_for_status()
	urls_body = urls_resp.json()
	print("\n=== /dsb/plan-urls API Response ===")
	print(urls_body)
	print("=============================\n")
	assert urls_body.get("success") is True
	assert isinstance(urls_body.get("plan_urls"), list)


def test_dsb_plan(base_url: str, session_token: str) -> None:
	if not (DSB_USERNAME and DSB_PASSWORD):
		pytest.skip("Set LANIS_DSB_USERNAME and LANIS_DSB_PASSWORD for DSB tests")

	headers = {"X-Session-Token": session_token}
	resp = requests.post(
		f"{base_url}/dsb/plan",
		headers=headers,
		json={
			"username": DSB_USERNAME,
			"password": DSB_PASSWORD,
			"plan_index": 0,
		},
		timeout=15,
	)
	resp.raise_for_status()
	body = resp.json()
	print("\n=== /dsb/plan API Response ===")
	print(body)
	print("=============================\n")
	assert body.get("success") is True
	assert isinstance(body.get("tables"), list)
	print(f"Success: {body.get('success')}")
	print(f"Results count: {body.get('count')}")
	print("=============================\n")
	assert body.get("success") is True
	assert body.get("count") == 0
	assert body.get("results") == []
