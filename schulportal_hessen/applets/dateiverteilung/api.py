from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

_DOWNLOAD_ACTIONS = {"download"}


def _clean_text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _absolute_portal_url(base_url: str, value: str) -> str:
    """Return a safe URL to this applet, or an empty string.

    File links are supplied by upstream HTML and later accepted through a public
    API query parameter. Treat both sources as untrusted and pin them to the
    configured Schulportal origin and the Dateiverteilung path.
    """
    value = (value or "").strip()
    if not value or value.startswith(("//", "\\")):
        return ""
    candidate = urljoin(f"{base_url.rstrip('/')}/", value)
    expected = urlparse(base_url)
    parsed = urlparse(candidate)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path.rstrip("/") != "/dateiverteilung.php"
    ):
        return ""
    query = parse_qs(parsed.query, keep_blank_values=True)
    action = str((query.get("a") or query.get("action") or [""])[0]).lower()
    if action not in _DOWNLOAD_ACTIONS:
        return ""
    return candidate


def _file_id(url: str, fallback: str) -> str:
    query = parse_qs(urlparse(url).query)
    for key in ("f", "file", "id", "d"):
        value = str((query.get(key) or [""])[0]).strip()
        if value:
            return value
    return fallback


def _parse_file(link: Tag, base_url: str, index: int) -> dict[str, Any] | None:
    url = _absolute_portal_url(base_url, str(link.get("href") or ""))
    if not url:
        return None
    name = str(link.get("download") or "").strip() or _clean_text(link)
    if not name:
        name = f"Datei {index + 1}"
    parent_text = _clean_text(link.parent)
    size_match = re.search(
        r"(?:\(|\b)(\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB))(?:\)|\b)",
        parent_text,
        re.IGNORECASE,
    )
    return {
        "id": _file_id(url, str(index + 1)),
        "name": name,
        "size": size_match.group(1) if size_match else "",
        "download_url": url,
    }


def _container_for(link: Tag) -> Tag:
    return (
        link.find_parent(["article", "section"])
        or link.find_parent(
            class_=re.compile(r"(?:panel|card|distribution|verteilung)", re.IGNORECASE)
        )
        or link.find_parent("tr")
        or link.parent
    )


def _parse_distribution(
    container: Tag, files: list[dict[str, Any]], index: int, base_url: str
) -> dict[str, Any]:
    heading = container.select_one(
        "h1, h2, h3, h4, h5, h6, .panel-title, .card-title, .title, strong"
    )
    title = _clean_text(heading) or f"Verteilung {index + 1}"

    description_node = container.select_one(
        ".description, .beschreibung, .hinweis, .markup, .card-text, .panel-body p"
    )
    description = _clean_text(description_node)
    if description == title:
        description = ""

    source_node = container.select_one(
        ".course, .kurs, .sender, .source, .herkunft, [data-course]"
    )
    source = _clean_text(source_node) or str(container.get("data-course") or "").strip()

    text = _clean_text(container)
    date_match = re.search(
        r"\b(\d{1,2}[.]\d{1,2}[.]\d{2,4}(?:\s+(?:um\s+)?\d{1,2}:\d{2})?)\b", text
    )
    distribution_id = str(container.get("data-id") or container.get("id") or "").strip()
    if not distribution_id:
        for file in files:
            query = parse_qs(urlparse(file["download_url"]).query)
            distribution_id = str(
                (
                    query.get("v")
                    or query.get("distribution")
                    or query.get("id")
                    or [""]
                )[0]
            ).strip()
            if distribution_id:
                break

    external_links: list[dict[str, str]] = []
    for link in container.select("a[href]"):
        href = str(link.get("href") or "").strip()
        absolute = urljoin(f"{base_url.rstrip('/')}/", href)
        if _absolute_portal_url(base_url, href) or not absolute.startswith(
            ("http://", "https://")
        ):
            continue
        label = _clean_text(link) or absolute
        if not any(item["url"] == absolute for item in external_links):
            external_links.append({"label": label, "url": absolute})

    classes = " ".join(container.get("class") or [])
    unread = bool(
        container.select_one(".badge, .label-new, .neu, [data-new='1']")
        or re.search(r"(?:^|\s)(?:new|unread|neu)(?:\s|$)", classes, re.IGNORECASE)
    )
    return {
        "id": distribution_id or f"distribution-{index + 1}",
        "title": title,
        "description": description,
        "source": source or "Schulportal",
        "created_at": date_match.group(1) if date_match else "",
        "unread": unread,
        "files": files,
        "links": external_links,
    }


def parse_dateiverteilung_html(html: str, base_url: str) -> list[dict[str, Any]]:
    """Parse recipient distributions across current and legacy portal markup."""
    soup = BeautifulSoup(html, "html.parser")
    grouped: dict[int, tuple[Tag, list[dict[str, Any]]]] = {}
    seen_urls: set[str] = set()

    for link in soup.select("a[href]"):
        parsed = _parse_file(link, base_url, len(seen_urls))
        if not parsed or parsed["download_url"] in seen_urls:
            continue
        seen_urls.add(parsed["download_url"])
        container = _container_for(link)
        key = id(container)
        if key not in grouped:
            grouped[key] = (container, [])
        grouped[key][1].append(parsed)

    # Text/link-only distributions have no download anchors. Prefer explicit
    # semantic containers so unrelated portal chrome is never returned.
    explicit = soup.select(
        "article, section[data-id], .distribution, .dateiverteilung, [data-distribution]"
    )
    for container in explicit:
        if id(container) not in grouped and _clean_text(container):
            grouped[id(container)] = (container, [])

    return [
        _parse_distribution(container, files, index, base_url)
        for index, (container, files) in enumerate(grouped.values())
    ]


def _looks_like_login(response: Any) -> bool:
    content_type = str(
        (getattr(response, "headers", {}) or {}).get("Content-Type") or ""
    ).lower()
    prefix = str(getattr(response, "text", "") or "")[:8192].lower()
    response_host = (
        urlparse(str(getattr(response, "url", "") or "")).hostname or ""
    ).lower()
    looks_html = "text/html" in content_type or bool(
        re.match(r"\s*<(?:!doctype\s+html|html|head|body|form)\b", prefix)
    )
    return "login.schulportal" in response_host or (
        looks_html
        and (
            "login.schulportal" in prefix
            or (
                "<form" in prefix
                and re.search(r"(?:anmelden|login|kennwort|passwort)", prefix)
            )
        )
    )


def dateiverteilung_get_overview(self) -> dict[str, Any]:
    if not self.logged_in:
        return {
            "success": False,
            "error": "Not logged in",
            "error_kind": "authentication",
        }
    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/dateiverteilung.php", timeout=(10, 30)
        )
        response.raise_for_status()
        if _looks_like_login(response):
            return {
                "success": False,
                "error": "Dateiverteilung session expired or portal returned a login page",
                "error_kind": "authentication",
            }
        distributions = parse_dateiverteilung_html(response.text, self.BASE_START_URL)
        return {
            "success": True,
            "distributions": distributions,
            "distribution_count": len(distributions),
            "file_count": sum(len(item["files"]) for item in distributions),
            "unread_count": sum(1 for item in distributions if item["unread"]),
        }
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return {
            "success": False,
            "error": f"Failed to fetch Dateiverteilung: {exc}",
            "error_kind": "authentication" if status_code in {401, 403} else "upstream",
            **(
                {"upstream_status": status_code} if isinstance(status_code, int) else {}
            ),
        }
    except requests.RequestException as exc:
        return {
            "success": False,
            "error": f"Failed to fetch Dateiverteilung: {exc}",
            "error_kind": "upstream",
        }
    except Exception as exc:  # noqa: BLE001 - normalize parser failures for API clients
        return {
            "success": False,
            "error": f"Failed to parse Dateiverteilung: {exc}",
            "error_kind": "upstream",
        }


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _stream(response: Any, first: bytes, iterator: Iterator[bytes]) -> Iterator[bytes]:
    try:
        if first:
            yield first
        for chunk in iterator:
            if chunk:
                yield chunk
    finally:
        _close_response(response)


def _filename(disposition: str, download_url: str) -> str:
    encoded = re.search(
        r"filename\*\s*=\s*(?:UTF-8)?''([^;]+)", disposition, re.IGNORECASE
    )
    if encoded:
        return unquote(encoded.group(1)).strip()
    plain = re.search(r'filename\s*=\s*"?([^";]+)', disposition, re.IGNORECASE)
    if plain:
        return plain.group(1).strip()
    return str(
        (parse_qs(urlparse(download_url).query).get("f") or ["dateiverteilung-datei"])[
            0
        ]
    )


def dateiverteilung_download_file(self, url: str) -> dict[str, Any]:
    if not self.logged_in:
        return {
            "success": False,
            "error": "Not logged in",
            "error_kind": "authentication",
        }
    download_url = _absolute_portal_url(self.BASE_START_URL, url)
    if not download_url:
        return {
            "success": False,
            "error": "Invalid Dateiverteilung download URL",
            "error_kind": "validation",
        }

    response = None
    try:
        response = self.session.get(
            download_url, stream=True, timeout=(10, 60), allow_redirects=False
        )
        if 300 <= response.status_code < 400:
            _close_response(response)
            return {
                "success": False,
                "error": "Unexpected redirect while downloading Dateiverteilung file",
                "error_kind": "authentication",
            }
        response.raise_for_status()
        headers = getattr(response, "headers", {}) or {}
        disposition = str(headers.get("Content-Disposition") or "")
        content_type = str(headers.get("Content-Type") or "application/octet-stream")
        iterator = response.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not isinstance(first, bytes):
            first = bytes(first or b"")
        prefix = first[:8192].decode("utf-8", errors="ignore").lstrip().lower()
        html = "text/html" in content_type.lower() or bool(
            re.match(r"<(?:!doctype\s+html|html|head|body)\b", prefix)
        )
        if html and "attachment" not in disposition.lower():
            _close_response(response)
            return {
                "success": False,
                "error": "Dateiverteilung session expired or portal returned a login page",
                "error_kind": "authentication",
            }
        return {
            "success": True,
            "filename": _filename(disposition, download_url),
            "content_type": content_type,
            "stream": _stream(response, first, iterator),
        }
    except requests.HTTPError as exc:
        _close_response(response)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return {
            "success": False,
            "error": f"Failed to download Dateiverteilung file: {exc}",
            "error_kind": "authentication" if status_code in {401, 403} else "upstream",
            **(
                {"upstream_status": status_code} if isinstance(status_code, int) else {}
            ),
        }
    except requests.RequestException as exc:
        _close_response(response)
        return {
            "success": False,
            "error": f"Failed to download Dateiverteilung file: {exc}",
            "error_kind": "upstream",
        }
    except Exception as exc:  # noqa: BLE001 - always close and normalize stream failures
        _close_response(response)
        return {
            "success": False,
            "error": f"Failed to download Dateiverteilung file: {exc}",
            "error_kind": "upstream",
        }
