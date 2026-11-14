"""Offline transport and persistence regressions for the shared-IP monitor."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import BaseAdapter

from api import public_status, uptime
from api.metrics.user_metrics_db import UserMetricsDB


class ScriptedAdapter(BaseAdapter):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request.url, kwargs.get("timeout")))
        status, headers, body = next(self.responses)
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = body.encode()
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def make_client(responses):
    session = requests.Session()
    session.trust_env = False
    adapter = ScriptedAdapter(responses)
    session.mount("https://", adapter)
    return SimpleNamespace(session=session), adapter


@pytest.mark.parametrize("phase", ["preflight", "login", "modules", "redirect"])
def test_rate_limit_survives_swallowed_sdk_errors_and_stops_fallbacks(
    monkeypatch, phase
):
    ok = (200, {}, "ok")
    throttle = (429, {"Retry-After": "1200"}, "private diagnostics")
    script = {
        "preflight": [throttle],
        "login": [ok, throttle],
        "modules": [ok, ok, throttle],
        "redirect": [(302, {"Location": "/next"}, ""), throttle],
    }[phase]
    client, adapter = make_client(script)
    client.close = client.session.close

    def login(*args):
        try:
            client.session.get("https://sph.example/login")
        except requests.RequestException:
            # Model SDK retry/fallback code swallowing the original HTTP error.
            try:
                client.session.get("https://sph.example/fallback")
            except requests.RequestException:
                pass
            return {"success": False}
        return {"success": True}

    def apps():
        try:
            client.session.get("https://sph.example/apps")
        except requests.RequestException:
            return {"success": False}
        return {"success": True, "data": {"entrys": [{}]}}

    client.login, client.get_apps = login, apps
    monkeypatch.setattr(uptime, "SchulportalHessenAPI", lambda: client)
    check = uptime._probe_portal(("school", "user", "secret"))
    assert check["status"] == "unknown"
    assert check["is_available"] is None
    assert check["error"] == "rate_limited"
    assert check["retry_after_seconds"] == 1200
    assert len(adapter.calls) == len(script)
    assert all(f["status"] == "unknown" for f in check["features"])
    assert "secret" not in str(check) and "private diagnostics" not in str(check)


@pytest.mark.parametrize(
    "status,headers,error",
    [
        (403, {}, "access_denied"),
        (401, {}, "access_denied"),
        (503, {"Retry-After": "600"}, "rate_limited"),
    ],
)
def test_ambiguous_access_or_retry_responses_are_not_outages(status, headers, error):
    client, adapter = make_client([(status, headers, "")])
    uptime._install_probe_transport(client)
    for _ in range(3):
        with pytest.raises(requests.RequestException):
            client.session.get("https://sph.example/", timeout=90)
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1] == uptime.get_uptime_timeout_seconds()
    assert client._uptime_observation["error"] == error


def test_request_budget_includes_redirects():
    script = [(302, {"Location": f"/next/{i}"}, "") for i in range(20)]
    client, adapter = make_client(script)
    uptime._install_probe_transport(client)
    with pytest.raises(requests.RequestException):
        client.session.get("https://sph.example/")
    assert len(adapter.calls) == uptime.UPTIME_REQUEST_BUDGET
    assert client._uptime_observation["error"] == "request_budget_exhausted"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("120", 120),
        ("garbage", 0),
        ("-1", 0),
        (None, 0),
        ("Sat, 14 Nov 2026 12:10:00 GMT", 600),
        ("Sat, 14 Nov 2026 11:00:00 GMT", 0),
    ],
)
def test_retry_after_delta_and_http_date(monkeypatch, value, expected):
    monkeypatch.setattr(
        uptime, "_utcnow", lambda: datetime.fromisoformat("2026-11-14T12:00:00")
    )
    assert uptime._retry_after_seconds(value) == expected


def test_shared_cooldown_survives_restart_excludes_downtime_and_does_not_resolve_incident(
    monkeypatch, tmp_path
):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        clock = [datetime.fromisoformat("2026-11-14T12:00:00")]
        monkeypatch.setattr(uptime, "_utcnow", lambda: clock[0])
        monkeypatch.setattr(
            uptime, "_monitor_accounts", lambda: [("1", "a", "x"), ("2", "b", "y")]
        )
        alerts, rounds = [], []
        monkeypatch.setattr(uptime, "_schedule_discord_notification", alerts.append)

        async def probe(selected, labels):
            rounds.append(selected)
            return [
                {
                    "account": "Account 1",
                    "status": "unknown",
                    "error": "rate_limited",
                    "retry_after_seconds": 1800,
                    "features": [],
                }
            ]

        monkeypatch.setattr(uptime, "_probe_round", probe)
        await database.record_uptime_check(
            {
                "checked_at": (clock[0] - timedelta(seconds=300)).isoformat(),
                "status": "down",
                "is_available": False,
            }
        )
        first = await uptime._run_uptime_when_due(True)
        assert first["status"] == "unknown"
        assert len(rounds) == 1  # No confirmation on the same IP.
        assert first["monitoring"]["pending_status"] == "down"
        assert not alerts
        assert first["monitoring"]["rate_limit_streak"] == 1
        assert datetime.fromisoformat(first["monitoring"]["next_probe_at"]) == clock[
            0
        ] + timedelta(seconds=1800)
        # Simulate a fresh worker after restart, plus repeated manual calls.
        for _ in range(3):
            monkeypatch.setattr(uptime, "_uptime_cycle_task", None)
            cached = await uptime.run_uptime_check()
            assert cached["status"] == "unknown"
        assert len(rounds) == 1
        assert len(await database.get_uptime_checks()) == 2
        clock[0] += timedelta(seconds=1800)
        second = await uptime._run_uptime_when_due(True)
        assert second["monitoring"]["rate_limit_streak"] == 2
        assert len(rounds) == 2
        history = await database.get_uptime_checks()
        assert uptime.group_uptime_incidents(history)[0]["resolved_at"] is None
        summary = await database.get_uptime_summary(
            datetime.fromisoformat("2026-11-01")
        )
        assert summary == {"checks": 3, "available_checks": 0, "failed_checks": 1}
        public = await public_status._build_public_status()
        assert public["current"]["status"] == "unknown"
        assert public["current"]["error"] == "rate_limited"
        assert public["summary"]["failed_checks"] == 1

    asyncio.run(scenario())


def test_rate_limit_backoff_grows_caps_and_resets_after_health(monkeypatch, tmp_path):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        clock = [datetime.fromisoformat("2026-11-14T12:00:00")]
        monkeypatch.setattr(uptime, "_utcnow", lambda: clock[0])
        monkeypatch.setattr(
            uptime, "_monitor_accounts", lambda: [("1", "a", "x"), ("2", "b", "y")]
        )
        monkeypatch.setattr(
            uptime, "_schedule_discord_notification", lambda check: None
        )
        limited = True
        calls = []

        async def probe(selected, labels):
            calls.append(selected)
            if limited:
                return [{"status": "unknown", "error": "rate_limited", "features": []}]
            return [
                {"account": labels[a[:2]], "status": "up", "features": []}
                for a in selected
            ]

        monkeypatch.setattr(uptime, "_probe_round", probe)
        for delay in [600, 1200, 2400, 3600, 3600]:
            check = await uptime._run_uptime_when_due(True)
            due = datetime.fromisoformat(check["monitoring"]["next_probe_at"])
            assert due == clock[0] + timedelta(seconds=delay)
            clock[0] = due
        limited = False
        check = await uptime._run_uptime_when_due(True)
        assert check["status"] == "up"
        assert "rate_limit_streak" not in check["monitoring"]
        before = len(calls)
        # Normal cadence is also enforced for manual requests.
        assert (await uptime._run_uptime_when_due(False))["status"] == "up"
        assert len(calls) == before

    asyncio.run(scenario())


def test_unconfirmed_second_account_failure_stays_unknown(monkeypatch, tmp_path):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        monkeypatch.setattr(
            uptime, "_monitor_accounts", lambda: [("1", "a", "x"), ("2", "b", "y")]
        )
        alerts, calls = [], []
        monkeypatch.setattr(uptime, "_schedule_discord_notification", alerts.append)

        async def no_wait(seconds):
            assert seconds == 60

        monkeypatch.setattr(uptime.asyncio, "sleep", no_wait)

        async def probe(selected, labels):
            calls.append(selected)
            states = ["down", "up"] if len(calls) == 1 else ["down", "down"]
            return [
                {"account": labels[a[:2]], "status": status, "features": []}
                for a, status in zip(selected, states)
            ]

        monkeypatch.setattr(uptime, "_probe_round", probe)
        check = await uptime._run_uptime_cycle(True)
        assert check["status"] == "unknown"
        assert check["monitoring"]["repeated_failures"] == 1
        assert not alerts
        assert uptime._classify_round([{"status": "down"}, {"status": "unknown"}]) == (
            "unknown",
            "inconclusive",
        )

    asyncio.run(scenario())
