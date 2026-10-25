import asyncio
import json
import multiprocessing
import threading
import time
from datetime import datetime

import pytest

from api import uptime
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
        (["up", "up", "up", "down"], ("degraded", "isolated")),
        (["down", "down", "up", "up"], ("degraded", "confirmed")),
        (["down"] * 4, ("down", "confirmed")),
        (["degraded"] * 4, ("degraded", "confirmed")),
        (["up", "up", "unknown", "down"], ("degraded", "isolated")),
    ],
)
def test_classification(states, expected):
    assert uptime._classify_round([result(state) for state in states]) == expected


def test_round_really_runs_concurrently(monkeypatch):
    barrier = threading.Barrier(2, timeout=3)

    def fake_probe(account, stopped=None):
        barrier.wait()
        return result("up")

    async def credentials(account):
        return account

    monkeypatch.setattr(uptime, "_registered_credentials", credentials)
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


@pytest.mark.parametrize("mixed", [False, True])
def test_cycle_preserves_only_unanimous_maintenance_reason(monkeypatch, tmp_path, mixed):
    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        monkeypatch.setattr(uptime, "_monitor_accounts", lambda: accounts(4))
        monkeypatch.setattr(uptime, "_schedule_discord_notification", lambda check: None)

        async def fake_round(selected, labels):
            return [
                {"account": labels[account[:2]], **result("down"),
                 "error": "timeout" if mixed and index == 0 else "maintenance"}
                for index, account in enumerate(selected)
            ]

        async def no_wait(seconds):
            pass

        monkeypatch.setattr(uptime, "_probe_round", fake_round)
        monkeypatch.setattr(uptime.asyncio, "sleep", no_wait)
        check = await uptime._run_uptime_cycle()
        expected = "confirmed" if mixed else "maintenance"
        assert check["status"] == "down"
        assert check["error"] == expected
        assert (await database.get_uptime_checks())[0]["error"] == expected

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


@pytest.mark.parametrize(
    "states,schools,expected,confidence,scope",
    [
        (["down", "up", "up", "up"], [1, 2, 1, 2], 4, "limited", "account"),
        (["down", "unknown", "up", "up"], [1, 2, 1, 2], 4, "limited", "account"),
        (["down", "down", "up", "up"], [1, 1, 2, 2], 4, "confirmed", "school"),
        (
            ["down", "down", "up", "up"],
            [1, 2, 1, 2],
            4,
            "confirmed",
            "multiple_schools",
        ),
        (
            ["down", "down", "unknown", "up"],
            [1, 2, 1, 2],
            4,
            "limited",
            "multiple_schools",
        ),
        (["down", "up"], [1, 2], 4, "limited", "account"),
        (["up", "up"], [1, 2], 2, "confirmed", "sampled_accounts"),
        (["up", "unknown"], [1, 2], 2, "inconclusive", "unknown"),
    ],
)
def test_confidence_and_scope_are_separate_from_availability(
    states, schools, expected, confidence, scope
):
    results = [
        {"account": f"Account {i}", "school_group": school, **result(state)}
        for i, (state, school) in enumerate(zip(states, schools))
    ]
    evidence = uptime._round_evidence(results, results[:1], expected)
    assert evidence["confidence"] == confidence
    assert evidence["scope"] == scope
    assert evidence["expected_accounts"] == expected
    assert evidence["sampled_accounts"] == len(states)
    assert evidence["completed_accounts"] == sum(s != "unknown" for s in states)
    assert evidence["repeated_failures"] == int(states[0] == "down")


def test_isolated_failure_counts_towards_uptime_and_scoped_incident(
    monkeypatch, tmp_path
):
    from datetime import timedelta

    from api import public_status

    async def scenario():
        database = UserMetricsDB(tmp_path / "metrics.db")
        monkeypatch.setattr(uptime, "user_metrics_db", database)
        monkeypatch.setattr(uptime, "_monitor_accounts", lambda: accounts(4))
        monkeypatch.setattr(uptime, "get_uptime_interval_seconds", lambda: 300)
        clock = [uptime._utcnow().replace(microsecond=0) - timedelta(seconds=600)]
        monkeypatch.setattr(uptime, "_utcnow", lambda: clock[0])
        alerts = []
        monkeypatch.setattr(uptime, "_schedule_discord_notification", alerts.append)
        state = "up"
        first_account = []

        async def fake_round(selected, labels):
            if not first_account:
                first_account.append(selected[0])
            observations = []
            for account in selected:
                observation = result(state if account == first_account[0] else "up")
                if observation["status"] == "down":
                    observation["features"][1]["status"] = "skipped"
                observations.append(
                    {
                        "account": labels[account[:2]],
                        "school_group": 1 if account[0] == "0" else 2,
                        **observation,
                    }
                )
            return observations

        # Make the initial pair stable so the same account is tested twice.
        monkeypatch.setattr(
            uptime, "_choose_accounts", lambda pool, count: pool[:count]
        )

        async def no_wait(seconds):
            pass

        monkeypatch.setattr(uptime, "_probe_round", fake_round)
        monkeypatch.setattr(uptime.asyncio, "sleep", no_wait)
        await uptime._run_uptime_cycle(True)
        clock[0] += timedelta(seconds=300)
        state = "down"
        check = await uptime._run_uptime_cycle(True)
        assert check["status"] == "degraded"
        assert check["is_available"] is False
        assert check["monitoring"]["confidence"] == "limited"
        assert check["monitoring"]["scope"] == "account"
        assert check["monitoring"]["repeated_failures"] == 1
        assert check["features"][0]["status"] == "degraded"
        assert alerts[-1]["status"] == "degraded"
        clock[0] += timedelta(seconds=300)
        public = await public_status._build_public_status()
        assert public["summary"]["available_checks"] == 1
        assert public["summary"]["failed_checks"] == 1
        assert public["summary"]["uptime_percent"] == 50.0
        assert public["current"]["confidence"] == "limited"
        assert public["current"]["scope"] == "account"
        assert public["current"]["features"][0]["status"] == "degraded"
        assert public["incidents"][0]["affected_features"] == ["login"]
        assert public["incidents"][0]["scope"] == "account"
        assert public["incidents"][0]["confidence"] == "limited"
        assert "Account 1" not in json.dumps(public)
        assert "monitor-" not in json.dumps(public)

    asyncio.run(scenario())


def test_public_evidence_projection_rejects_private_or_invalid_values():
    from api import public_status

    projected = public_status._check(
        {
            "status": "degraded",
            "monitoring": {
                "confidence": "private-name",
                "scope": "private-school",
                "rounds": [{"account": "private-account"}],
            },
        },
        datetime.fromisoformat("2026-10-22"),
    )
    assert "confidence" not in projected
    assert "scope" not in projected
    assert "private" not in json.dumps(projected)


def test_scoped_discord_alert_explains_limited_confidence(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    def post(url, **kwargs):
        calls.append(kwargs["json"]["content"])
        return Response()

    monkeypatch.setattr(uptime.requests, "post", post)
    uptime._send_discord_alert(
        "https://discord.example/test",
        {
            "status": "degraded",
            "monitoring": {
                "confidence": "limited",
                "scope": "account",
                "rounds": [{"account": "private-account"}],
            },
        },
        False,
    )
    assert "begrenzte Aussagekraft" in calls[0]
    assert "einzelnes Testkonto" in calls[0]
    assert "private-account" not in calls[0]


def test_shutdown_stops_shared_cycle_and_reaps_active_workers(monkeypatch):
    async def scenario():
        pool = accounts(2)
        baseline = {p.pid for p in multiprocessing.active_children()}
        after_probe = []
        monkeypatch.setattr(uptime, "_uptime_stopping", False)
        monkeypatch.setattr(uptime, "_uptime_cycle_task", None)

        async def credentials(account):
            return account

        monkeypatch.setattr(uptime, "_registered_credentials", credentials)
        monkeypatch.setattr(uptime, "_probe_child", _child_hangs)
        monkeypatch.setenv("LANIS_UPTIME_PROBE_DEADLINE_SECONDS", "60")

        async def cycle(scheduled):
            await uptime._probe_round(pool, {a[:2]: str(i) for i, a in enumerate(pool)})
            after_probe.append("late notification")

        monkeypatch.setattr(uptime, "_run_uptime_cycle", cycle)
        waiter = asyncio.create_task(uptime.run_uptime_check())

        async def wait_for_children():
            while (
                len({p.pid for p in multiprocessing.active_children()} - baseline) < 2
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_children(), timeout=5)
        await asyncio.wait_for(
            asyncio.gather(uptime.stop_uptime_cycle(), uptime.stop_uptime_cycle()),
            timeout=5,
        )
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert {p.pid for p in multiprocessing.active_children()} == baseline
        assert not after_probe
        assert uptime._uptime_cycle_task is None
        with pytest.raises(RuntimeError, match="shutting down"):
            await uptime.run_uptime_check()
        await uptime.stop_uptime_cycle()  # Idempotent after cleanup.

    asyncio.run(scenario())


def test_shutdown_cancels_confirmation_delay_without_persisting_or_alerting(
    monkeypatch,
):
    async def scenario():
        monkeypatch.setattr(uptime, "_uptime_cycle_phase", "initial")

        async def forbid_write(*args, **kwargs):
            raise AssertionError("Cancelled cycle must not persist")

        def forbid_alert(*args, **kwargs):
            raise AssertionError("Cancelled cycle must not schedule a notification")

        monkeypatch.setattr(uptime.user_metrics_db, "record_uptime_check", forbid_write)
        monkeypatch.setattr(uptime, "_schedule_discord_notification", forbid_alert)
        monkeypatch.setattr(uptime, "_uptime_stopping", False)
        monkeypatch.setattr(uptime, "_uptime_cycle_task", None)
        monkeypatch.setattr(uptime, "_monitor_accounts", lambda: accounts(4))

        async def failed_round(selected, labels):
            return [{"account": labels[a[:2]], **result("down")} for a in selected]

        monkeypatch.setattr(uptime, "_probe_round", failed_round)
        waiter = asyncio.create_task(uptime.run_uptime_check())

        async def wait_for_retry_delay():
            while uptime._uptime_cycle_phase != "waiting":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_retry_delay(), timeout=2)
        await uptime.stop_uptime_cycle()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    asyncio.run(scenario())


def test_application_shutdown_stops_cycle_before_draining_notifications(monkeypatch):
    from api import api as application

    async def scenario():
        events = []
        for name in (
            "_dsb_scheduler_task",
            "_message_notification_task",
            "_uptime_scheduler_task",
            "_whatsapp_history_cleanup_task",
            "_file_cache_cleanup_task",
        ):
            monkeypatch.setattr(application, name, None)
        monkeypatch.setattr(application, "_whatsapp_unlink_confirmation_tasks", set())

        async def stop_cycle():
            events.append("stop cycle")

        async def drain_notifications():
            assert events == ["stop cycle"]
            events.append("drain notifications")

        async def no_op(*args, **kwargs):
            pass

        monkeypatch.setattr(application, "stop_uptime_cycle", stop_cycle)
        monkeypatch.setattr(
            application, "drain_uptime_notification_tasks", drain_notifications
        )
        monkeypatch.setattr(application.task_queue, "stop", no_op)
        monkeypatch.setattr(application.whatsapp_task_queue, "stop", no_op)
        monkeypatch.setattr(application.sessions, "shutdown", no_op)
        await application._cleanup_sessions()
        assert events == ["stop cycle", "drain notifications"]

    asyncio.run(scenario())
