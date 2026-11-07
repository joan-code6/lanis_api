import pytest
import requests
from schulportal_hessen.tools.transport import ObservedSession, set_html_encoding


@pytest.mark.parametrize('encoding', ['utf-8', 'iso-8859-1', 'cp1252'])
@pytest.mark.parametrize('header', ['text/html', 'text/html; charset=UTF-8', 'text/html; charset=iso-8859-1'])
def test_portal_html_encoding(encoding, header):
    text = '<html><meta charset="utf-8"><p>Küchemann, Christel Swantje (KÜM) – Grüße</p></html>'
    if encoding == 'iso-8859-1':
        text = text.replace('–', '-')
    response = requests.Response()
    response._content = text.encode(encoding)
    response.headers['Content-Type'] = header
    response.encoding = 'utf-8'
    set_html_encoding(response)
    assert response.text == text


@pytest.mark.parametrize('stream,content_type,disposition', [(True, 'text/html', ''), (False, 'application/json', ''), (False, 'text/html', 'attachment; filename=test.html')])
def test_other_responses_untouched(stream, content_type, disposition):
    response = requests.Response()
    response.headers.update({'Content-Type': content_type, 'Content-Disposition': disposition})
    response.encoding = 'original'
    set_html_encoding(response, stream=stream)
    assert response.encoding == 'original'


def test_shared_session_decodes_before_return(monkeypatch):
    response = requests.Response()
    response.url = 'https://start.schulportal.hessen.de/lerngruppen.php'
    response.status_code = 200
    response.headers['Content-Type'] = 'text/html; charset=UTF-8'
    response.encoding = 'utf-8'
    response._content = b'<p>K\xfcchemann (K\xdcM)</p>'
    monkeypatch.setattr(requests.Session, 'request', lambda *args, **kwargs: response)
    assert ObservedSession().get(response.url).text == '<p>Küchemann (KÜM)</p>'
