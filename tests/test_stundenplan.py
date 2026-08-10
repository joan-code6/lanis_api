from bs4 import BeautifulSoup

from schulportal_hessen.applets.stundenplan.api import (
    _extract_room_text,
    _parse_time_range,
    parse_timetable_html,
)


TIMETABLE_HTML = """
<span id="aktuelleWoche">Woche A</span>
<div id="all"><table><tbody>
  <tr><th>Stunde</th><th>Montag</th><th>Dienstag</th></tr>
  <tr>
    <td><span class="print-show"><b>1.</b></span><span class="VonBis">08:00 - 08:45</span></td>
    <td rowspan="2"><div class="stunde" data-mix="mathe"><b>Mathematik</b><small>AB</small><span class="badge">Q1</span> B 101</div></td>
    <td></td>
  </tr>
  <tr>
    <td><span class="print-show"><b>2.</b></span><span class="VonBis">08:50 – 09:35</span></td>
    <td><div class="stunde"><b>Biologie</b><small>CD</small> Bio-Labor</div></td>
  </tr>
</tbody></table></div>
<div id="own"><table><tbody>
  <tr><th>Stunde</th><th>Montag</th><th>Dienstag</th></tr>
  <tr><td><span class="VonBis">08:00 - 08:45</span></td><td></td><td></td></tr>
</tbody></table></div>
"""


def test_parse_timetable_with_rowspans_and_personal_plan() -> None:
    result = parse_timetable_html(TIMETABLE_HTML)

    assert result["success"] is True
    assert result["days"] == ["Montag", "Dienstag"]
    assert result["week_badge"] == "Woche A"
    assert result["plan_for_own"] == [[], []]
    assert len(result["hours"]) == 2

    monday = result["plan_for_all"][0][0]
    assert monday["name"] == "Mathematik"
    assert monday["teacher"] == "AB"
    assert monday["room"] == "B 101"
    assert monday["duration"] == 2
    assert monday["start_time"] == {"hour": 8, "minute": 0}
    assert monday["end_time"] == {"hour": 9, "minute": 35}

    tuesday = result["plan_for_all"][1][0]
    assert tuesday["name"] == "Biologie"
    assert tuesday["room"] == "Bio-Labor"
    assert tuesday["stunde"] == 2


def test_parse_time_range_rejects_invalid_values() -> None:
    assert _parse_time_range("25:00 - 26:00") is None
    assert _parse_time_range("not a time") is None


def test_parse_timetable_reports_missing_table() -> None:
    assert parse_timetable_html("<html></html>") == {
        "success": False,
        "error": "Timetable table not found",
    }


def test_malformed_time_does_not_shift_later_lessons() -> None:
    html = """
    <div id="all"><table><tbody>
      <tr><th>Stunde</th><th>Montag</th></tr>
      <tr><td><span class="VonBis">08:00 - 08:45</span></td><td></td></tr>
      <tr><td><span class="VonBis">unknown</span></td><td></td></tr>
      <tr><td><span class="VonBis">10:00 - 10:45</span></td>
          <td><div class="stunde"><b>Deutsch</b> A 1</div></td></tr>
    </tbody></table></div>
    """

    result = parse_timetable_html(html)

    lesson = result["plan_for_all"][0][0]
    assert lesson["start_time"] == {"hour": 10, "minute": 0}
    assert lesson["end_time"] == {"hour": 10, "minute": 45}


def test_room_extraction_handles_labels_nested_in_badge() -> None:
    soup = BeautifulSoup(
        '<div class="stunde"><b>Sport</b><span class="badge"><small>EF</small></span> Halle 1</div>',
        "html.parser",
    )

    assert _extract_room_text(soup.div) == "Halle 1"


def test_live_portal_thead_does_not_shift_lesson_times() -> None:
    html = """
    <div id="all"><table>
      <thead><tr><th>Stunde</th><th>Montag</th></tr></thead>
      <tbody>
        <tr><td><span class="print-show"><b>0. Stunde</b></span><span class="VonBis">07:00 - 07:45</span></td><td></td></tr>
        <tr><td><span class="print-show"><b>1. Stunde</b></span><span class="VonBis">07:50 - 08:35</span></td>
            <td><div class="stunde"><b>Mathematik</b><small>AB</small> A208</div></td></tr>
      </tbody>
    </table></div>
    """

    result = parse_timetable_html(html)

    assert result["days"] == ["Montag"]
    lesson = result["plan_for_all"][0][0]
    assert lesson["stunde"] == 1
    assert lesson["start_time"] == {"hour": 7, "minute": 50}
    assert lesson["end_time"] == {"hour": 8, "minute": 35}
