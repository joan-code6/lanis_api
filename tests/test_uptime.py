import asyncio
from pathlib import Path

from api import uptime
from api.metrics.user_metrics_db import UserMetricsDB


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def close(self):
        pass


class FakeSession:
    def __init__(self, module_status: int = 200):
        self.module_status = module_status

    def request(self, method, url, **kwargs):
        return FakeResponse(200)

    def get(self, url, **kwargs):
        return FakeResponse(self.module_status)


class FakeClient:
    module_status = 200
    login_arguments = None

    def __init__(self):
        self.session = FakeSession(self.module_status)

    def login(self, school_id, username, password):
        self.login_arguments = (school_id, username, password)
        type(self).login_arguments = self.login_arguments
        return {"success": True}

    def get_apps(self):
        return {"success": True, "data": {"entrys": [{"name": "Kalender"}]}}

    def get_available_modules(self, apps_result):
        return [
            {"name": "Kalender", "url": "https://start.example/kalender"},
            {"name": "Vertretungsplan", "url": "https://start.example/vertretung"},
        ]

    def close(self):
        pass


def configure_monitor(monkeypatch):
    monkeypatch.setenv("LANIS_UPTIME_SCHOOL_ID", "5201")
    monkeypatch.setenv("LANIS_UPTIME_USERNAME", "monitor-user")
    monkeypatch.setenv("LANIS_UPTIME_PASSWORD", "monitor-password")


def test_probe_runs_login_and_opens_all_modules(monkeypatch):
    configure_monitor(monkeypatch)
    monkeypatch.setattr(uptime, "SchulportalHessenAPI", FakeClient)

    result = uptime._probe_portal()

    assert result["status"] == "up"
    assert result["is_available"] is True
    assert [feature["status"] for feature in result["features"]] == ["up", "up"]
    assert result["features"][1]["module_count"] == 2
    assert result["features"][1]["opened_count"] == 2
    assert FakeClient.login_arguments == ("5201", "monitor-user", "monitor-password")


def test_probe_reports_module_open_failure_without_exposing_credentials(monkeypatch):
    configure_monitor(monkeypatch)
    FakeClient.module_status = 503
    monkeypatch.setattr(uptime, "SchulportalHessenAPI", FakeClient)

    try:
        result = uptime._probe_portal()
    finally:
        FakeClient.module_status = 200

    assert result["status"] == "degraded"
    assert result["features"][0]["status"] == "up"
    assert result["features"][1]["status"] == "down"
    assert result["features"][1]["error"] == "module_open_failed"
    assert "monitor-password" not in str(result)


def test_probe_reports_missing_credentials_without_attempting_login(monkeypatch):
    monkeypatch.delenv("LANIS_UPTIME_SCHOOL_ID", raising=False)
    monkeypatch.delenv("LANIS_UPTIME_USERNAME", raising=False)
    monkeypatch.delenv("LANIS_UPTIME_PASSWORD", raising=False)
    monkeypatch.delenv("LANIS_API_SCHOOL_ID", raising=False)
    monkeypatch.delenv("LANIS_API_USERNAME", raising=False)
    monkeypatch.delenv("LANIS_API_PASSWORD", raising=False)

    result = uptime._probe_portal()

    assert result["status"] == "not_configured"
    assert result["is_available"] is None
    assert [feature["status"] for feature in result["features"]] == [
        "not_configured",
        "skipped",
    ]


def test_uptime_checks_are_persisted_and_returned_newest_first(tmp_path: Path):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        await database.record_uptime_check(
            {
                "checked_at": "2026-09-03T10:00:00",
                "url": "https://portal.example/",
                "status": "up",
                "is_available": True,
                "status_code": None,
                "latency_ms": 120,
                "error": None,
                "features": [{"name": "login", "status": "up"}],
            }
        )
        await database.record_uptime_check(
            {
                "checked_at": "2026-09-03T10:05:00",
                "url": "https://portal.example/",
                "status": "down",
                "is_available": False,
                "status_code": None,
                "latency_ms": 15000,
                "error": "timeout",
            }
        )

        checks = await database.get_uptime_checks()
        assert [check["checked_at"] for check in checks] == [
            "2026-09-03T10:05:00",
            "2026-09-03T10:00:00",
        ]
        assert checks[0]["is_available"] is False
        assert checks[1]["is_available"] is True
        assert checks[1]["features"][0]["name"] == "login"

    asyncio.run(scenario())
