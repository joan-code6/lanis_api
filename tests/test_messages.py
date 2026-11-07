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


def test_header_recipient_labels_are_structured_and_bounded():
    from schulportal_hessen.applets.nachrichten.api import _structured_recipients

    conversation = {
        'Empfaenger': ''.join(f'<span class="label label-info"><i></i>User {i}</span>' for i in range(1300)),
        'WeitereEmpfaenger': '<span class="label">Alle Lernenden (SuS)</span>',
    }
    _structured_recipients(conversation, limit=100)
    assert len(conversation['Empfaenger']) == 100
    assert conversation['EmpfaengerTotal'] == 1300
    assert conversation['WeitereEmpfaenger'] == [{'name': 'Alle Lernenden (SuS)'}]
