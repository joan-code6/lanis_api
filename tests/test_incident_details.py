import asyncio
from datetime import datetime
from api import public_status, uptime


def test_incident_id_stays_at_start_through_recovery():
    checks = [
        {"checked_at": "2026-11-01T10:00:00Z", "status": "down"},
        {"checked_at": "2026-11-01T10:01:00Z", "status": "down"},
        {"checked_at": "2026-11-01T10:02:00Z", "status": "up"},
    ]
    first = uptime.group_uptime_incidents(checks[:1])[0]
    recovered = uptime.group_uptime_incidents(checks)[0]
    assert first["id"] == recovered["id"] == "20261101T100000000000Z"
    assert recovered["resolved_at"] == checks[-1]["checked_at"]


def test_detail_sanitizes_observations(monkeypatch):
    class Database:
        async def get_uptime_checks(self, **kwargs):
            return [{"checked_at": "2026-11-01T10:00:00", "status": "down",
                     "error": "secret diagnostic", "username": "secret", "features": []}]
    monkeypatch.setattr(uptime, "user_metrics_db", Database())
    detail = asyncio.run(public_status.get_public_incident("20261101T100000000000Z"))
    assert detail["id"] == "20261101T100000000000Z"
    assert "secret" not in str(detail)
    assert asyncio.run(public_status.get_public_incident("invalid")) is None


def test_detail_normalizes_legacy_held_recovery(monkeypatch):
    rows = [
        {"checked_at": "2026-11-01T10:00:00", "status": "degraded", "monitoring": {
            "classification": "isolated", "scope": "account", "observed_status": "degraded", "failed_accounts": 1}},
        {"checked_at": "2026-11-01T10:01:00", "status": "degraded", "monitoring": {
            "classification": "recovering", "observed_status": "up", "failed_accounts": 0}},
        {"checked_at": "2026-11-01T10:02:00", "status": "down"},
    ]
    class Database:
        async def get_uptime_checks(self, **kwargs):
            return list(reversed(rows))
    monkeypatch.setattr(uptime, "user_metrics_db", Database())
    detail = asyncio.run(public_status.get_public_incident("20261101T100200000000Z"))
    assert detail is not None
    assert detail["started_at"] == "2026-11-01T10:02:00Z"
    assert len(detail["observations"]) == 1


def test_discord_link_uses_public_incident_projection(monkeypatch):
    rows = [
        {"checked_at": "2026-11-01T10:00:00", "status": "degraded", "monitoring": {
            "classification": "isolated", "scope": "account", "observed_status": "degraded", "failed_accounts": 1}},
        {"checked_at": "2026-11-01T10:01:00", "status": "degraded", "monitoring": {
            "classification": "recovering", "observed_status": "up", "failed_accounts": 0}},
        {"checked_at": "2026-11-01T10:02:00", "status": "down"},
    ]
    class Database:
        async def get_uptime_checks(self, **kwargs): return list(reversed(rows))
        async def get_uptime_alert_state_details(self): return False, None
        async def get_uptime_alert_deliveries(self, **kwargs): return []
        async def set_uptime_alert_state(self, value): pass
        async def record_uptime_alert_delivery(self, *args): pass
    sent = []
    monkeypatch.setattr(uptime, "user_metrics_db", Database())
    monkeypatch.setattr(uptime, "_discord_webhook_url", lambda: "https://discord.example/test")
    monkeypatch.setattr(uptime, "_send_discord_alert", lambda url, check, recovered: sent.append(check))
    asyncio.run(uptime._notify_discord_on_transition(rows[-1]))
    assert sent[0]["incident_id"] == "20261101T100200000000Z"
    assert asyncio.run(public_status.get_public_incident(sent[0]["incident_id"])) is not None
