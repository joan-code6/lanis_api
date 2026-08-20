from schulportal_hessen.applets.mein_unterricht.api import meinunterricht_get_course


class FakeResponse:
    text = """
    <html><body>
      <h1 data-book="42">
        Mathematik 10a
        <small><span class="label-info">2. Halbjahr</span></small>
      </h1>
    </body></html>
    """

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse()


class FakeCryptor:
    authenticated = True


class FakeClient:
    logged_in = True
    cryptor = FakeCryptor()
    session = FakeSession()
    BASE_START_URL = "https://example.invalid"


def test_course_heading_uses_first_visible_text() -> None:
    result = meinunterricht_get_course(FakeClient(), "42")

    assert result["success"] is True
    assert result["course_name"] == "Mathematik 10a"
    assert result["semester"] == "2. Halbjahr"
