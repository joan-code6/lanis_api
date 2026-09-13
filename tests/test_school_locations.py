import asyncio
import sqlite3
import threading
from datetime import datetime, timezone

from fastapi import BackgroundTasks

from api import school_locations


def test_failed_directory_refresh_preserves_stale_data_and_is_throttled(
    monkeypatch,
):
    stale_directory = {"5201": {"name": "Cached School", "location": "Kassel"}}
    monkeypatch.setattr(
        school_locations,
        "_school_directory_cache",
        {"data": stale_directory, "created_at": datetime.min},
    )
    monkeypatch.setattr(school_locations, "_school_directory_failure_until", None)
    calls = []

    class Client:
        def school_list_get_all(self):
            calls.append(True)
            return {"success": False, "error": "upstream unavailable"}

        def close(self):
            return None

    monkeypatch.setattr(school_locations, "SchulportalHessenAPI", Client)

    assert asyncio.run(school_locations.get_school_directory()) == stale_directory
    assert asyncio.run(school_locations.get_school_directory()) == stale_directory
    assert len(calls) == 1
    assert school_locations._school_directory_failure_until is not None
    assert school_locations._school_directory_failure_until > datetime.now(
        timezone.utc
    ).replace(tzinfo=None)


def test_empty_successful_directory_is_cached(monkeypatch):
    monkeypatch.setattr(
        school_locations,
        "_school_directory_cache",
        {"data": None, "created_at": None},
    )
    monkeypatch.setattr(school_locations, "_school_directory_failure_until", None)
    calls = []

    class Client:
        def school_list_get_all(self):
            calls.append(True)
            return {"success": True, "districts": []}

        def close(self):
            return None

    monkeypatch.setattr(school_locations, "SchulportalHessenAPI", Client)

    assert asyncio.run(school_locations.get_school_directory()) == {}
    assert asyncio.run(school_locations.get_school_directory()) == {}
    assert len(calls) == 1


def test_geocoded_coordinates_are_shared_through_persistent_cache(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        school_locations, "_SCHOOL_GEOCODE_DB_PATH", tmp_path / "locations.db"
    )
    monkeypatch.setattr(school_locations, "_school_geocode_cache", {})
    monkeypatch.setattr(school_locations, "_wait_for_geocode_slot", lambda: True)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"lat": "50.25", "lon": "8.75"}]

    monkeypatch.setattr(school_locations.requests, "get", lambda *args, **kwargs: Response())

    assert school_locations.geocode_school("5201", "Testschule", "Teststadt") == (
        50.25,
        8.75,
    )
    monkeypatch.setattr(school_locations, "_school_geocode_cache", {})
    assert school_locations.get_cached_school_coordinates("5201") == (50.25, 8.75)


def test_active_persistent_claim_prevents_duplicate_worker_lookup(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        school_locations, "_SCHOOL_GEOCODE_DB_PATH", tmp_path / "locations.db"
    )
    monkeypatch.setattr(school_locations, "_school_geocode_cache", {})
    with school_locations._connect_coordinate_db() as database:
        database.execute(
            "INSERT INTO school_coordinates (school_id, claim_until) VALUES (?, ?)",
            ("5201", datetime.max.replace(tzinfo=timezone.utc).isoformat()),
        )
        database.commit()

    def unexpected_request(*args, **kwargs):
        raise AssertionError("duplicate geocoding request")

    monkeypatch.setattr(school_locations.requests, "get", unexpected_request)
    assert school_locations.geocode_school("5201", "Testschule", "Teststadt") is None


def test_nominatim_slots_are_rate_limited_through_persistent_state(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        school_locations, "_SCHOOL_GEOCODE_DB_PATH", tmp_path / "locations.db"
    )
    waits = []
    monkeypatch.setattr(school_locations.time, "sleep", waits.append)

    assert school_locations._wait_for_geocode_slot()
    assert school_locations._wait_for_geocode_slot()

    assert waits
    assert waits[-1] >= 0.9


def test_background_population_is_bounded_and_skips_cached_schools(monkeypatch):
    monkeypatch.setattr(school_locations, "_MAX_BACKGROUND_GEOCODES", 2)
    monkeypatch.setattr(school_locations, "_school_population_lock", threading.Lock())
    monkeypatch.setattr(
        school_locations,
        "_cached_school_coordinates",
        lambda school_id: (school_id == "cached", None),
    )
    geocoded = []
    monkeypatch.setattr(
        school_locations,
        "geocode_school",
        lambda school_id, name, location: geocoded.append(school_id),
    )

    schools = [
        ("cached", "Cached", "Town"),
        ("one", "One", "Town"),
        ("two", "Two", "Town"),
        ("three", "Three", "Town"),
    ]
    tasks = BackgroundTasks()

    assert school_locations.schedule_school_coordinate_population(tasks, schools)
    assert not school_locations.schedule_school_coordinate_population(
        BackgroundTasks(), schools
    )
    asyncio.run(tasks())

    assert geocoded == ["one", "two"]


def test_sqlite_coordination_failure_skips_nominatim(monkeypatch):
    monkeypatch.setattr(school_locations, "_school_geocode_cache", {})
    monkeypatch.setattr(
        school_locations,
        "_connect_coordinate_db",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )

    def unexpected_request(*args, **kwargs):
        raise AssertionError("uncoordinated Nominatim request")

    monkeypatch.setattr(school_locations.requests, "get", unexpected_request)

    assert school_locations.geocode_school("5201", "Testschule", "Teststadt") is None


def test_rate_slot_failure_skips_nominatim(monkeypatch):
    monkeypatch.setattr(school_locations, "_school_geocode_cache", {})
    monkeypatch.setattr(school_locations, "_claim_school_geocode", lambda _id: True)
    monkeypatch.setattr(school_locations, "_wait_for_geocode_slot", lambda: False)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("uncoordinated Nominatim request")

    monkeypatch.setattr(school_locations.requests, "get", unexpected_request)

    assert school_locations.geocode_school("5201", "Testschule", "Teststadt") is None
