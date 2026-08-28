from __future__ import annotations

from schulportal_hessen.applets.dateispeicher.api import dateispeicher_download_file


class FakeResponse:
    content = b"file contents"
    headers = {
        "Content-Disposition": "attachment; filename*=UTF-8''Arbeitsblatt%20%282026%29.pdf",
        "Content-Type": "application/pdf",
    }

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


class FakeClient:
    BASE_START_URL = "https://portal.example"
    logged_in = True

    def __init__(self):
        self.session = FakeSession()


def test_dateispeicher_download_uses_authenticated_portal_url():
    client = FakeClient()

    result = dateispeicher_download_file(client, 42)

    assert result["success"] is True
    assert result["file_id"] == 42
    assert result["filename"] == "Arbeitsblatt (2026).pdf"
    assert result["content_type"] == "application/pdf"
    assert result["content"] == b"file contents"
    assert client.session.calls == [
        (
            ("https://portal.example/dateispeicher.php",),
            {"params": {"a": "download", "f": "42"}},
        )
    ]


def test_dateispeicher_download_rejects_invalid_file_ids():
    result = dateispeicher_download_file(FakeClient(), 0)

    assert result == {"success": False, "error": "Invalid file id"}
