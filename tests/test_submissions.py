import io
from types import SimpleNamespace

import pytest
import requests

from schulportal_hessen.applets.mein_unterricht.submissions import (
    _decode_ref,
    _encode_ref,
    meinunterricht_download_submission_file,
    meinunterricht_get_submission,
    meinunterricht_get_submissions,
    meinunterricht_upload_files,
    parse_submission_detail,
    parse_submission_summaries,
    parse_upload_statuses,
)

BASE_URL = "https://example.invalid"

LOGIN_HTML = '<html><form><input name="user2"><input type="password" name="password"></form></html>'


def _portal_response(body: str, url: str, headers=None, status_code=200):
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    response.headers.update(headers or {})
    return response


SUMMARY_HTML = """
<div id="content">
  <div class="course-folder">
    <a href="meinunterricht.php?a=sus_view&amp;id=42"><span class="name">Mathe</span></a>
    <div class="btn-group">
      <button class="btn btn-warning"><i class="fa fa-upload"></i> Testabgabe <small>bis Freitag</small><span class="badge">1</span></button>
      <ul class="dropdown-menu"><li><a href="meinunterricht.php?a=sus_abgabe&amp;b=42&amp;e=7&amp;id=9">Öffnen</a></li></ul>
    </div>
  </div>
</div>
"""


DETAIL_HTML = """
<div id="content">
  <div class="row"><div class="col-md-12"><h1>Testabgabe</h1></div></div>
  <div class="row">
    <div class="col-md-12">
      <span class="editable">Montag, 1.9.26 8:00 ab</span>
      <b><span class="editable">Freitag, 5.9.26 23:59 spätestens</span></b>
      <i class="fa fa-check-square-o fa-fw"></i><span class="label label-success">erlaubt</span>
      <i class="fa fa-check-square-o fa-fw"></i><span class="label label-success">nicht erlaubt</span>
      <i class="fa fa-eye fa-fw"></i><span class="label label-info">Lehrkräfte</span>
      <i class="fa fa-trash-o fa-fw"></i><span class="label label-info">30.09.2026</span>
      <i class="fa fa-file fa-fw"></i><span class="label label-warning">PDF, DOCX</span>
      <i class="fa fa-file fa-fw"></i><span class="label label-warning">10 MB</span>
      <div class="alert alert-info">Bitte nur die fertige Datei abgeben.</div>
    </div>
    <div class="col-md-7">
      <form><input name="b" value="42"><input name="e" value="7"><input name="id" value="9"></form>
      <ul><li><a href="dateiverteilung.php?a=download&amp;f=123">fertig.pdf</a><small>Heute 12:00</small></li></ul>
    </div>
    <div class="col-md-5">
      <ul><li><a href="dateiverteilung.php?a=download&amp;f=456">Beispiel.pdf</a><span class="label label-info">Frau Beispiel</span></li></ul>
    </div>
  </div>
</div>
"""


TABLE_SUMMARY_HTML = """
<div id="content">
  <a href="meinunterricht.php?a=sus_abgaben&amp;halbjahr=1">Alle Abgaben</a>
  <table class="abgaben"><tbody><tr>
    <td>
      <a href="meinunterricht.php?a=sus_abgabe&amp;b=42&amp;e=7&amp;id=9">
        <i class="fa fa-upload"></i><b>Testabgabe</b>
      </a>
      <span class="label label-danger">0 Dateien</span>
      <small><i class="fa fa-clock"></i> Dienstag bis Mittwoch</small>
      <a href="meinunterricht.php?a=sus_abgabe&amp;b=42&amp;e=7&amp;id=9">
        Details
      </a>
      <span class="label label-success">Abgabe aktuell möglich</span>
    </td>
  </tr></tbody></table>
</div>
"""


def test_submission_summary_parser_returns_typed_records() -> None:
    result = parse_submission_summaries(SUMMARY_HTML, BASE_URL)

    assert len(result) == 1
    assert result[0]["title"] == "Testabgabe"
    assert result[0]["status"] == "open"
    assert result[0]["course_id"] == "42"
    assert result[0]["entry_id"] == "7"
    assert result[0]["uploaded_count"] == 1
    assert result[0]["detail_ref"] == result[0]["id"]


def test_submission_summary_parser_handles_production_table_without_navigation_rows() -> None:
    result = parse_submission_summaries(TABLE_SUMMARY_HTML, BASE_URL)

    assert len(result) == 1
    assert result[0]["title"] == "Testabgabe"
    assert result[0]["status"] == "open"
    assert result[0]["uploaded"] == "0 Dateien"
    assert result[0]["uploaded_count"] == 0
    assert result[0]["course_id"] == "42"
    assert result[0]["entry_id"] == "7"


def test_submission_detail_parser_returns_rules_and_files() -> None:
    detail = parse_submission_detail(DETAIL_HTML, BASE_URL, "detail-ref")

    assert detail["course_id"] == "42"
    assert detail["entry_id"] == "7"
    assert detail["upload_id"] == "9"
    assert detail["allows_multiple_files"] is True
    assert detail["allows_multiple_attempts"] is False
    assert detail["allowed_file_types"] == ["PDF", "DOCX"]
    assert detail["max_file_size"] == "10 MB"
    assert detail["status"] == "open"
    assert detail["uploaded_count"] == 1
    assert len(detail["own_files"]) == 1
    assert detail["own_files"][0]["index"] == "123"
    assert len(detail["public_files"]) == 1
    assert detail["public_files"][0]["person"] == "Frau Beispiel"


def test_submission_detail_parser_handles_closed_page_without_upload_form() -> None:
    source_url = "meinunterricht.php?a=sus_abgabe&b=42&e=7&id=9"
    detail_ref = _encode_ref(source_url, BASE_URL)
    detail = parse_submission_detail(
        """
        <div id="content">
          <h1>Geschlossene Abgabe</h1>
          <div class="row"><div class="col-md-12">
            <span class="editable">Freitag, 5.9.26 23:59 spätestens</span>
            <div class="alert alert-info">Die Abgabe ist geschlossen.</div>
          </div></div>
        </div>
        """,
        BASE_URL,
        detail_ref,
        source_url=source_url,
    )

    assert detail["success"] is True
    assert detail["course_id"] == "42"
    assert detail["entry_id"] == "7"
    assert detail["upload_id"] == "9"
    assert detail["status"] == "closed"
    assert detail["can_upload"] is False
    assert detail["start"] is None
    assert detail["deadline"] == "Freitag, 5.9.26 23:59"


def test_upload_status_parser_keeps_per_file_messages() -> None:
    statuses = parse_upload_statuses(
        """
        <div id="content"><ul>
          <li><b>good.pdf</b> <span class="label label-success">erfolgreich</span></li>
          <li><b>bad.exe</b> <span class="label label-danger">fehlgeschlagen</span>: Dateityp nicht erlaubt</li>
        </ul></div>
        """
    )

    assert statuses == [
        {"name": "good.pdf", "status": "erfolgreich", "message": None},
        {"name": "bad.exe", "status": "fehlgeschlagen", "message": "Dateityp nicht erlaubt"},
    ]


class _Response:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.posts = []

    def get(self, *_args, **_kwargs):
        if _kwargs.get("params", {}).get("a") == "sus_abgaben":
            return _Response(SUMMARY_HTML)
        return _Response(DETAIL_HTML)

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        return _Response(
            '<div id="content"><ul><li><b>good.pdf</b> '
            '<span class="label label-success">erfolgreich</span></li></ul></div>'
        )


class _Client:
    logged_in = True
    BASE_START_URL = BASE_URL

    def __init__(self) -> None:
        self.session = _Session()


def test_submission_methods_use_refs_and_multipart_fields() -> None:
    client = _Client()
    summary = meinunterricht_get_submissions(client)
    assert summary["success"] is True

    # The detail method receives the opaque ref, never an arbitrary browser URL.
    detail = meinunterricht_get_submission(client, summary["submissions"][0]["id"])
    assert detail["success"] is True

    uploaded = meinunterricht_upload_files(
        client,
        "42",
        "7",
        "9",
        [{"filename": "good.pdf", "stream": io.BytesIO(b"pdf"), "content_type": "application/pdf"}],
    )
    assert uploaded["success"] is True
    assert client.session.posts[0][1]["data"] == {"a": "sus_abgabe", "b": "42", "e": "7", "id": "9"}
    assert client.session.posts[0][1]["files"][0][0] == "file1"
    assert client.session.posts[0][1]["files"][0][1][1].read() == b"pdf"


@pytest.mark.parametrize("missing", ["b", "e", "id"])
def test_form_field_without_value_uses_source_identifier(missing) -> None:
    identifiers = {"b": "42", "e": "7", "id": "9"}
    html = DETAIL_HTML.replace(
        f'name="{missing}" value="{identifiers[missing]}"', f'name="{missing}"'
    )
    detail = parse_submission_detail(
        html, BASE_URL, "detail-ref",
        source_url="meinunterricht.php?a=sus_abgabe&b=42&e=7&id=9",
    )
    assert (detail["course_id"], detail["entry_id"], detail["upload_id"]) == ("42", "7", "9")
    assert detail["can_upload"] is True


@pytest.mark.parametrize("reference", ["a", "%%%%", "", "_w", "a" * 4097])
def test_invalid_file_references_return_bad_request_without_downloading(reference) -> None:
    def unexpected_download(_url):
        pytest.fail("Invalid references must not issue upstream requests")

    client = SimpleNamespace(
        logged_in=True, BASE_START_URL=BASE_URL,
        meinunterricht_download_file=unexpected_download,
    )
    result = meinunterricht_download_submission_file(client, reference)
    assert result["success"] is False
    assert result["status_code"] == 400


def test_references_round_trip_unicode_and_query_values() -> None:
    relative = "dateiverteilung.php?a=download&f=123&name=Prüfung.pdf"
    assert _decode_ref(_encode_ref(relative, BASE_URL), BASE_URL) == f"{BASE_URL}/{relative}"


def test_closed_submission_preserves_deadline_and_hides_deletion() -> None:
    import re

    html = re.sub(r"<form>.*?</form>", "", DETAIL_HTML)
    detail = parse_submission_detail(html, BASE_URL, "detail-ref")
    assert detail["start"] == "Montag, 1.9.26 8:00"
    assert detail["deadline"] == "Freitag, 5.9.26 23:59"
    assert len(detail["own_files"]) == 1
    assert detail["can_delete"] is False


def test_unlabeled_public_file_does_not_count_as_own_upload() -> None:
    detail = parse_submission_detail(
        DETAIL_HTML.replace('<span class="label label-info">Frau Beispiel</span>', ''),
        BASE_URL, "detail-ref",
    )
    assert detail["uploaded_count"] == 1
    assert detail["public_files"][0]["index"] == "456"
    assert detail["public_files"][0]["person"] is None


def test_plain_upload_success_reports_each_file(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(client.session, "post", lambda *args, **kwargs: _Response("1"))
    result = meinunterricht_upload_files(
        client, "42", "7", "9", [{"filename": "Prüfung.pdf", "content": b"pdf"}]
    )
    assert result["success"] is True
    assert result["all_succeeded"] is True
    assert result["files"] == [{"name": "Prüfung.pdf", "status": "erfolgreich", "message": None}]


def test_six_files_are_rejected_before_contacting_portal(monkeypatch) -> None:
    client = _Client()
    def unexpected_post(*args, **kwargs):
        pytest.fail("Unsupported file count must be rejected before uploading")
    monkeypatch.setattr(client.session, "post", unexpected_post)
    result = meinunterricht_upload_files(
        client, "42", "7", "9", [{"filename": f"{i}.pdf", "content": b"pdf"} for i in range(6)]
    )
    assert result["success"] is False


@pytest.mark.parametrize("kind", ["list", "detail"])
@pytest.mark.parametrize("url,html,status_code", [
    ("https://login.schulportal.hessen.de/?i=1234", "<html>Anmelden</html>", 200),
    (f"{BASE_URL}/login.php", "<html>Anmelden</html>", 200),
    (f"{BASE_URL}/meinunterricht.php", LOGIN_HTML, 200),
    (f"{BASE_URL}/meinunterricht.php", "Unauthorized", 401),
])
def test_submission_reads_reject_login_responses(monkeypatch, kind, url, html, status_code):
    client = _Client()
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: _portal_response(
        html, url, status_code=status_code,
    ))
    if kind == "list":
        result = meinunterricht_get_submissions(client)
    else:
        result = meinunterricht_get_submission(client, _encode_ref(
            "meinunterricht.php?a=sus_abgabe&b=42&e=7&id=9", BASE_URL,
        ))
    assert result["success"] is False
    assert result["status_code"] == 401
    assert "submissions" not in result
    assert "title" not in result


def test_submission_detail_allows_deletion_password_field(monkeypatch):
    client = _Client()
    html = DETAIL_HTML + '<form><input type="password" name="password"></form>'
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: _portal_response(
        html, f"{BASE_URL}/meinunterricht.php",
    ))
    result = meinunterricht_get_submission(client, _encode_ref(
        "meinunterricht.php?a=sus_abgabe&b=42&e=7&id=9", BASE_URL,
    ))
    assert result["success"] is True
    assert result["title"] == "Testabgabe"


@pytest.mark.parametrize("body,headers,url,success", [
    (LOGIN_HTML, {"Content-Type": "text/html"}, f"{BASE_URL}/dateiverteilung.php", False),
    (LOGIN_HTML, {"Content-Type": "application/octet-stream"}, f"{BASE_URL}/dateiverteilung.php", False),
    ("<html>Anmelden</html>", {}, "https://login.schulportal.hessen.de/", False),
    ("<div>" + LOGIN_HTML + "</div>", {}, f"{BASE_URL}/dateiverteilung.php", False),
    ("<html>Arbeitsblatt</html>", {"Content-Type": "text/html", "Content-Disposition": 'attachment; filename="work.html"'}, f"{BASE_URL}/dateiverteilung.php", True),
    (LOGIN_HTML, {"Content-Type": "text/html", "Content-Disposition": 'attachment; filename="login-exercise.html"'}, f"{BASE_URL}/dateiverteilung.php", True),
    ("%PDF-1.7", {"Content-Type": "application/pdf"}, f"{BASE_URL}/dateiverteilung.php", True),
])
def test_submission_download_rejects_login_html_but_allows_real_attachments(
    body, headers, url, success
):
    from schulportal_hessen.applets.mein_unterricht.api import (
        meinunterricht_download_file,
    )

    response = _portal_response(body, url, headers)
    client = SimpleNamespace(
        logged_in=True, BASE_START_URL=BASE_URL,
        session=SimpleNamespace(get=lambda _url: response),
    )
    client.meinunterricht_download_file = lambda ref: meinunterricht_download_file(client, ref)
    result = meinunterricht_download_submission_file(client, _encode_ref(
        "dateiverteilung.php?a=download&f=123", BASE_URL,
    ))
    assert result["success"] is success
    if success:
        assert result["content"] == body.encode("utf-8")
    else:
        assert result["status_code"] == 401
        assert "content" not in result
