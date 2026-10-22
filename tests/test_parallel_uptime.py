import asyncio
import json
import multiprocessing
import threading
import time
from datetime import datetime

import pytest

from api import uptime
from api.auth_db import _encrypt_password
from api.metrics.user_metrics_db import UserMetricsDB


def accounts(count=6):
    return [(str(i % 2), f"monitor-{i}", "private-password") for i in range(count)]


def result(status):
    return {
        "status": status,
        "features": [
            {"name": "login", "status": "down" if status == "down" else "up"},
            {"name": "modules", "status": "up" if status == "up" else "down"},
        ],
    }


def test_encrypted_pool_deduplicates_and_requires_private_file(monkeypatch, tmp_path):
    path = tmp_path / "accounts.json"
    data = [
        {"school_id": school, "username": user, "password": _encrypt_password(password)}
        for school, user, password in accounts(4)
    ]
    data.append(dict(data[0]))
    data.append({**data[1], "enabled": False})
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setenv("LANIS_UPTIME_ACCOUNTS_FILE", str(path))
    assert len(uptime._monitor_accounts()) == 4
    assert uptime.uptime_is_configured()
    path.chmod(0o644)
    assert uptime._monitor_accounts() == []
    path.chmod(0o600)
    data[0]["password"] = "plaintext-must-not-work"
    path.write_text(json.dumps(data))
    assert uptime._monitor_accounts() == []


def test_selection_is_distinct_and_prefers_school_diversity():
    pool = accounts()
    for _ in range(20):
        chosen = uptime._choose_accounts(pool, 2)
        assert len(set(chosen)) == 2
        assert len({account[0] for account in chosen}) == 2
        extra = uptime._choose_accounts([a for a in pool if a not in chosen], 2)
        assert len(set(chosen + extra)) == 4
    assert uptime._choose_accounts([], 2) == []


@pytest.mark.parametrize(
    "states,expected",
    [
        (["up"] * 4, ("up", "healthy")),
        (["up", "up", "up", "down"], ("unknown", "isolated")),
        (["down", "down", "up", "up"], ("degraded", "confirmed")),
        (["down"] * 4, ("down", "confirmed")),
        (["degraded"] * 4, ("degraded", "confirmed")),
        (["up", "up", "unknown", "down"], ("unknown", "inconclusive")),
    ],
)
def test_classification(states, expected):
    assert uptime._classify_round([result(state) for state in states]) == expected


def test_round_really_runs_concurrently(monkeypatch):
    barrier = threading.Barrier(2, timeout=3)

    def fake_probe(account):
        barrier.wait()
        return result("up")

    monkeypatch.setattr(uptime, "_isolated_probe", fake_probe)
    pool = accounts(2)
    results = asyncio.run(
        uptime._probe_round(pool, {a[:2]: str(i) for i, a in enumerate(pool)})
    )
    assert len(results) == 2
    assert len({r["account"] for r in results}) == 2


@pytest.mark.parametrize("pool_size", [0, 1, 2, 3, 6])
def test_cycle_expands_only_once_and_persists_one_sample(
    monkeypatch, tmp_path, pool_size
):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        monkeypatch.setattr(uptime, "_monitor_accounts", lambda: accounts(pool_size))
        monkeypatch.setattr(
            uptime, "_schedule_discord_notification", lambda check: None
        )
        rounds = []

        async def fake_round(selected, labels):
            rounds.append(selected)
            return [{"account": labels[a[:2]], **result("down")} for a in selected]

        async def no_wait(seconds):
            assert seconds == 15

        monkeypatch.setattr(uptime, "_probe_round", fake_round)
        monkeypatch.setattr(uptime.asyncio, "sleep", no_wait)
        check = await uptime._run_uptime_cycle()
        stored = await database.get_uptime_checks()
        assert len(stored) == 1
        assert stored[0]["monitoring"] == check["monitoring"]
        assert "private-password" not in json.dumps(stored)
        assert "monitor-" not in json.dumps(stored)
        if pool_size < 2:
            assert not rounds
            assert check["is_available"] is None
            counts = await database.get_uptime_summary(
                datetime.fromisoformat("2000-01-01")
            )
            assert counts["failed_checks"] == 0
        else:
            assert len(rounds) == 2
            assert len(rounds[0]) == 2
            assert len(rounds[1]) == min(pool_size, 4)
            assert set(rounds[0]).issubset(rounds[1])
            assert len(rounds[1]) == len(set(rounds[1]))

    asyncio.run(scenario())


def test_shared_cycle_survives_cancelled_waiter(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def cycle(scheduled):
            calls.append(scheduled)
            entered.set()
            await release.wait()
            return {"status": "up"}

        monkeypatch.setattr(uptime, "_uptime_cycle_task", None)
        monkeypatch.setattr(uptime, "_run_uptime_cycle", cycle)
        first = asyncio.create_task(uptime.run_uptime_check(scheduled=True))
        await entered.wait()
        second = asyncio.create_task(uptime.run_uptime_check())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == {"status": "up"}
        assert calls == [True]

    asyncio.run(scenario())


def test_recovery_requires_two_scheduled_cycles_and_survives_unknown(
    monkeypatch, tmp_path
):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        monkeypatch.setattr(uptime, "_monitor_accounts", lambda: accounts(4))
        monkeypatch.setattr(
            uptime, "_schedule_discord_notification", lambda check: None
        )
        state = "down"

        async def fake_round(selected, labels):
            return [{"account": labels[a[:2]], **result(state)} for a in selected]

        async def no_wait(seconds):
            pass

        monkeypatch.setattr(uptime, "_probe_round", fake_round)
        monkeypatch.setattr(uptime.asyncio, "sleep", no_wait)
        assert (await uptime._run_uptime_cycle(True))["status"] == "down"
        state = "unknown"
        assert (await uptime._run_uptime_cycle(True))["status"] == "unknown"
        state = "up"
        assert (await uptime._run_uptime_cycle(False))["status"] == "down"
        first = await uptime._run_uptime_cycle(True)
        assert first["status"] == "down"
        assert first["monitoring"]["recovery_streak"] == 1
        second = await uptime._run_uptime_cycle(True)
        assert second["status"] == "up"

    asyncio.run(scenario())


def _child_hangs(connection, credentials):
    time.sleep(60)


def _child_succeeds(connection, credentials):
    connection.send(result("up"))
    connection.close()


@pytest.mark.parametrize(
    "target,expected", [(_child_hangs, "unknown"), (_child_succeeds, "up")]
)
def test_isolated_worker_is_reaped(monkeypatch, target, expected):
    monkeypatch.setattr(uptime, "_probe_child", target)
    monkeypatch.setenv("LANIS_UPTIME_PROBE_DEADLINE_SECONDS", "5")
    children = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    check = uptime._isolated_probe(accounts(1)[0])
    assert check["status"] == expected
    assert time.monotonic() - started < 12
    assert {child.pid for child in multiprocessing.active_children()} == children


@pytest.mark.parametrize(
    "first_state,classification,round_count",
    [("up", "healthy", 1), ("down", "transient", 2)],
)
def test_success_and_transient_keep_diagnostics_without_false_alerts(
    monkeypatch, tmp_path, first_state, classification, round_count
):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        monkeypatch.setattr(uptime, "_monitor_accounts", lambda: accounts(4))
        alerts = []
        monkeypatch.setattr(uptime, "_schedule_discord_notification", alerts.append)
        calls = []

        async def fake_round(selected, labels):
            calls.append(selected)
            return [
                {
                    "account": labels[a[:2]],
                    **result(first_state if len(calls) == 1 else "up"),
                }
                for a in selected
            ]

        async def no_wait(seconds):
            pass

        monkeypatch.setattr(uptime, "_probe_round", fake_round)
        monkeypatch.setattr(uptime.asyncio, "sleep", no_wait)
        check = await uptime._run_uptime_cycle(True)
        assert check["status"] == "up"
        assert check["monitoring"]["classification"] == classification
        assert len(check["monitoring"]["rounds"]) == round_count
        assert len(await database.get_uptime_checks()) == 1
        assert all(alert["status"] == "up" for alert in alerts)

    asyncio.run(scenario())


def test_two_failures_establish_degradation_even_with_missing_evidence():
    assert uptime._classify_round(
        [result(s) for s in ["down", "down", "unknown", "up"]]
    ) == ("degraded", "confirmed")
    assert uptime._classify_round([]) == ("unknown", "inconclusive")


def test_monitoring_diagnostics_are_not_in_public_projection():
    from api import public_status

    projected = public_status._check(
        {
            "status": "degraded",
            "features": [],
            "monitoring": {"rounds": [{"account": "private-account"}]},
        },
        datetime.fromisoformat("2026-10-22"),
    )
    assert "monitoring" not in projected
    assert "private-account" not in json.dumps(projected)


def test_encrypted_provisioning_command_does_not_overwrite(monkeypatch, tmp_path):
    import io
    import sys

    from api import uptime_accounts

    path = tmp_path / "pool.json"
    payload = json.dumps(
        [{"school_id": s, "username": u, "password": p} for s, u, p in accounts(2)]
    )
    monkeypatch.setattr(sys, "argv", ["uptime_accounts", str(path)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    uptime_accounts.main()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "private-password" not in path.read_text()
    original = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    with pytest.raises(SystemExit):
        uptime_accounts.main()
    assert path.read_bytes() == original
