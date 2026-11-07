from datetime import datetime, timezone
from api.public_status import _check


def test_account_evidence_is_allowlisted_and_labels_are_local():
    row = {"status": "down", "monitoring": {"rounds": [
        {"name": "initial", "results": [{"account": "private-name", "school_group": 123,
          "username": "secret", "error": "private diagnostic", "status": "down",
          "features": [{"name": "login", "status": "down", "url": "secret"}]}]},
        {"name": "confirmation", "results": [{"account": "private-name", "status": "up"},
          {"account": "other-private-name", "status": "up"}]}]}}
    result = _check(row, datetime.now(timezone.utc))
    rounds = result["account_checks"]
    assert rounds[0]["accounts"][0]["label"] == rounds[1]["accounts"][0]["label"] == "Testkonto 1"
    assert rounds[1]["accounts"][1]["label"] == "Testkonto 2"
    assert rounds[0]["accounts"][0]["status"] == "down"
    assert "private" not in str(result) and "secret" not in str(result)
    assert "school_group" not in str(result)


def test_legacy_observation_has_no_fabricated_account_checks():
    assert "account_checks" not in _check({"status": "down"}, datetime.now(timezone.utc))
