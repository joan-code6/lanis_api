import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest
from pydantic import ValidationError

from api import api as api_module
from api import auth_db
from api.api import (
    DashboardPreferencesRequest,
    HomeworkPreferencesRequest,
    SidebarPreferencesRequest,
    UserPreferencesRequest,
    VertretungsplanPreferencesRequest,
    get_account_preferences,
    update_account_preferences,
)


def test_dashboard_preferences_limit_pinned_modules() -> None:
    accepted = DashboardPreferencesRequest(pinned_modules=["module"] * 50)
    assert len(accepted.pinned_modules or []) == 50

    with pytest.raises(ValidationError):
        DashboardPreferencesRequest(pinned_modules=["module"] * 51)


def test_user_preferences_defaults_and_partial_updates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())

    defaults, stored = asyncio.run(auth_db.get_user_preferences("5201:Student"))
    assert stored is False
    assert defaults["appearance"] == {
        "theme_mode": "system",
        "theme_color": "cyan",
    }
    assert defaults["sidebar"] == {"order": auth_db.DEFAULT_SIDEBAR_ORDER}
    assert defaults["dashboard"]["pinned_modules"] == []
    assert defaults["homework"] == {"completed_display": "green"}
    assert defaults["vertretungsplan"] == {"class_override": ""}
    assert defaults["onboarding"]["status"] == "not_started"

    saved = asyncio.run(
        auth_db.save_user_preferences(
            "5201:Student",
            {
                "appearance": {"theme_color": "ruby"},
                "dashboard": {"pinned_modules": ["Nachrichten"]},
            },
        )
    )
    assert saved["appearance"] == {
        "theme_mode": "system",
        "theme_color": "ruby",
    }
    assert saved["dashboard"] == {
        "pinned_modules": ["Nachrichten"],
        "view_mode": "grid",
    }

    loaded, stored = asyncio.run(auth_db.get_user_preferences("5201:STUDENT"))
    assert stored is True
    assert loaded == saved


def test_user_preferences_ignore_malformed_stored_json(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(db_path))
    asyncio.run(auth_db.initialize())

    async def insert_malformed() -> None:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO user_preferences (user_id, preferences) VALUES (?, ?)",
                ("user-a", "not-json"),
            )
            await db.commit()

    asyncio.run(insert_malformed())
    loaded, stored = asyncio.run(auth_db.get_user_preferences("user-a"))
    assert stored is True
    assert loaded == auth_db.DEFAULT_USER_PREFERENCES


def test_preferences_route_cleans_module_names(monkeypatch) -> None:
    captured = {}

    async def save_preferences(user_id, updates):
        captured["user_id"] = user_id
        captured["updates"] = updates
        return updates

    monkeypatch.setattr(api_module, "save_user_preferences", save_preferences)
    payload = UserPreferencesRequest(
        dashboard={
            "pinned_modules": [" Nachrichten ", "Nachrichten", "", "Kalender"],
            "view_mode": "list",
        },
        vertretungsplan={"class_override": " 10 A "},
    )
    result = asyncio.run(
        update_account_preferences(payload, SimpleNamespace(user_id="5201:student"))
    )

    assert captured["updates"]["dashboard"]["pinned_modules"] == [
        "Nachrichten",
        "Kalender",
    ]
    assert captured["updates"]["vertretungsplan"]["class_override"] == "10 A"
    assert result["stored"] is True


def test_vertretungsplan_preferences_limit_class_override() -> None:
    with pytest.raises(ValidationError):
        VertretungsplanPreferencesRequest(class_override="x" * 101)


def test_sidebar_preferences_reject_unknown_and_oversized_orders() -> None:
    accepted = SidebarPreferencesRequest(order=["dashboard", "wahlen"])
    assert accepted.order == ["dashboard", "wahlen"]

    with pytest.raises(ValidationError):
        SidebarPreferencesRequest(order=["unknown"])

    with pytest.raises(ValidationError):
        SidebarPreferencesRequest(
            order=["dashboard"] * (len(auth_db.DEFAULT_SIDEBAR_ORDER) + 1)
        )


def test_sidebar_preferences_are_normalized_before_saving(monkeypatch) -> None:
    captured = {}

    async def save_preferences(user_id, updates):
        captured["updates"] = updates
        return updates

    monkeypatch.setattr(api_module, "save_user_preferences", save_preferences)
    payload = UserPreferencesRequest(
        sidebar={"order": ["settings", "dashboard", "settings"]}
    )

    result = asyncio.run(
        update_account_preferences(payload, SimpleNamespace(user_id="5201:student"))
    )

    expected = ["settings", "dashboard"] + [
        item
        for item in auth_db.DEFAULT_SIDEBAR_ORDER
        if item not in {"settings", "dashboard"}
    ]
    assert captured["updates"]["sidebar"]["order"] == expected
    assert result["preferences"]["sidebar"]["order"] == expected


def test_homework_preferences_accept_overview_display_choices() -> None:
    orange = HomeworkPreferencesRequest(completed_display="orange")
    green = HomeworkPreferencesRequest(completed_display="green")
    hidden = HomeworkPreferencesRequest(completed_display="hidden")

    assert orange.completed_display == "orange"
    assert green.completed_display == "green"
    assert hidden.completed_display == "hidden"


def test_homework_preferences_are_included_in_updates() -> None:
    payload = UserPreferencesRequest(
        homework={"completed_display": "hidden"},
        onboarding={"last_step": "homework"},
    )
    dumped = (
        payload.model_dump(exclude_none=True)
        if hasattr(payload, "model_dump")
        else payload.dict(exclude_none=True)
    )

    assert dumped == {
        "homework": {"completed_display": "hidden"},
        "onboarding": {"last_step": "homework"},
    }


def test_empty_vertretungsplan_preference_group_does_not_clear_override() -> None:
    payload = UserPreferencesRequest(vertretungsplan={})
    dumped = (
        payload.model_dump(exclude_none=True)
        if hasattr(payload, "model_dump")
        else payload.dict(exclude_none=True)
    )

    assert dumped == {"vertretungsplan": {}}


def test_preferences_get_route_reports_new_account(monkeypatch) -> None:
    async def get_preferences(_user_id):
        return ({"appearance": {"theme_mode": "system"}}, False)

    monkeypatch.setattr(api_module, "get_user_preferences", get_preferences)
    result = asyncio.run(
        get_account_preferences(SimpleNamespace(user_id="5201:student"))
    )

    assert result == {
        "success": True,
        "stored": False,
        "preferences": {"appearance": {"theme_mode": "system"}},
    }
