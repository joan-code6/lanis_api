from schulportal_hessen.applets.lerngruppen.api import parse_lerngruppen_html

LIVE_STYLE_HTML = """
<section id="LGs">
  <table>
    <thead><tr><td>Lerngruppe</td><td>Lehrkräfte</td><td>Schulhalbjahr</td></tr></thead>
    <tbody>
      <tr data-id="2914">
        <td>Biologie 05f1 <small>(051BIO01-F)</small></td>
        <td>
          <div class="btn-group">
            <button class="btn btn-primary dropdown-toggle">MST</button>
            <ul class="dropdown-menu">
              <li><a href="mailto:max.mustermann@example.org"><i class="fa"></i>Mustermann, Max</a></li>
              <li><a href="nachrichten.php?to[]=bC0xMjg2NTY=}">Nachricht schreiben</a></li>
            </ul>
          </div>
        </td>
        <td>1. Halbjahr 2026/27</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="klausuren">
  <table>
    <thead><tr><td>Termin</td><td>Lerngruppe</td><td>Typ</td><td>Unterrichtsstunden</td><td>Bearbeitungszeit</td></tr></thead>
    <tbody>
      <tr data-type="klausur" data-id="892" data-lerngruppe="2914">
        <td>Do, 25.11.2026</td>
        <td>Biologie 05f1 <small>(051BIO01-F)</small></td>
        <td>Lernkontrolle</td><td>1., 2.</td><td>45 Min.</td>
      </tr>
    </tbody>
  </table>
</section>
"""


HEADERLESS_HTML = """
<div id="LGs"><table><tbody>
  <tr data-id="42">
    <td>Mathematik GK <small>(M-GK-1)</small></td>
    <td><div class="btn-group"><button class="dropdown-toggle">WEB</button><div class="dropdown-menu"><a>Weber, Erika</a></div></div></td>
    <td>2. Halbjahr</td>
  </tr>
</tbody></table></div>
"""


def test_parse_live_table_labels_and_td_headers() -> None:
    result = parse_lerngruppen_html(LIVE_STYLE_HTML)

    assert result["group_count"] == 1
    assert result["exam_count"] == 1
    assert result["groups"][0] == {
        "id": "2914",
        "semester": "1. Halbjahr 2026/27",
        "course_name": "Biologie 05f1",
        "course_sys_id": "051BIO01-F",
        "teachers": [
            {
                "krz": "MST",
                "first_name": "Max",
                "last_name": "Mustermann",
                "email": "max.mustermann@example.org",
                "recipient_id": "l-128656",
            }
        ],
        "exams": [result["exams"][0]],
    }
    assert result["exams"][0] == {
        "id": "892",
        "course_id": "2914",
        "course_name": "Biologie 05f1",
        "course_sys_id": "051BIO01-F",
        "date": "2026-11-25",
        "type": "Lernkontrolle",
        "duration_label": "45 Min.",
        "hours": "1., 2.",
    }


def test_parse_headerless_table_from_semantic_cell_markup() -> None:
    result = parse_lerngruppen_html(HEADERLESS_HTML)

    group = result["groups"][0]
    assert group["course_name"] == "Mathematik GK"
    assert group["course_sys_id"] == "M-GK-1"
    assert group["semester"] == "2. Halbjahr"
    assert group["teachers"][0]["krz"] == "WEB"
    assert group["teachers"][0]["last_name"] == "Weber"
