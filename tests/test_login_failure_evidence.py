import pytest
import requests

from schulportal_hessen.base import SchulportalHessenAPI


@pytest.mark.parametrize(
    "failure,code",
    [
        (requests.Timeout, "timeout"),
        (requests.ConnectionError, "connection_error"),
        (requests.exceptions.SSLError, "tls_error"),
        (requests.RequestException, "request_error"),
    ],
)
def test_login_preserves_safe_transport_failure(monkeypatch, caplog, failure, code):
    def request(*args, **kwargs):
        raise failure("private-password?token=private-token")

    monkeypatch.setattr(requests.Session, "request", request)
    client = SchulportalHessenAPI()
    try:
        result = client.login("1234", "private-user", "private-password")
        assert not result["success"]
        assert result["error_code"] == code
        assert result["error_kind"] == "upstream"
        assert "private-" not in str(result) + caplog.text
        assert f"code={code}" in caplog.text
    finally:
        client.close()


@pytest.mark.parametrize(
    "status,kind,code",
    [
        (200, "unconfirmed", "login_unconfirmed"),
        (503, "upstream", "login_http_error"),
    ],
)
def test_missing_session_cookie_does_not_prove_an_outage(
    monkeypatch, status, kind, code
):
    def request(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = status
        response.url = url
        response._content = b"<html>Login</html>"
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = SchulportalHessenAPI()
    try:
        result = client.login("1234", "private-user", "private-password")
        assert result["error_kind"] == kind
        assert result["error_code"] == code
    finally:
        client.close()
