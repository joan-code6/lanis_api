import asyncio

from fastapi import BackgroundTasks

from api import homepage as homepage_module


class _MetricsStub:
    async def get_homepage_adoption(self, minimum=1):
        assert minimum == 5
        return 11, 3, ["1000", "2000", "3000"]


def test_homepage_map_exposes_only_thresholded_directory_pins(monkeypatch):
    async def directory():
        return {
            "1000": {"name": "Alpha-Schule", "location": "Frankfurt"},
            "2000": {"name": "Beta-Schule", "location": "Unbekannt"},
            "3000": {"name": "Gamma-Schule", "location": "Unbekannt"},
        }

    monkeypatch.setattr(homepage_module, "user_metrics_db", _MetricsStub())
    monkeypatch.setattr(homepage_module, "get_school_directory", directory)
    monkeypatch.setattr(
        homepage_module,
        "get_cached_school_coordinates",
        lambda school_id: {
            "2000": (50.0, 8.0),
            "3000": (float("nan"), 8.0),
        }.get(school_id),
    )
    monkeypatch.setattr(
        homepage_module,
        "city_coordinates",
        lambda location: (50.1, 8.6) if location == "Frankfurt" else None,
    )
    route = next(
        route
        for route in homepage_module.router.routes
        if route.path == "/homepage/user-map"
    )
    assert route.dependant.dependencies == []
    response = asyncio.run(homepage_module.homepage_user_map(BackgroundTasks()))

    assert response["known_users"] == 11
    assert response["known_schools"] == 3
    assert response["mapped_schools"] == 2
    assert response["schools"] == [
        {
            "school_id": "1000",
            "name": "Alpha-Schule",
            "city": "Frankfurt",
            "latitude": 50.1,
            "longitude": 8.6,
        },
        {
            "school_id": "2000",
            "name": "Beta-Schule",
            "city": "Unbekannt",
            "latitude": 50.0,
            "longitude": 8.0,
        },
    ]
    assert all(
        set(school) == {"school_id", "name", "city", "latitude", "longitude"}
        for school in response["schools"]
    )


def test_homepage_map_tolerates_an_unavailable_directory(monkeypatch):
    async def directory():
        return {}

    monkeypatch.setattr(homepage_module, "user_metrics_db", _MetricsStub())
    monkeypatch.setattr(homepage_module, "get_school_directory", directory)
    monkeypatch.setattr(
        homepage_module, "get_cached_school_coordinates", lambda _school_id: None
    )
    monkeypatch.setattr(homepage_module, "city_coordinates", lambda _location: None)

    background_tasks = BackgroundTasks()
    response = asyncio.run(homepage_module.homepage_user_map(background_tasks))

    assert response["known_users"] == 11
    assert response["known_schools"] == 3
    assert response["mapped_schools"] == 0
    assert response["schools"] == []
    assert background_tasks.tasks == []


def test_homepage_map_skips_directory_fetch_without_qualifying_schools(monkeypatch):
    class EmptyMetricsStub:
        async def get_homepage_adoption(self, minimum=1):
            assert minimum == 5
            return 4, 1, []

    async def unexpected_directory_fetch():
        raise AssertionError("school directory fetched without qualifying schools")

    monkeypatch.setattr(homepage_module, "user_metrics_db", EmptyMetricsStub())
    monkeypatch.setattr(
        homepage_module, "get_school_directory", unexpected_directory_fetch
    )

    response = asyncio.run(homepage_module.homepage_user_map(BackgroundTasks()))

    assert response["known_users"] == 4
    assert response["known_schools"] == 1
    assert response["mapped_schools"] == 0
    assert response["schools"] == []
