import base64
import datetime
import gzip
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_DSB_URL = "https://www.dsbmobile.de/"


def _build_getdata_payload(username: str, password: str) -> str:
    request_data = {
        "UserId": username,
        "UserPw": password,
        "Abos": [],
        "AppVersion": "2.3.1",
        "Language": "de",
        "OsVersion": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "AppId": "",
        "Device": "WebApp",
        "PushId": "",
        "BundleId": "de.heinekingmedia.inhouse.dsbmobile.web",
        "Date": datetime.datetime.utcnow().isoformat() + "Z",
        "LastUpdate": datetime.datetime.utcnow().isoformat() + "Z",
    }
    json_bytes = json.dumps(request_data, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(json_bytes)
    return base64.b64encode(compressed).decode("utf-8")


DSB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _make_dsb_session(
    dsbmobile_cookie: Optional[str] = None,
    asp_session_id: Optional[str] = None,
) -> requests.Session:
    session = requests.Session()
    session.headers.update(DSB_HEADERS)
    if dsbmobile_cookie:
        session.cookies.set("DSBmobile", dsbmobile_cookie, domain=".dsbmobile.de")
    if asp_session_id:
        session.cookies.set("ASP.NET_SessionId", asp_session_id, domain=".dsbmobile.de")
    return session


def _get_dsb_session(self) -> requests.Session:
    dsb_cookie = getattr(self, "_dsb_cookie", None)
    dsb_sid = getattr(self, "_dsb_session_id", None)
    return _make_dsb_session(dsbmobile_cookie=dsb_cookie, asp_session_id=dsb_sid)


DSB_GETDATA_FIXED_B64 = "H4sIAAAAAAAAA4WP20rEMBCGXyXkSsFNk7ZZNHuliCJ42Is9gOJFshnbsN2kNNvtovjuTiOLl8Iw/PP9wxy+6DJC92CpovQi6fnwq69NiFS9vaNq2xV00QWPTs4KJtB+1L7qdQWILGD9Ev96nsKnaxqdScbJ2dp5G4ZInhdEcMZnBMG0nJHjmLqDEhLpObmHzTZkORccQ5A718FHOGbJpemG05G3cHCbce8aDGIk8z7WJ/em97aBVFlgNTgPW+erHVinmfN16CMwG80uGNcAG8CMI/V+HJjzfDrh5aTgC1EoeaWKS1ZK+Zrejftla//p+/4B9MquQU8BAAA="


def _extract_login_payload(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        for parser in ("lxml", "html5lib"):
            try:
                soup = BeautifulSoup(html, parser)
                form = soup.find("form")
                if form:
                    break
            except Exception:
                continue
    if not form:
        return {}

    payload: Dict[str, str] = {}
    for input_tag in form.find_all("input"):
        name = input_tag.get("name", "")
        if not name:
            continue
        input_type = (input_tag.get("type") or "").lower()
        value = input_tag.get("value", "") or ""
        if input_type in {"checkbox", "radio"}:
            if "checked" in (input_tag.attrs or {}):
                payload[name] = value
        else:
            payload[name] = value

    if "txtUser" not in payload or "txtPass" not in payload:
        for inp in soup.find_all("input"):
            name = (inp.get("name") or "").lower()
            if name in ("txtuser", "txtpass"):
                payload[name] = inp.get("value") or ""

    if "txtUser" not in payload:
        for inp in soup.find_all("input"):
            itype = (inp.get("type") or "").lower()
            placeholder = (inp.get("placeholder") or "").lower()
            if itype == "text" and ("benutzer" in placeholder or "user" in placeholder):
                payload["txtUser"] = inp.get("value") or ""
            elif itype == "password":
                payload["txtPass"] = inp.get("value") or ""

    return payload


def _parse_table(table: Any) -> Dict[str, Any]:
    header_cells = table.find_all("th")
    headers = (
        [cell.get_text(" ", strip=True) for cell in header_cells]
        if header_cells
        else []
    )

    rows = []
    body_rows = table.find_all("tr")
    start_index = 1 if headers and body_rows else 0

    for row in body_rows[start_index:]:
        cells = row.find_all(["td", "th"])
        values = [cell.get_text(" ", strip=True) for cell in cells]
        if not any(values):
            continue
        if headers and len(headers) == len(values):
            rows.append(dict(zip(headers, values)))
        else:
            rows.append(values)

    caption_tag = table.find("caption")
    caption = caption_tag.get_text(" ", strip=True) if caption_tag else ""

    return {"caption": caption, "headers": headers, "rows": rows}


def _parse_plan_tables(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find(["h1", "h2", "h3"])
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    tables = []
    for table in soup.find_all("table"):
        tables.append(_parse_table(table))

    return {"title": title, "tables": tables}


def _find_getdata_endpoint(session: requests.Session) -> Optional[str]:
    try:
        config_resp = session.get(
            urljoin(BASE_DSB_URL, "scripts/configuration.js"), timeout=15
        )
        config_text = config_resp.text

        match = re.search(
            r'METHOD\s*:\s*[\'"]([^\'"]+\.ashx/GetData)[\'"]', config_text
        )
        if match:
            return urljoin(BASE_DSB_URL, match.group(1))

        return None
    except Exception:
        return None


def _extract_plan_urls_from_menu(items: List[Dict], urls: List[str]) -> None:
    for item in items:
        childs = item.get("Childs", [])
        if childs:
            _extract_plan_urls_from_menu(childs, urls)
        detail = item.get("Detail", "")
        if detail and detail.startswith("http"):
            urls.append(detail)

        root = item.get("Root", {})
        if root:
            root_childs = root.get("Childs", [])
            for rc in root_childs:
                rc_detail = rc.get("Detail", "")
                if rc_detail and rc_detail.startswith("http"):
                    urls.append(rc_detail)

                rc_childs = rc.get("Childs", [])
                for rcc in rc_childs:
                    rcc_detail = rcc.get("Detail", "")
                    if rcc_detail and rcc_detail.startswith("http"):
                        urls.append(rcc_detail)


def dsb_login(self, username: str, password: str) -> Dict[str, Any]:
    """Login to DSBmobile to access substitution plans.

    DSBmobile (https://www.dsbmobile.de) is a separate platform
    from Schulportal Hessen that provides substitution/replacement
    plan information for schools.

    This method establishes a separate session for DSBmobile
    and stores the authentication cookies for subsequent calls.

    Parameters
    ----------
    username : str
        DSBmobile username, typically in format "{school_id}{username}"
        or just the school identifier depending on school configuration.
    password : str
        DSBmobile password.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing:
        - success (bool): Whether login succeeded
        - session_cookie (str): The DSBMobile session cookie value
        - session_id (str): ASP.NET session ID
        - response_url (str): Final redirect URL

    Raises
    ------
    RequestsException
        If the HTTP request fails.

    Notes
    -----
    DSBmobile uses different credentials than SPH. The username
    is typically provided by the school administration.
    This is a completely separate system from Schulportal Hessen.

    Example
    -----
    >>> api.dsb_login("F1234", "mypassword")
    {'success': True, 'session_cookie': 'abc123...', 'session_id': 'def456...'}
    """
    session = _make_dsb_session()
    self.dsb_username = username
    self.dsb_password = password

    try:
        landing = session.get(BASE_DSB_URL, timeout=15)
        landing.raise_for_status()

        login_page = session.get(urljoin(BASE_DSB_URL, "Login.aspx"), timeout=15)
        login_page.raise_for_status()

        if not login_page.text or len(login_page.text.strip()) < 100:
            return {
                "success": False,
                "error": "Login page returned empty or truncated content.",
                "content_length": len(login_page.text),
                "status_code": login_page.status_code,
                "url": login_page.url,
            }

        payload = _extract_login_payload(login_page.text)
        if not payload:
            snippet = login_page.text[:800] if login_page.text else "(empty)"
            return {
                "success": False,
                "error": "Failed to parse login form. No form or input fields found in the HTML.",
                "html_snippet": snippet,
                "url": login_page.url,
                "status_code": login_page.status_code,
            }

        if "txtUser" not in payload or "txtPass" not in payload:
            snippet = login_page.text[:800] if login_page.text else "(empty)"
            return {
                "success": False,
                "error": "Login form found but missing txtUser/txtPass input fields.",
                "extracted_fields": list(payload.keys()),
                "html_snippet": snippet,
                "url": login_page.url,
            }

        payload["txtUser"] = username
        payload["txtPass"] = password
        if "ctl03" not in payload:
            payload["ctl03"] = "Anmelden"

        response = session.post(
            urljoin(BASE_DSB_URL, "Login.aspx"),
            data=payload,
            timeout=15,
            allow_redirects=True,
        )
        response.raise_for_status()

        has_cookie = any(
            cookie.name.lower() == "dsbmobile" for cookie in session.cookies
        )
        is_default = "default.aspx" in response.url.lower()

        if not is_default and not has_cookie:
            default_resp = session.get(
                urljoin(BASE_DSB_URL, "default.aspx"), timeout=15
            )
            default_resp.raise_for_status()
            is_default = "default.aspx" in default_resp.url.lower()
            has_cookie = any(
                cookie.name.lower() == "dsbmobile" for cookie in session.cookies
            )

        if has_cookie:
            self._dsb_cookie = session.cookies.get("DSBmobile")
            self._dsb_session_id = session.cookies.get("ASP.NET_SessionId")
            self.dsb_logged_in = True

        if not has_cookie:
            snippet = response.text[:800] if response.text else "(empty)"
            return {
                "success": False,
                "error": "Login failed. No DSBmobile cookie set after form submission. Check credentials.",
                "response_url": response.url,
                "has_default_redirect": is_default,
                "cookies_received": [c.name for c in session.cookies],
                "html_snippet": snippet,
            }

        return {
            "success": True,
            "session_cookie": self._dsb_cookie,
            "session_id": self._dsb_session_id,
            "response_url": response.url,
        }
    except requests.RequestException as e:
        return {"success": False, "error": f"Failed to login: {str(e)}"}


def dsb_get_plan_urls(
    self, username: Optional[str] = None, password: Optional[str] = None
) -> Dict[str, Any]:
    """
    Fetch plan URLs from GetData XHR endpoint after login.

    Args:
        username: DSBmobile username or school identifier (e.g. {username}).
        password: DSBmobile password (e.g. {password}).

    Returns:
        Dict with a list of plan URLs.

    Example:
        >>> api.dsb_get_plan_urls("{username}", "{password}")
        {"success": True, "plan_urls": ["{plan_url}", ...]}
    """
    if not getattr(self, "dsb_logged_in", False):
        if not username or not password:
            return {
                "success": False,
                "error": "Not logged in. Provide username and password.",
            }
        login = self.dsb_login(username, password)
        if not login.get("success"):
            return login

    session = _get_dsb_session(self)
    getdata_url = _find_getdata_endpoint(session)

    if not getdata_url:
        return {"success": False, "error": "Could not find GetData endpoint URL"}

    payload = {
        "req": {
            "Data": _build_getdata_payload(
                getattr(self, "dsb_username", username or ""),
                getattr(self, "dsb_password", password or ""),
            ),
            "DataType": 1,
        }
    }

    try:
        response = session.post(
            getdata_url,
            json=payload,
            timeout=15,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://www.dsbmobile.de",
                "Referer": "https://www.dsbmobile.de/default.aspx",
            },
        )
        response.raise_for_status()

        data = response.json()
        if "d" not in data:
            return {"success": False, "error": "Invalid GetData response", "raw": data}

        b64_data = data["d"]
        if not b64_data:
            return {"success": False, "error": "Empty response data"}

        decompressed = gzip.decompress(base64.b64decode(b64_data))
        menu_data = json.loads(decompressed.decode("utf-8"))

        plan_urls: List[str] = []
        html_plan_url: Optional[str] = None
        _extract_plan_urls_from_menu(menu_data.get("ResultMenuItems", []), plan_urls)

        for item in menu_data.get("ResultMenuItems", []):
            for child in item.get("Childs", []):
                if "Pl" in str(child.get("Title", "")):
                    root = child.get("Root", {})
                    for rc in root.get("Childs", []):
                        for rcc in rc.get("Childs", []):
                            detail = rcc.get("Detail", "")
                            if detail and detail.endswith(".htm"):
                                html_plan_url = detail

        self.dsb_plan_urls = plan_urls
        self.dsb_menu_data = menu_data
        return {
            "success": True,
            "plan_urls": plan_urls,
            "count": len(plan_urls),
            "html_plan_url": html_plan_url,
            "menu_items": [
                i.get("Title") for i in menu_data.get("ResultMenuItems", [])
            ],
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch GetData: {str(e)}"}


def dsb_get_substitution_plan(
    self,
    username: Optional[str] = None,
    password: Optional[str] = None,
    plan_index: int = 0,
    plan_url: Optional[str] = None,
    include_raw: bool = False,
    klasse: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch and parse the substitution plan table from DSBmobile.

    Args:
        username: DSBmobile username or school identifier.
        password: DSBmobile password.
        plan_index: Which plan URL to parse (default: 0).
        plan_url: Explicit plan URL to fetch (overrides plan_index).
        include_raw: Include raw HTML in response.
        klasse: Filter results by class name (e.g. "05A", "10C").

    Returns:
        Dict with the plan URL, title, parsed tables, and optional raw HTML.

    Example:
        >>> api.dsb_get_substitution_plan("{username}", "{password}")
        {"success": True, "plan_url": "{plan_url}", "tables": [...]}
    """
    if plan_url is None:
        plan_urls_result = self.dsb_get_plan_urls(username=username, password=password)
        if not plan_urls_result.get("success"):
            return plan_urls_result

        if plan_url is None:
            plan_url = plan_urls_result.get("html_plan_url")
        if not plan_url:
            plan_urls = plan_urls_result.get("plan_urls", [])
            if plan_index < 0 or plan_index >= len(plan_urls):
                return {
                    "success": False,
                    "error": f"plan_index out of range. Found {len(plan_urls)} plan URLs.",
                }
            plan_url = plan_urls[plan_index]

    session = _get_dsb_session(self)
    try:
        response = session.get(plan_url, timeout=15)
        response.raise_for_status()

        raw_html = response.text
        parsed = _parse_plan_tables(raw_html)

        tables = parsed.get("tables", [])
        if klasse:
            tables = _filter_tables_by_klasse(tables, klasse)

        return {
            "success": True,
            "plan_url": plan_url,
            "raw_html": raw_html if include_raw else None,
            "title": parsed.get("title", ""),
            "tables": tables,
        }
    except requests.RequestException as e:
        return {
            "success": False,
            "error": f"Failed to fetch substitution plan: {str(e)}",
        }


def _filter_tables_by_klasse(tables: List[Dict], klasse: str) -> List[Dict]:
    """Filter tables to only include rows matching the given class name."""
    filtered = []
    klasse_input = klasse.strip().upper()
    if len(klasse_input) == 2 and klasse_input[0].isdigit():
        klasse_input = klasse_input[0] + "0" + klasse_input[1]

    klasse_lower = klasse_input.lower()

    for table in tables:
        rows = table.get("rows", [])
        if not rows:
            continue
        filtered_rows = []
        for row in rows:
            if isinstance(row, dict):
                klasse_value = row.get("Klasse(n)", row.get("Klasse", ""))
            else:
                klasse_value = ""

            kv_upper = klasse_value.upper()
            kv_parts = [
                p.strip().upper() for p in klasse_value.replace(",", " ").split()
            ]
            if klasse_lower in klasse_value.lower() or klasse_input in kv_parts:
                filtered_rows.append(row)
        if filtered_rows:
            filtered.append({**table, "rows": filtered_rows})
    return filtered
