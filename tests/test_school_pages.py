import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace

import pytest
import requests
from fastapi import HTTPException

from api import school_pages


class ExportResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("upstream failure")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


@pytest.fixture(autouse=True)
def clear_profile_cache(monkeypatch):
    school_pages._profile_cache.clear()
    monkeypatch.setattr(school_pages, "_refresh_locks", [asyncio.Lock() for _ in range(64)])
    monkeypatch.setattr(school_pages, "_fetch_slots", asyncio.Semaphore(4))
    yield
    school_pages._profile_cache.clear()


def school_export(school_id, color):
    return {
        "Id": school_id,
        "Name": f"School {school_id}",
        "Ort": f"Town {school_id}",
        "Kurzname": "",
        "Farben": {"bg": color, "border": "#123456", "activeBG": "#654321", "text": "#fff"},
        "Logo": f"https://start.schulportal.hessen.de/exporteur.php?a=schoollogo&i={school_id}",
        "CSS": f"https://start.schulportal.hessen.de/exporteur.php?a=schoolcss&i={school_id}",
        "bgimg": {
            "xs": {
                "url": f"https://start.schulportal.hessen.de/exporteur.php?a=schoolbg&i={school_id}&s=xs",
                "px": 768,
            }
        },
        "Support": "School support instructions",
        "Hint": False,
        "LetzteAenderung": 123,
    }


def test_profiles_load_distinct_school_identity_colors_and_assets_from_sph(monkeypatch):
    calls = []

    def get(url, params, timeout):
        calls.append((url, params, timeout))
        color = "#abcdef" if params["i"] == "1001" else "#fedcba"
        return ExportResponse(school_export(params["i"], color))

    monkeypatch.setattr(school_pages.requests, "get", get)
    first = asyncio.run(school_pages._get_school_profile("1001"))
    second = asyncio.run(school_pages._get_school_profile("2002"))
    assert first["name"] == "School 1001"
    assert second["name"] == "School 2002"
    assert first["city"] == "Town 1001"
    assert first["palette"]["primary"] == "#abcdef"
    assert second["palette"]["primary"] == "#fedcba"
    assert first["palette"]["accent"] == "#654321"
    assert first["assets"]["logo"].endswith("i=1001")
    assert second["assets"]["campus"]["xs"].endswith("i=2002&s=xs")
    assert first["assets"]["campus_widths"]["xs"] == 768
    assert first["assets"]["campus"]["lg"] is None
    assert first["short_name"] == ""
    assert first["support_html"] == "School support instructions"
    assert first["last_modified"] == 123
    assert "theme_color" not in first
    assert calls[0][1] == {"a": "school", "i": "1001"}
    assert calls[0][2] == (5, 15)


def test_missing_or_unsafe_branding_is_not_replaced_with_another_school(monkeypatch):
    payload = school_export("1001", "red; background: url(bad)")
    payload["Logo"] = "https://schulportal.hessen.de.evil.example/logo.png"
    payload["CSS"] = "javascript:alert(1)"
    payload["bgimg"] = False
    monkeypatch.setattr(school_pages.requests, "get", lambda *_args, **_kwargs: ExportResponse(payload))
    result = asyncio.run(school_pages._get_school_profile("1001"))
    assert result["palette"]["primary"] is None
    assert result["assets"]["logo"] is None
    assert result["assets"]["stylesheet"] is None
    assert all(value is None for value in result["assets"]["campus"].values())


@pytest.mark.parametrize("school_id", ["bad", "１２３４", "12/34", "12345678901"])
def test_invalid_ids_do_not_contact_sph(monkeypatch, school_id):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid ID contacted SPH")

    monkeypatch.setattr(school_pages.requests, "get", unexpected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(school_pages._get_school_profile(school_id))
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "response,expected_status",
    [
        (ExportResponse(status=404), 404),
        (ExportResponse(status=503), 502),
        (ExportResponse(ValueError("HTML instead of JSON")), 502),
        (ExportResponse(school_export("2002", "#123456")), 502),
    ],
)
def test_upstream_errors_are_reported_instead_of_static_profiles(monkeypatch, response, expected_status):
    monkeypatch.setattr(school_pages.requests, "get", lambda *_args, **_kwargs: response)
    with pytest.raises(HTTPException) as error:
        asyncio.run(school_pages._get_school_profile("1001"))
    assert error.value.status_code == expected_status


def test_timeout_returns_gateway_timeout(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise requests.Timeout("connection timeout")

    monkeypatch.setattr(school_pages.requests, "get", timeout)
    with pytest.raises(HTTPException) as error:
        asyncio.run(school_pages._get_school_profile("1001"))
    assert error.value.status_code == 504


def test_concurrent_requests_share_one_fetch_and_expired_profiles_refresh(monkeypatch):
    calls = []
    now = [100.0]
    monkeypatch.setattr(school_pages, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def fetch(school_id):
        calls.append(school_id)
        time.sleep(0.01)
        return {"school_id": school_id, "name": f"revision {len(calls)}"}

    monkeypatch.setattr(school_pages, "_fetch_school_profile", fetch)

    async def scenario():
        responses = await asyncio.gather(*[
            school_pages._get_school_profile("1001") for _ in range(8)
        ])
        assert calls == ["1001"]
        responses[0]["name"] = "mutated by caller"
        cached = await school_pages._get_school_profile("1001")
        assert cached["name"] == "revision 1"
        now[0] += school_pages._CACHE_TTL + 1
        refreshed = await school_pages._get_school_profile("1001")
        assert refreshed["name"] == "revision 2"

    asyncio.run(scenario())


def test_failed_requests_are_temporarily_cached_and_then_retried(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(school_pages, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def missing(school_id):
        calls.append(school_id)
        raise HTTPException(status_code=404, detail="School not found")

    monkeypatch.setattr(school_pages, "_fetch_school_profile", missing)
    for _ in range(2):
        with pytest.raises(HTTPException):
            asyncio.run(school_pages._get_school_profile("1001"))
    assert calls == ["1001"]
    now[0] += school_pages._ERROR_CACHE_TTL + 1
    with pytest.raises(HTTPException):
        asyncio.run(school_pages._get_school_profile("1001"))
    assert calls == ["1001", "1001"]


def test_directory_lists_all_sph_schools_without_fetching_each_profile(monkeypatch):
    async def directory():
        return {
            "2002": {"name": "Zulu school", "location": "Town Z"},
            "1001": {"name": "Alpha school", "location": "Town A"},
        }

    monkeypatch.setattr(school_pages, "get_school_directory", directory)
    result = asyncio.run(school_pages.list_schools())
    assert result["schools"] == [
        {"school_id": "1001", "name": "Alpha school", "city": "Town A"},
        {"school_id": "2002", "name": "Zulu school", "city": "Town Z"},
    ]


def test_profile_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(school_pages, "_MAX_CACHE_ENTRIES", 2)
    for school_id in ["1001", "2002", "3003"]:
        school_pages._remember(school_id, school_pages._CacheEntry(100, {"school_id": school_id}))
    assert list(school_pages._profile_cache) == ["2002", "3003"]
