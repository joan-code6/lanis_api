from schulportal_hessen.applets.oberstufenwahl.api import (
    parse_oberstufenwahl_form,
    parse_oberstufenwahl_overview,
    serialize_oberstufenwahl_submission,
    validate_oberstufenwahl_submission,
    wahlen_submit,
)


FORM_HTML = """
<html><body>
  <h1>AG Einwahlen 26/27</h1>
  <form method="post">
    <input type="hidden" name="a" value="abgabe">
    <input type="hidden" name="w" value="18">
    <input type="hidden" name="kurse">
    <div class="row block"><div class="panel"><div class="panel-body">
      <h2 data-name="Bemerkungen" data-min="-1" data-max="-1"></h2>
      <div class="col-md-12"><label>Name<input id="bemerkung0" type="text"></label></div>
      <div class="col-md-12"><label>Klasse<select id="bemerkung1"><option>10a</option><option>10b</option></select></label></div>
    </div></div></div>
    <div class="row block"><div class="panel"><div class="panel-body">
      <h2 data-name="Erstwunsch" data-min="0" data-max="1000" data-firstonly="1"></h2>
      <select id="sel1" data-type="must" data-nr="1" data-not='["Zweitwunsch"]'>
        <option value=""></option>
        <option value="A" data-fach="A" data-hidetoo="[]">A</option>
        <option value="B" data-fach="B" data-hidetoo="[]">B</option>
      </select>
    </div></div></div>
    <div class="row block"><div class="panel"><div class="panel-body">
      <h2 data-name="Zweitwunsch" data-min="0" data-max="1"></h2>
      <div class="subblock"><h3 data-name="AG-Auswahl" data-min="0" data-max="1"></h3>
        <div class="checkbox"><label><input type="checkbox" data-name="A" data-min="0" data-max="1">A</label></div>
        <div class="checkbox"><label><input type="checkbox" data-name="B" data-min="0" data-max="1">B</label></div>
      </div>
    </div></div></div>
  </form>
</body></html>
"""


def test_parse_overview_extracts_election_metadata() -> None:
    result = parse_oberstufenwahl_overview(
        """
        <a href="oberstufenwahl.php?a=abgabe&amp;w=18">AG Einwahlen 26/27</a>
        <span>Teilnahme vom 03.09.2026 um 00:00 Uhr bis 20.09.2026 um 23:00 Uhr</span>
        <a href="https://info.schulportal.hessen.de/das-sph/sph-paedorg/oberstufenkurswahl/">Weitere Informationen</a>
        """
    )

    assert result["success"] is True
    assert result["elections"][0]["id"] == "18"
    assert result["elections"][0]["starts_on"] == "2026-09-03"
    assert result["elections"][0]["ends_on"] == "2026-09-20"


def test_form_and_serializer_match_portal_kurse_shape() -> None:
    form = parse_oberstufenwahl_form(FORM_HTML, "18")
    assert form["success"] is True
    assert [field["id"] for field in form["personal_fields"]] == [
        "bemerkung0",
        "bemerkung1",
    ]

    kurse = serialize_oberstufenwahl_submission(
        form,
        {
            "fields": {"bemerkung0": "Test Person", "bemerkung1": "10b"},
            "selections": {"sel1": "A", "checkbox:Zweitwunsch:B": True},
        },
    )

    assert kurse == [
        {
            "name": "Bemerkungen",
            "anmerk": ["Name # Test Person", "Klasse # 10b"],
        },
        {"name": "Erstwunsch", "values": ["A"]},
        "B",
    ]

    assert validate_oberstufenwahl_submission(
        form,
        {
            "fields": {"bemerkung0": "Test Person", "bemerkung1": "10b"},
            "selections": {"sel1": "A", "checkbox:Zweitwunsch:A": True},
        },
    ) == ["A kann nicht gleichzeitig gewählt werden."]


def test_submit_requires_confirmation_before_upstream_post() -> None:
    class Response:
        status_code = 200
        url = "https://example.invalid/oberstufenwahl.php?a=abgabe&w=18"

        def raise_for_status(self):
            return None

    class Session:
        post_calls = 0

        def get(self, *_args, **_kwargs):
            return type(
                "GetResponse",
                (),
                {
                    "text": FORM_HTML,
                    "raise_for_status": lambda self: None,
                    "url": Response.url,
                },
            )()

        def post(self, *_args, **_kwargs):
            self.post_calls += 1
            return Response()

    class Client:
        logged_in = True
        BASE_START_URL = "https://example.invalid"
        session = Session()

    submission = {
        "fields": {"bemerkung0": "Test Person", "bemerkung1": "10b"},
        "selections": {"sel1": "A", "checkbox:Zweitwunsch:B": True},
    }
    not_confirmed = wahlen_submit(Client(), "18", submission)
    assert not_confirmed["success"] is False
    assert Client.session.post_calls == 0

    confirmed = wahlen_submit(Client(), "18", submission, confirmed=True)
    assert confirmed["success"] is True
    assert Client.session.post_calls == 1
