"""Real OAuth and MCP transport for reviewers without school sessions or effects."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_agents import (
    HEAD,
    client,
    environment,  # noqa: F401 -- imported pytest fixture
    exchange,
    request_grant,
)

from api import agent_mcp, agents, auth_db
from api import api as api_module
from api.demo import ACCESS_TOKEN, sample_data
from api.demo.agents import DEMO_USER_ID


@pytest.mark.usefixtures("environment")
def test_demo_oauth_mcp_scopes_refresh_and_revocation(monkeypatch):
    async def scenario():
        # The MCP transport manager supports one lifespan per instance. Isolate
        # this transport from the existing real-account integration test.
        from starlette.routing import Route

        monkeypatch.setattr(agent_mcp.mcp, "_session_manager", None)
        transport = agent_mcp.mcp.streamable_http_app()
        monkeypatch.setattr(
            api_module.app.router,
            "routes",
            [
                Route("/mcp", endpoint=transport, methods=["GET", "POST", "DELETE"])
                if getattr(route, "path", None) == "/mcp"
                else route
                for route in api_module.app.router.routes
            ],
        )
        api_module.app.dependency_overrides.pop(agents.owner)
        monkeypatch.setitem(HEAD, "X-Session-Token", ACCESS_TOKEN)
        monkeypatch.setitem(HEAD, "X-LANIS-Analytics-Consent", "account-v2")
        forbidden = AsyncMock(side_effect=AssertionError("Real school work attempted"))
        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", forbidden
        )
        monkeypatch.setattr(auth_db, "get_refresh_token_by_user_id", forbidden)
        monkeypatch.setattr(api_module.task_queue, "add_task", forbidden)
        monkeypatch.setattr(api_module.user_metrics_db, "record_activity", forbidden)
        from api import analytics_store

        monkeypatch.setattr(analytics_store, "record_request", forbidden)
        monkeypatch.setattr(analytics_store, "save_diagnostic", forbidden)
        async with agent_mcp.mcp.session_manager.run(), client() as c:
            cid, code, _ = await request_grant(c, scopes=" ".join(agents.SCOPES))
            assert (
                await exchange(c, cid, code, code_verifier="x" * 64)
            ).status_code == 400
            response = await exchange(c, cid, code)
            assert response.status_code == 200, response.text
            tokens = response.json()
            grant = await agents.authenticate(tokens["access_token"])
            assert grant["user_id"] == DEMO_USER_ID
            headers = {
                "Authorization": "Bearer " + tokens["access_token"],
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            }

            async def rpc(method, params=None):
                response = await c.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params or {},
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()["result"]

            listed = await rpc("tools/list")
            available = {t["name"] for t in listed["tools"]}
            assert available == set(agent_mcp.TOOL_SCOPES)
            calls = {
                "lanis_connection": {},
                "lanis_courses": {},
                "lanis_modules": {},
                "lanis_profile": {},
                "lanis_course": {"course_id": "b2"},
                "lanis_timetable": {},
                "lanis_substitutions": {},
                "lanis_dsb_substitutions": {},
                "lanis_calendar": {"year": 2026},
                "lanis_calendar_views": {},
                "lanis_calendar_event": {"event_id": "ev1"},
                "lanis_messages": {},
                "lanis_message": {"conversation_id": "uq-1"},
                "lanis_open_message": {"conversation_id": "uq-1"},
                "lanis_recipients": {"query": "Neumann"},
                "lanis_grades": {"course_id": "b2"},
                "lanis_attendance": {},
                "lanis_study_groups": {},
                "lanis_exams": {},
                "lanis_video_rooms": {},
                "lanis_files": {},
                "lanis_file": {"file_id": 101},
                "lanis_search_files": {"query": "Mathematik"},
                "lanis_distributions": {},
                "lanis_distributed_file": {"url": "/dateiverteilung/file"},
                "lanis_submissions": {},
                "lanis_submission": {},
                "lanis_elections": {},
                "lanis_election": {"election_id": "demo-election"},
                "lanis_preferences": {},
                "lanis_send_message": {
                    "recipients": ["r1"],
                    "subject": "Demo",
                    "body": "Discard me",
                },
                "lanis_reply_message": {
                    "conversation_id": "uq-1",
                    "body": "Discard me",
                },
                "lanis_set_homework": {
                    "course_id": "b2",
                    "entry_id": "b2e1",
                    "done": True,
                },
                "lanis_upload_submission": {
                    "course_id": "b2",
                    "entry_id": "b2e1",
                    "upload_id": "demo",
                    "filename": "demo.txt",
                    "content_base64": "RGVtbw==",
                },
                "lanis_delete_submission_file": {
                    "course_id": "b2",
                    "entry_id": "b2e1",
                    "upload_id": "demo",
                    "file_index": "0",
                },
                "lanis_update_preferences": {
                    "updates": {"appearance": {"theme_mode": "dark"}}
                },
                "lanis_update_lesson": {
                    "date": "2026-10-28",
                    "period": "1",
                    "subject": "Demo",
                },
                "lanis_submit_election": {
                    "election_id": "demo-election",
                    "fields": {},
                    "selections": {},
                    "confirm": True,
                },
            }
            assert set(calls) == available
            before = sample_data()
            discovered_submissions = []
            for name, arguments in calls.items():
                if name == "lanis_submission":
                    arguments = {"detail_ref": discovered_submissions[1]["detail_ref"]}
                result = await rpc("tools/call", {"name": name, "arguments": arguments})
                assert not result.get("isError"), (name, result)
                assert result["structuredContent"].get("success"), (name, result)
                if name == "lanis_submissions":
                    discovered_submissions = result["structuredContent"]["submissions"]
                    assert discovered_submissions
                    for submission in discovered_submissions:
                        detail = await rpc(
                            "tools/call",
                            {
                                "name": "lanis_submission",
                                "arguments": {"detail_ref": submission["detail_ref"]},
                            },
                        )
                        assert not detail.get("isError"), detail
                        selected = detail["structuredContent"]["submission"]
                        assert selected == submission
                if name == "lanis_course":
                    assert "marks" not in result["structuredContent"]
                    assert all(
                        "attendance" not in e
                        for e in result["structuredContent"]["entries"]
                    )
                if name == "lanis_timetable":
                    assert all(
                        "homework" not in l
                        for d in result["structuredContent"]["days"]
                        for l in d["lessons"]
                    )
            assert sample_data() == before
            denied = await rpc(
                "tools/call",
                {
                    "name": "lanis_submit_election",
                    "arguments": {
                        "election_id": "demo-election",
                        "fields": {},
                        "selections": {},
                    },
                },
            )
            assert denied["structuredContent"]["error"] == "CONFIRMATION_REQUIRED"
            overview = (await c.get("/agents", headers=HEAD)).json()
            assert not any(overview["channels"].values())
            assert (
                await c.post("/agents/channels/signal/pairing", headers=HEAD, json={})
            ).status_code == 403
            refresh = await c.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": cid,
                    "refresh_token": tokens["refresh_token"],
                },
            )
            assert refresh.status_code == 200
            assert (await agents.authenticate(refresh.json()["access_token"]))[
                "user_id"
            ] == DEMO_USER_ID
            # Narrowing the grant takes effect for already issued tokens too.
            gid = overview["connections"][0]["id"]
            assert (
                await c.patch(
                    "/agents/" + gid,
                    headers=HEAD,
                    json={
                        "name": "Demo",
                        "mode": "custom",
                        "scopes": ["schoolwork:read"],
                    },
                )
            ).status_code == 200
            narrowed = await rpc("tools/list")
            assert "lanis_send_message" not in {t["name"] for t in narrowed["tools"]}
            denied = await rpc(
                "tools/call",
                {
                    "name": "lanis_send_message",
                    "arguments": calls["lanis_send_message"],
                },
            )
            assert denied.get("isError") and "PERMISSION_REQUIRED" in str(denied)
            assert (await c.delete("/agents/" + gid, headers=HEAD)).status_code == 200
            assert await agents.authenticate(tokens["access_token"]) is None
            assert await agents.authenticate(refresh.json()["access_token"]) is None
            forbidden.assert_not_called()
            async with auth_db.aiosqlite.connect(auth_db.DB_PATH) as db:
                assert (
                    await (
                        await db.execute(
                            "SELECT count(*) FROM refresh_tokens WHERE user_id=?",
                            (DEMO_USER_ID,),
                        )
                    ).fetchone()
                )[0] == 0
                assert (
                    await (
                        await db.execute(
                            "SELECT last_used FROM agent_grants WHERE id=?", (gid,)
                        )
                    ).fetchone()
                )[0] is None

    asyncio.run(scenario())
