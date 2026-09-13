from datetime import datetime, timezone

from api import school_locations


def test_geocoded_coordinates_are_shared_through_persistent_cache(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        school_locations, "_SCHOOL_GEOCODE_DB_PATH", tmp_path / "locations.db"
    )
    monkeypatch.setattr(school_locations, "_school_geocode_cache", {})
    monkeypatch.setattr(school_locations, "_wait_for_geocode_slot", lambda: None)

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

    school_locations._wait_for_geocode_slot()
    school_locations._wait_for_geocode_slot()

    assert waits
    assert waits[-1] >= 0.9


def test_background_population_is_bounded_and_skips_cached_schools(monkeypatch):
    monkeypatch.setattr(school_locations, "_MAX_BACKGROUND_GEOCODES", 2)
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

    school_locations.populate_school_coordinates(
        [
            ("cached", "Cached", "Town"),
            ("one", "One", "Town"),
            ("two", "Two", "Town"),
            ("three", "Three", "Town"),
        ]
    )

    assert geocoded == ["one", "two"]
