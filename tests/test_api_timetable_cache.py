import asyncio
from datetime import date

from api import api as api_module
from api.api import AuthManager, AuthSession


def test_user_cache_invalidation_removes_only_that_users_entries():
    async def scenario():
        manager = AuthManager()
        await manager.set_cache("user-a", "/stundenplan", {"value": "a"})
        await manager.set_cache("user-a", "/meinunterricht", {"value": "a"})
        await manager.set_cache("user-b", "/stundenplan", {"value": "b"})

        await manager.invalidate_user_cache("user-a")

        assert await manager.get_cached("user-a", "/stundenplan") is None
        assert await manager.get_cached("user-a", "/meinunterricht") is None
        assert await manager.get_cached("user-b", "/stundenplan") == {"value": "b"}

    asyncio.run(scenario())


def test_endpoint_cache_invalidation_rejects_stale_writes():
    async def scenario():
        manager = AuthManager()
        await manager.set_cache("user-a", "/nachrichten/headers", {"value": "old"})
        await manager.set_cache(
            "user-a", "/nachrichten/conversation", {"value": "conversation"}
        )
        await manager.set_cache("user-a", "/stundenplan", {"value": "plan"})

        old_version = await manager.get_cache_version("user-a", "/nachrichten/headers")
        await manager.invalidate_endpoint_cache("user-a", "/nachrichten/headers")
        new_version = await manager.get_cache_version("user-a", "/nachrichten/headers")

        assert new_version == old_version + 1
        assert await manager.get_cached("user-a", "/nachrichten/headers") is None
        assert await manager.get_cached("user-a", "/nachrichten/conversation") == {
            "value": "conversation"
        }
        assert await manager.get_cached("user-a", "/stundenplan") == {"value": "plan"}
        assert not await manager.set_cache_if_current_version(
            "user-a", "/nachrichten/headers", {"value": "stale"}, "", old_version
        )
        assert await manager.set_cache_if_current_version(
            "user-a", "/nachrichten/headers", {"value": "fresh"}, "", new_version
        )
        assert await manager.get_cached("user-a", "/nachrichten/headers") == {
            "value": "fresh"
        }

    asyncio.run(scenario())


def test_user_cache_invalidation_rejects_fetches_started_before_version_registration():
    async def scenario():
        manager = AuthManager()
        old_version = await manager.get_cache_version("user-a", "/nachrichten/headers")

        await manager.invalidate_user_cache("user-a")

        assert (
            await manager.get_cache_version("user-a", "/nachrichten/headers")
            == old_version + 1
        )
        assert not await manager.set_cache_if_current_version(
            "user-a", "/nachrichten/headers", {"value": "stale"}, "", old_version
        )

    asyncio.run(scenario())


def test_timetable_does_not_cache_a_failed_course_overview(monkeypatch):
    class FakeClient:
        def stundenplan_get_plan(self):
            return {"success": True, "plan_for_all": [], "plan_for_own": []}

        def meinunterricht_get_overview(self):
            return {"success": False, "error": "temporary failure"}

    class FakeSessions:
        def __init__(self):
            self.cached = []

        async def get_cached(self, user_id, endpoint, params=""):
            return None

        async def set_cache(
            self, user_id, endpoint, data, params="", is_long_term=False
        ):
            self.cached.append((user_id, endpoint, data))

    fake_sessions = FakeSessions()
    monkeypatch.setattr(api_module, "sessions", fake_sessions)
    auth = AuthSession(
        client=FakeClient(), user_id="user-a", school_id="school", username="user"
    )

    result = asyncio.run(api_module.get_stundenplan(auth=auth))

    assert result["success"] is True
    assert [endpoint for _, endpoint, _ in fake_sessions.cached] == ["/stundenplan"]


def test_vertretungsplan_options_keep_profile_cache_long_term(monkeypatch):
    class FakeClient:
        def benutzer_get_data(self):
            return {"success": True, "data": {"klasse": "10a"}}

    class FakeSessions:
        def __init__(self):
            self.cache_writes = []

        async def get_cached(self, _user_id, endpoint, _params=""):
            if endpoint == "/vertretungsplan":
                return {"success": True, "days": []}
            return None

        async def set_cache(
            self, _user_id, endpoint, _data, _params="", is_long_term=False
        ):
            self.cache_writes.append((endpoint, is_long_term))

    fake_sessions = FakeSessions()
    monkeypatch.setattr(api_module, "sessions", fake_sessions)
    auth = AuthSession(
        client=FakeClient(), user_id="user-a", school_id="school", username="user"
    )

    result = asyncio.run(
        api_module.get_vertretungsplan_notification_options(auth=auth)
    )

    assert result["success"] is True
    assert fake_sessions.cache_writes == [("/benutzer", True)]


def test_timetable_cache_is_keyed_by_the_current_week(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.timetable_calls = 0

        def stundenplan_get_plan(self):
            self.timetable_calls += 1
            return {"success": True, "plan_for_all": [], "plan_for_own": []}

        def meinunterricht_get_overview(self):
            return {"success": True, "entries": []}

    class FakeSessions:
        def __init__(self):
            self.cache = {}

        async def get_cached(self, user_id, endpoint, params=""):
            return self.cache.get((user_id, endpoint, params))

        async def set_cache(
            self, user_id, endpoint, data, params="", is_long_term=False
        ):
            self.cache[(user_id, endpoint, params)] = data

    fake_client = FakeClient()
    fake_sessions = FakeSessions()
    auth = AuthSession(
        client=fake_client, user_id="user-a", school_id="school", username="user"
    )
    current_week = date(2026, 8, 24)
    monkeypatch.setattr(api_module, "sessions", fake_sessions)

    async def no_custom_lessons(_user_id):
        return []

    monkeypatch.setattr(api_module, "get_custom_lessons", no_custom_lessons)
    monkeypatch.setattr(api_module, "current_timetable_monday", lambda: current_week)

    async def scenario():
        await api_module.get_stundenplan(auth=auth)
        current_week = date(2026, 8, 31)
        monkeypatch.setattr(
            api_module, "current_timetable_monday", lambda: current_week
        )
        await api_module.get_stundenplan(auth=auth)

    asyncio.run(scenario())

    assert fake_client.timetable_calls == 2


def test_timetable_exposes_week_anchor_and_unmodified_template(monkeypatch):
    portal_lesson = {"stunde": 1, "name": "Mathematik"}

    class FakeClient:
        def stundenplan_get_plan(self):
            return {
                "success": True,
                "plan_for_all": [[portal_lesson]],
                "plan_for_own": [[dict(portal_lesson)]],
            }

        def meinunterricht_get_overview(self):
            return {"success": True, "entries": []}

    class FakeSessions:
        async def get_cached(self, _user_id, _endpoint, _params=""):
            return None

        async def set_cache(
            self, _user_id, _endpoint, _data, _params="", is_long_term=False
        ):
            return None

    async def custom_lessons(_user_id):
        return [
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Deutsch",
                "duration": 1,
            }
        ]

    monkeypatch.setattr(api_module, "sessions", FakeSessions())
    monkeypatch.setattr(api_module, "get_custom_lessons", custom_lessons)
    monkeypatch.setattr(
        api_module, "current_timetable_monday", lambda: date(2026, 8, 24)
    )
    auth = AuthSession(
        client=FakeClient(), user_id="user-a", school_id="school", username="user"
    )

    result = asyncio.run(api_module.get_stundenplan(auth=auth))

    assert result["week_start"] == "2026-08-24"
    assert result["template_plan_for_all"][0][0]["name"] == "Mathematik"
    assert result["template_plan_for_own"][0][0]["name"] == "Mathematik"
    assert result["plan_for_all"][0][0]["name"] == "Deutsch"
