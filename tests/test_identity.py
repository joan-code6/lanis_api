from api.api import _make_user_id
from api.identity import canonicalize_user_id, normalize_username


def test_case_variants_share_one_internal_user_id():
    assert _make_user_id(" 5201 ", "Bennet.Wegener") == "5201:bennet.wegener"
    assert _make_user_id("5201", "bennet.wegener") == "5201:bennet.wegener"


def test_legacy_user_id_is_canonicalized_at_the_boundary():
    assert canonicalize_user_id("5201:Bennet.wegener") == "5201:bennet.wegener"
    assert canonicalize_user_id("opaque-test-user") == "opaque-test-user"
    assert normalize_username("  Bennet.Wegener  ") == "bennet.wegener"
