import asyncio

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
