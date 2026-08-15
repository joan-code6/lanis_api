from schulportal_hessen.applets.nachrichten.api import (
    _extract_users,
    _normalize_recipient,
)


def test_extract_users_supports_sph_recipient_items_payload() -> None:
    users = [{"id": "l-123", "text": "Mustermann, Erika", "type": "usr"}]

    assert _extract_users({"items": users, "total_count": 1}) == users


def test_normalize_recipient_matches_public_api_contract() -> None:
    assert _normalize_recipient(
        {"id": "l-123", "text": "Mustermann, Erika", "type": "usr"}
    ) == {
        "id": "l-123",
        "name": "Mustermann, Erika",
        "username": "",
        "type": "usr",
    }
