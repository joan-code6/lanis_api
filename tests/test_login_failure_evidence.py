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


@pytest.mark.parametrize(
    "state,expected",
    [
        ("4", "login_rejected"),
        ("5", "login_rejected"),
        ("9", "login_unconfirmed"),
        ("42", "login_unconfirmed"),
    ],
)
def test_portal_state_identifies_rejected_credentials_without_using_hidden_error_text(
    monkeypatch, state, expected
):
    def request(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = (
            f'<span id="lemon_code">{state}</span><input type="password">'
            '<div class="d-none alert-danger">Static error text</div>'
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = SchulportalHessenAPI()
    try:
        result = client.login("1234", "private-user", "private-password")
        assert result["error_code"] == expected
        assert result["error_kind"] == (
            "authentication" if expected == "login_rejected" else "unconfirmed"
        )
    finally:
        client.close()


def test_login_preserves_server_routing_cookie_in_outgoing_credentials_request(
    monkeypatch,
):
    from requests.cookies import get_cookie_header

    seen = []

    def request(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = b'<span id="lemon_code">9</span>'
        if method.lower() == "get":
            self.cookies.set("sph-login-upstream", "8", domain=".hessen.de")
        else:
            cookie = get_cookie_header(
                self.cookies, requests.Request(method, url).prepare()
            )
            seen.append(
                [
                    item.strip()
                    for item in cookie.split(";")
                    if item.strip().startswith("sph-login-upstream=")
                ]
            )
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = SchulportalHessenAPI()
    try:
        client.login("1234", "private-user", "private-password")
        assert seen == [["sph-login-upstream=8"]]
    finally:
        client.close()
