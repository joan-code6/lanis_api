"""Hosted channels use real grants and persistence; no messaging or AI network."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
import test_agents
from test_agents import BOB, HEAD, client

environment = test_agents.environment
from api import agent_mcp
from api import agent_store as store
from api import hosted_agents as hosted
from api import hosted_store as state


async def paired(c, channel="signal", scopes=None):
    response = await c.post(
        f"/agents/channels/{channel}/pairing",
        headers=HEAD,
        json={
            "name": "My agent",
            "mode": "custom",
            "scopes": scopes or ["timetable:read"],
        },
    )
    assert response.status_code == 200, response.text
    code = response.json()["code"]
    assert await state.pair(code, channel, "+491234567890")
    row = await state.link(channel, "+491234567890")
    return row["id"], code


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(hosted, "channels", lambda: {"signal": True, "whatsapp": True})


def test_pairing_scopes_single_use_and_isolation(environment):
    async def scenario():
        async with client() as c:
            gid, code = await paired(c)
            assert not await state.pair(code, "signal", "+491234567891")
            grant = await state.grant(gid)
            assert grant["scopes"] == ["timetable:read"]
            overview = (await c.get("/agents", headers=HEAD)).json()
            assert overview["connections"][0]["channel"] == "signal"
            assert "recipient" not in overview["connections"][0]
            assert (
                await c.get(f"/agents/{gid}/schedules", headers=BOB)
            ).status_code == 404
            assert (
                await c.post(
                    f"/agents/{gid}/schedules",
                    headers=BOB,
                    json={"prompt": "test", "cron": "0 7 * * *"},
                )
            ).status_code == 404

    asyncio.run(scenario())


def test_schedules_encrypted_claimed_and_revoked(environment):
    async def scenario():
        async with client() as c:
            gid, _ = await paired(c)
            result = await c.post(
                f"/agents/{gid}/schedules",
                headers=HEAD,
                json={
                    "prompt": "Private homework task",
                    "cron": "0 7 * * 1-5",
                    "timezone": "Europe/Berlin",
                },
            )
            assert result.status_code == 200, result.text
            sid = result.json()["id"]
            async with store.database(True) as db:
                row = await store.one(
                    db, "SELECT * FROM hosted_schedules WHERE id=?", (sid,)
                )
                assert "Private homework" not in row["data"]
                await db.execute(
                    "UPDATE hosted_schedules SET next_run=? WHERE id=?",
                    (time.time() - 1, sid),
                )
            assert (await state.claim_due())["id"] == sid
            assert await state.claim_due() is None
            assert (await c.delete(f"/agents/{gid}", headers=HEAD)).status_code == 200
            assert await state.grant(gid) is None
            assert await state.schedules(gid) == []
            assert await state.link("signal", "+491234567890") is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data",
    [
        {"cron": "* * * * *"},
        {"cron": "* * * * * *"},
        {"at": "2020-01-01T10:00:00+01:00"},
        {"at": "2099-01-01T10:00:00"},
        {"cron": "0 7 * * *", "timezone": "Invalid/Zone"},
    ],
)
def test_invalid_schedules(environment, data):
    async def scenario():
        async with client() as c:
            gid, _ = await paired(c)
            response = await c.post(
                f"/agents/{gid}/schedules",
                headers=HEAD,
                json={"prompt": "task", **data},
            )
            assert response.status_code == 400, response.text

    asyncio.run(scenario())


def test_mcp_hosted_identity_rechecks_permissions(environment):
    async def scenario():
        async with client() as c:
            gid, _ = await paired(c)
            context = agent_mcp._hosted_grant.set(gid)
            try:
                assert (await agent_mcp.identity("timetable:read"))["id"] == gid
                with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
                    await agent_mcp.identity("messages:write")
                await hosted.revoke(gid)
                with pytest.raises(ValueError, match="CONNECTION_REVOKED"):
                    await agent_mcp.identity()
            finally:
                agent_mcp._hosted_grant.reset(context)

    asyncio.run(scenario())


def test_stop_bypasses_rate_limit(environment, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(hosted, "send", send)

    async def scenario():
        async with client() as c:
            gid, _ = await paired(c)
            for _ in range(20):
                await store.rate_limit("hosted:signal:+491234567890", 20, 60)
            await hosted.receive("signal", "+491234567890", "stop", "STOP")
            assert await state.grant(gid) is None
            send.assert_awaited_once()

    asyncio.run(scenario())


def test_turn_uses_mcp_and_persists_bounded_encrypted_history(environment, monkeypatch):
    async def fake_agent(**kwargs):
        names = {t.name for t in kwargs["tools"]}
        assert "lanis_timetable" in names
        assert "lanis_send_message" not in names
        if kwargs["user_message"] == "scheduled":
            assert "create_schedule" not in names
        else:
            assert "create_schedule" in names
        return "Hello"

    monkeypatch.setattr(hosted, "run_agent", fake_agent)

    async def scenario():
        async with client() as c:
            gid, _ = await paired(c)
            assert await hosted.turn(gid, "Hello") == "Hello"
            assert await hosted.turn(gid, "scheduled", scheduled=True) == "Hello"
            async with store.database() as db:
                row = await store.one(
                    db, "SELECT * FROM hosted_links WHERE id=?", (gid,)
                )
                assert "Hello" not in row["history"]
                assert state.unseal(row["history"])[-1]["content"] == "Hello"
            assert (
                await c.patch(
                    f"/agents/{gid}",
                    headers=HEAD,
                    json={
                        "name": "renamed",
                        "scopes": ["timetable:read"],
                        "mode": "custom",
                    },
                )
            ).status_code == 200
            async with store.database() as db:
                assert (
                    await store.one(
                        db, "SELECT history FROM hosted_links WHERE id=?", (gid,)
                    )
                )["history"] is None

    asyncio.run(scenario())


def test_signal_filters_groups_and_syncs():
    from api.signal import incoming

    envelope = {
        "sourceNumber": "+491234567890",
        "timestamp": 123,
        "dataMessage": {"message": "Hello"},
    }
    assert incoming({"envelope": envelope}) == ("+491234567890", "123", "Hello")
    envelope["dataMessage"]["groupInfo"] = {"groupId": "group"}
    assert incoming({"envelope": envelope}) is None
    assert incoming({"envelope": {"syncMessage": {}}}) is None


def test_whatsapp_dispatch_uses_shared_harness(environment, monkeypatch):
    from api import api as app
    from api.whatsapp import IncomingWhatsAppMessage

    receiver = AsyncMock()
    monkeypatch.setattr(hosted, "receive", receiver)
    incoming = IncomingWhatsAppMessage("unique-id", "491234567890", "hello")
    asyncio.run(app._process_whatsapp_message(incoming))
    receiver.assert_awaited_once_with("whatsapp", "491234567890", "unique-id", "hello")


def test_school_writes_require_single_use_confirmation(environment, monkeypatch):
    import re
    from types import SimpleNamespace

    from mcp.types import TextContent

    write = SimpleNamespace(
        name="lanis_set_homework",
        description="Set homework",
        inputSchema={"type": "object"},
        annotations=SimpleNamespace(readOnlyHint=False),
    )
    monkeypatch.setattr(
        agent_mcp, "available_tools", AsyncMock(return_value=([write], {}))
    )
    execute = AsyncMock(
        return_value=[TextContent(type="text", text='{"success":true}')]
    )
    monkeypatch.setattr(agent_mcp.mcp, "call_tool", execute)

    async def model(**kwargs):
        tool = next(t for t in kwargs["tools"] if t.name == "lanis_set_homework")
        result = await tool.handler(
            {"course_id": "math", "entry_id": "1", "done": True}
        )
        assert result["prepared"] and not result["executed"]
        return "Änderung vorbereitet."

    monkeypatch.setattr(hosted, "run_agent", model)

    async def scenario():
        async with client() as c:
            gid, _ = await paired(c, scopes=["schoolwork:write"])
            response = await hosted.turn(gid, "Mark my homework done")
            execute.assert_not_awaited()
            code = re.search(r"BESTÄTIGEN ([A-F0-9]{6})", response)[1]
            assert "success" in await hosted.confirm(gid, code)
            execute.assert_awaited_once()
            assert "ungültig" in await hosted.confirm(gid, code)
            execute.assert_awaited_once()

    asyncio.run(scenario())


def test_scheduled_turn_excludes_writes_and_schedule_tools(environment, monkeypatch):
    from types import SimpleNamespace

    tools = [
        SimpleNamespace(
            name=name,
            description=name,
            inputSchema={},
            annotations=SimpleNamespace(readOnlyHint=read),
        )
        for name, read in [("lanis_timetable", True), ("lanis_set_homework", False)]
    ]
    monkeypatch.setattr(
        agent_mcp, "available_tools", AsyncMock(return_value=(tools, {}))
    )

    async def model(**kwargs):
        assert [t.name for t in kwargs["tools"]] == ["lanis_timetable"]
        return "Read only"

    monkeypatch.setattr(hosted, "run_agent", model)

    async def scenario():
        async with client() as c:
            gid, _ = await paired(c, scopes=["schoolwork:write", "timetable:read"])
            assert await hosted.turn(gid, "task", scheduled=True) == "Read only"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "channel,old_window,expected",
    [("signal", False, "delivered"), ("whatsapp", True, "delivery_window_closed")],
)
def test_scheduler_records_delivery_and_closes_one_time_tasks(
    environment, monkeypatch, channel, old_window, expected
):
    from datetime import datetime, timezone

    turn = AsyncMock(return_value="scheduled reply")
    send = AsyncMock()
    monkeypatch.setattr(hosted, "turn", turn)
    monkeypatch.setattr(hosted, "send", send)

    async def scenario():
        async with client() as c:
            gid, _ = await paired(c, channel=channel)
            task = await state.add_schedule(
                gid,
                {
                    "prompt": "task",
                    "at": datetime.fromtimestamp(
                        time.time() + 600, timezone.utc
                    ).isoformat(),
                },
            )
            async with store.database(True) as db:
                await db.execute(
                    "UPDATE hosted_schedules SET next_run=? WHERE id=?",
                    (time.time() - 10, task["id"]),
                )
                if old_window:
                    await db.execute(
                        "UPDATE hosted_links SET last_inbound_at=0 WHERE id=?", (gid,)
                    )
            worker = asyncio.create_task(hosted.scheduler())
            try:
                for _ in range(100):
                    rows = await state.schedules(gid)
                    if rows[0]["last_status"]:
                        break
                    await asyncio.sleep(0.01)
                assert rows[0]["last_status"] == expected
                assert not rows[0]["enabled"]
                if old_window:
                    turn.assert_not_awaited()
                    send.assert_not_awaited()
                else:
                    send.assert_awaited_once()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_account_export_and_deletion_include_hosted_state(environment):
    from api import account_data, auth_db

    async def scenario():
        async with client() as c:
            gid, _ = await paired(c)
            await state.add_schedule(
                gid, {"prompt": "Private task", "cron": "0 7 * * *"}
            )
            export = await account_data.export_hosted_agents("5201:alice")
            assert export["connections"][0]["schedules"][0]["prompt"] == "Private task"
            await auth_db.delete_user_data("5201:alice")
            for table in [
                "hosted_links",
                "hosted_pairings",
                "hosted_schedules",
                "hosted_actions",
                "hosted_runs",
            ]:
                async with store.database() as db:
                    assert (await store.one(db, f"SELECT count(*) AS n FROM {table}"))[
                        "n"
                    ] == 0

    asyncio.run(scenario())


def test_signal_env_enables_bundled_bridge(monkeypatch):
    from api import signal

    monkeypatch.delenv("SIGNAL_BRIDGE_URL", raising=False)
    monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
    assert not signal.configured()
    monkeypatch.setenv("SIGNAL_BOT_NUMBER", "+491234567890")
    assert signal.configured()
    assert signal.config()[0] == "http://127.0.0.1:8081"
    monkeypatch.setenv("SIGNAL_BRIDGE_URL", "http://signal-bridge:8080")
    assert signal.configured()
    assert signal.config()[0] == "http://signal-bridge:8080"


@pytest.mark.parametrize("phone", [None, "+491234567890"])
def test_signal_sender_identity_survives_phone_privacy_changes(phone):
    from api.signal import incoming

    sender = "d24bcf3d-f9bb-4357-9c83-9afbe2b92493"
    payload = {
        "envelope": {
            "source": sender,
            "sourceUuid": sender,
            "sourceNumber": phone,
            "timestamp": 123,
            "dataMessage": {"message": "LANIS ABCD-1234"},
        }
    }
    assert incoming(payload) == (sender, "123", "LANIS ABCD-1234")


@pytest.mark.parametrize("source", ["not-an-account", "group.invalid", "", None])
def test_signal_rejects_invalid_sender_identifiers(source):
    from api.signal import incoming

    assert (
        incoming(
            {
                "envelope": {
                    "source": source,
                    "timestamp": 123,
                    "dataMessage": {"message": "hello"},
                }
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"envelope": []},
        {"envelope": {"dataMessage": []}},
        {"envelope": {"dataMessage": ["invalid"]}},
    ],
)
def test_signal_ignores_malformed_envelopes(payload):
    from api.signal import incoming

    assert incoming(payload) is None


@pytest.mark.parametrize(
    "body",
    [
        [{"timestamp": "123"}],
        {"timestamp": "123"},
        [{"timestamp": "123", "errors": {}}],
        [{"timestamp": "123", "errors": {"recipients": []}}],
    ],
)
def test_signal_accepts_confirmed_send_responses(monkeypatch, body):
    from unittest.mock import Mock

    from api import signal

    monkeypatch.setattr(
        signal, "config", lambda: ("http://localhost:8081", "+491234567890")
    )
    response = Mock(is_redirect=False)
    response.json.return_value = body
    post = Mock(return_value=response)
    monkeypatch.setattr(signal.requests, "post", post)
    asyncio.run(signal.send_text("+491234567891", "Hello"))
    post.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        [],
        ["invalid"],
        {},
        [
            {
                "timestamp": "123",
                "errors": {"recipients": [{"reason": "untrusted identity"}]},
            }
        ],
        [
            {"timestamp": "123"},
            {"timestamp": "124", "errors": {"recipients": [{"reason": "failed"}]}},
        ],
    ],
)
def test_signal_rejects_failed_or_invalid_send_responses(monkeypatch, body):
    from unittest.mock import Mock

    from api import signal

    monkeypatch.setattr(
        signal, "config", lambda: ("http://localhost:8081", "+491234567890")
    )
    response = Mock(is_redirect=False)
    response.json.return_value = body
    monkeypatch.setattr(signal.requests, "post", Mock(return_value=response))
    with pytest.raises(RuntimeError):
        asyncio.run(signal.send_text("+491234567891", "Hello"))
    response.close.assert_called_once()


def test_signal_successful_turn_sends_only_the_reply(environment, monkeypatch):
    from unittest.mock import Mock

    from api import signal

    monkeypatch.setattr(
        signal, "config", lambda: ("http://localhost:8081", "+491234567891")
    )
    response = Mock(is_redirect=False)
    response.json.return_value = [{"timestamp": "123"}]
    post = Mock(return_value=response)
    monkeypatch.setattr(signal.requests, "post", post)
    monkeypatch.setattr(
        hosted, "run_agent", AsyncMock(return_value="Hey! Wie kann ich dir helfen?")
    )

    async def scenario():
        async with client() as c:
            await paired(c)
            await hosted.receive("signal", "+491234567890", "greeting-send-test", "Hey")
            post.assert_called_once()
            assert (
                post.call_args.kwargs["json"]["message"]
                == "Hey! Wie kann ich dir helfen?"
            )

    asyncio.run(scenario())
