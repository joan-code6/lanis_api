import io

from schulportal_hessen.applets.mein_unterricht.submissions import (
    _encode_ref,
    meinunterricht_get_submission,
    meinunterricht_get_submissions,
    meinunterricht_upload_files,
    parse_submission_detail,
    parse_submission_summaries,
    parse_upload_statuses,
)

BASE_URL = "https://example.invalid"


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
