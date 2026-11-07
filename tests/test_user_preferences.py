import asyncio
import json
from types import SimpleNamespace

import aiosqlite
import pytest
from pydantic import ValidationError

from api import api as api_module
from api import auth_db
from api.api import (
    AppearancePreferencesRequest,
    DashboardPreferencesRequest,
    HomeworkPreferencesRequest,
    SidebarPreferencesRequest,
    TimetablePreferencesRequest,
    UserPreferencesRequest,
    VertretungsplanPreferencesRequest,
    get_account_preferences,
    update_account_preferences,
)


@pytest.mark.parametrize("theme_color", ["coral", "blush", "moss"])
def test_appearance_preferences_accept_new_themes(theme_color: str) -> None:
    assert AppearancePreferencesRequest(theme_color=theme_color).theme_color == theme_color


def test_dashboard_preferences_limit_module_lists() -> None:
    accepted = DashboardPreferencesRequest(pinned_modules=["module"] * 50)
    assert len(accepted.pinned_modules or []) == 50
    accepted_hidden = DashboardPreferencesRequest(hidden_modules=["module"] * 50)
    assert len(accepted_hidden.hidden_modules or []) == 50

    with pytest.raises(ValidationError):
        DashboardPreferencesRequest(pinned_modules=["module"] * 51)

    with pytest.raises(ValidationError):
        DashboardPreferencesRequest(hidden_modules=["module"] * 51)


def test_user_preferences_defaults_and_partial_updates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())

    defaults, stored = asyncio.run(auth_db.get_user_preferences("5201:Student"))
    assert stored is False
    assert defaults["appearance"] == {
        "theme_mode": "system",
        "theme_color": "cyan",
    }
    assert defaults["sidebar"] == {
        "order": auth_db.DEFAULT_SIDEBAR_ORDER,
        "hidden_items": [],
        "show_feedback_button": True,
    }
    assert defaults["dashboard"]["pinned_modules"] == []
    assert defaults["dashboard"]["hidden_modules"] == []
    assert defaults["dashboard"]["show_search"] is True
    assert defaults["dashboard"]["show_subtitle"] is True
    assert defaults["dashboard"]["notifications_enabled"] is True
    assert defaults["dashboard"]["notification_show_read"] is False
    assert defaults["dashboard"]["notification_limit"] == 20
    assert defaults["homework"] == {"completed_display": "green", "hide_completed_in_overview": False}
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
        "hidden_modules": [],
        "module_order": [],
        "view_mode": "grid",
        "show_search": True,
        "show_subtitle": True,
        "notifications_enabled": True,
        "notification_messages_enabled": True,
        "notification_native_enabled": True,
        "notification_dsb_enabled": True,
        "notification_show_read": False,
        "notification_limit": 20,
    }

    loaded, stored = asyncio.run(auth_db.get_user_preferences("5201:STUDENT"))
    assert stored is True
    assert loaded == saved


@pytest.mark.parametrize("show_search,show_subtitle", [(False, True), (True, False), (False, False)])
def test_dashboard_visibility_persists_through_preferences_route(
    tmp_path, monkeypatch, show_search, show_subtitle
) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(api_module, "save_user_preferences", auth_db.save_user_preferences)
    asyncio.run(auth_db.initialize())
    auth = SimpleNamespace(user_id="5201:student")

    asyncio.run(update_account_preferences(
        UserPreferencesRequest(dashboard={
            "show_search": show_search,
            "show_subtitle": show_subtitle,
        }),
        auth,
    ))
    asyncio.run(update_account_preferences(
        UserPreferencesRequest(dashboard={"view_mode": "list"}), auth,
    ))

    loaded, stored = asyncio.run(auth_db.get_user_preferences(auth.user_id))
    assert stored is True
    assert loaded["dashboard"]["show_search"] is show_search
    assert loaded["dashboard"]["show_subtitle"] is show_subtitle
    assert loaded["dashboard"]["view_mode"] == "list"


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
            "hidden_modules": [" Kalender ", "Dateispeicher", "Dateispeicher"],
            "view_mode": "list",
        },
        vertretungsplan={"class_override": " 10 A "},
    )
    result = asyncio.run(
        update_account_preferences(payload, SimpleNamespace(user_id="5201:student"))
    )

    assert captured["updates"]["dashboard"]["pinned_modules"] == [
        "Nachrichten",
    ]
    assert captured["updates"]["dashboard"]["hidden_modules"] == [
        "Kalender",
        "Dateispeicher",
    ]
    assert captured["updates"]["vertretungsplan"]["class_override"] == "10 A"
    assert result["stored"] is True


def test_saving_hidden_modules_removes_them_from_pinned_modules(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())

    asyncio.run(
        auth_db.save_user_preferences(
            "5201:Student",
            {"dashboard": {"pinned_modules": ["Nachrichten", "Kalender"]}},
        )
    )
    saved = asyncio.run(
        auth_db.save_user_preferences(
            "5201:Student",
            {"dashboard": {"hidden_modules": ["Kalender"]}},
        )
    )

    assert saved["dashboard"]["pinned_modules"] == ["Nachrichten"]
    assert saved["dashboard"]["hidden_modules"] == ["Kalender"]


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
            order=["dashboard"] * (len(auth_db.DEFAULT_SIDEBAR_ORDER) + 11)
        )


def test_sidebar_preferences_accept_search_and_dividers() -> None:
    accepted = SidebarPreferencesRequest(
        order=["search", "dashboard", "divider-1788796958596", "messages"]
    )
    assert accepted.order == ["search", "dashboard", "divider-1788796958596", "messages"]

    with pytest.raises(ValidationError):
        SidebarPreferencesRequest(order=["divider-invalid"])


def test_sidebar_preferences_hidden_items() -> None:
    accepted = SidebarPreferencesRequest(hidden_items=["messages", "dateispeicher"])
    assert accepted.hidden_items == ["messages", "dateispeicher"]

    with pytest.raises(ValidationError):
        SidebarPreferencesRequest(hidden_items=["unknown"])


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


def test_timetable_visibility_preferences_persist_independently(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    session = SimpleNamespace(user_id="5201:student")

    defaults, _ = asyncio.run(auth_db.get_user_preferences(session.user_id))
    assert defaults["timetable"] == {
        "view_mode": "rolling", "layout_mode": "cards",
        "show_homework": True, "show_exams": True, "show_substitutions": True,
        "class_colors": {},
    }

    for patch in (
        {"show_substitutions": False},
        {"show_homework": False},
        {"show_exams": False},
        {"view_mode": "week"},
        {"layout_mode": "compact"},
        {"class_colors": {"course:math-1": "#2563EB"}},
    ):
        asyncio.run(update_account_preferences(UserPreferencesRequest(timetable=patch), session))

    loaded, stored = asyncio.run(auth_db.get_user_preferences(session.user_id))
    assert stored is True
    assert loaded["timetable"] == {
        "view_mode": "week", "layout_mode": "compact",
        "show_homework": False, "show_exams": False, "show_substitutions": False,
        "class_colors": {"course:math-1": "#2563eb"},
    }

    result = asyncio.run(update_account_preferences(
        UserPreferencesRequest(timetable={"show_exams": True}), session,
    ))
    assert result["preferences"]["timetable"] == {
        "view_mode": "week", "layout_mode": "compact",
        "show_homework": False, "show_exams": True, "show_substitutions": False,
        "class_colors": {"course:math-1": "#2563eb"},
    }

    replaced = asyncio.run(update_account_preferences(
        UserPreferencesRequest(timetable={"class_colors": {"course:physics-1": "#abcdef"}}), session,
    ))
    assert replaced["preferences"]["timetable"]["class_colors"] == {"course:physics-1": "#abcdef"}

    cleared = asyncio.run(update_account_preferences(
        UserPreferencesRequest(timetable={"class_colors": {}}), session,
    ))
    assert cleared["preferences"]["timetable"]["class_colors"] == {}


def test_timetable_class_colors_validate_keys_values_and_limit() -> None:
    accepted = TimetablePreferencesRequest(
        class_colors={"course:math-1": "#AABBCC"},
    )
    assert accepted.class_colors == {"course:math-1": "#aabbcc"}

    for invalid in ({"": "#aabbcc"}, {"course:math-1": "blue"}):
        with pytest.raises(ValidationError):
            TimetablePreferencesRequest(class_colors=invalid)

    with pytest.raises(ValidationError):
        TimetablePreferencesRequest(
            class_colors={f"course:{index}": "#aabbcc" for index in range(101)},
        )


def test_saved_sidebar_order_includes_new_defaults_without_losing_customization():
    saved_order = ["settings", "divider-1788796958596", "dashboard", "messages"]
    stored = json.dumps({"sidebar": {"order": saved_order, "hidden_items": ["messages"]}})

    preferences = auth_db._decode_user_preferences(stored, "5201:student")

    assert preferences["sidebar"]["order"] == saved_order + [
        item for item in auth_db.DEFAULT_SIDEBAR_ORDER if item not in saved_order
    ]
    assert preferences["sidebar"]["hidden_items"] == ["messages"]
    assert auth_db._decode_user_preferences(json.dumps(preferences), "5201:student") == preferences


def test_dashboard_module_order_is_accepted_and_bounded():
    order = ["Nachrichten", "Kalender"]
    assert DashboardPreferencesRequest(module_order=order).model_dump(exclude_none=True) == {"module_order": order}
    with pytest.raises(ValueError):
        DashboardPreferencesRequest(module_order=["module"] * 51)


def test_preferences_http_contract_round_trip(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    asyncio.run(auth_db.initialize())
    async def identity():
        return SimpleNamespace(user_id='5201:student')
    api_module.app.dependency_overrides[api_module.local_auth_dependency] = identity
    try:
        client = TestClient(api_module.app)
        response = client.get('/settings/preferences')
        assert response.status_code == 200, response.text
        response = client.patch('/settings/preferences', json={'appearance': {'theme_color': '#F43F5E', 'secondary_theme_color': '#047857'}, 'homework': {'hide_completed_in_overview': True}})
        assert response.status_code == 200, response.text
        saved = client.get('/settings/preferences').json()['preferences']
        assert saved['homework']['hide_completed_in_overview'] is True
        assert saved['appearance']['theme_color'] == '#f43f5e'
        assert saved['appearance']['secondary_theme_color'] == '#047857'
        response = client.patch('/settings/preferences', json={'homework': {'hide_completed_in_overview': False}})
        assert response.status_code == 200
        saved = client.get('/settings/preferences').json()['preferences']
        assert saved['homework']['hide_completed_in_overview'] is False
        assert saved['appearance']['secondary_theme_color'] == '#047857'
    finally:
        api_module.app.dependency_overrides.pop(api_module.local_auth_dependency, None)


@pytest.mark.parametrize('color', ['#abc', '#ABCDEF', 'cyan'])
def test_appearance_accepts_current_and_legacy_colors(color):
    assert AppearancePreferencesRequest(theme_color=color, secondary_theme_color=color).theme_color


@pytest.mark.parametrize('color', ['red', '#xyz', '#12345', 'url(example)'])
def test_appearance_rejects_invalid_colors(color):
    with pytest.raises(ValidationError):
        AppearancePreferencesRequest(theme_color=color)


def test_homework_orange_survives_preferences_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    asyncio.run(auth_db.initialize())
    user_id = '5201:student'
    saved = asyncio.run(auth_db.save_user_preferences(user_id, {'homework': {'completed_display': 'orange'}}))
    assert saved['homework'] == {'completed_display': 'orange', 'hide_completed_in_overview': False}
    saved = asyncio.run(auth_db.save_user_preferences(user_id, saved))
    assert saved['homework']['completed_display'] == 'orange'
    saved = asyncio.run(auth_db.save_user_preferences(user_id, {'homework': {'hide_completed_in_overview': False}}))
    assert saved['homework']['completed_display'] == 'orange'
    saved = asyncio.run(auth_db.save_user_preferences(user_id, {'homework': {'hide_completed_in_overview': True}}))
    assert saved['homework']['completed_display'] == 'hidden'
    saved = asyncio.run(auth_db.save_user_preferences(user_id, {'homework': {'hide_completed_in_overview': False}}))
    assert saved['homework']['completed_display'] == 'green'
