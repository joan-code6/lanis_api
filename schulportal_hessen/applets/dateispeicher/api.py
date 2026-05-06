from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


def _normalize_header(value: str) -> str:
    value = value.strip().lower()
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(value.split())


def _header_index(headers: List[str], *candidates: str) -> Optional[int]:
    for candidate in candidates:
        if candidate in headers:
            return headers.index(candidate)
    return None


def _parse_files_table(table: Any) -> List[Dict[str, Any]]:
    headers = [_normalize_header(th.get_text(" ", strip=True)) for th in table.select("thead th")]

    idx_name = _header_index(headers, "name")
    idx_changed = _header_index(headers, "anderung", "aenderung", "anderungen")
    idx_size = _header_index(headers, "grosse", "groesse", "grosze")

    files: List[Dict[str, Any]] = []
    for row in table.select("tbody tr"):
        fields = row.find_all("td")
        if idx_name is None or idx_name >= len(fields):
            continue
        file_id = row.get("data-id")
        if not file_id:
            continue
        name_cell = fields[idx_name]
        hint = None
        hint_node = name_cell.find("small")
        if hint_node:
            hint = hint_node.get_text(" ", strip=True)
            hint_node.extract()
        name = name_cell.get_text(" ", strip=True)
        changed = fields[idx_changed].get_text(" ", strip=True) if idx_changed is not None and idx_changed < len(fields) else ""
        size = fields[idx_size].get_text(" ", strip=True) if idx_size is not None and idx_size < len(fields) else ""

        files.append(
            {
                "id": int(file_id),
                "name": name,
                "download_url": f"https://start.schulportal.hessen.de/dateispeicher.php?a=download&f={file_id}",
                "changed": changed,
                "size": size,
                "note": hint,
            }
        )

    return files


def _parse_folders(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    folders: List[Dict[str, Any]] = []
    for folder in soup.select(".folder"):
        name_node = folder.select_one(".caption")
        desc_node = folder.select_one(".desc")
        name = name_node.get_text(" ", strip=True) if name_node else ""
        desc = desc_node.get_text(" ", strip=True) if desc_node else ""

        count_text = ""
        for node in folder.select("[title]"):
            title = node.get("title", "")
            if "Ordner" in title:
                count_text = node.get_text(" ", strip=True)
                break
        match = re.search(r"\d+", count_text)
        subfolders = int(match.group(0)) if match else 0
        folder_id = folder.get("data-id")
        if not folder_id:
            continue

        folders.append(
            {
                "id": int(folder_id),
                "name": name,
                "subfolders": subfolders,
                "description": desc,
            }
        )

    return folders


def dateispeicher_get_node(self, folder_id: int = 0) -> Dict[str, Any]:
    """Fetch files and subfolders for a dateispeicher folder.

    Args:
        folder_id: Folder id to open. Use 0 for the root folder.

    Returns:
        Dict with files and folders.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        response = self.session.get(
            f"{self.BASE_START_URL}/dateispeicher.php",
            params={"a": "view", "folder": str(folder_id)},
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        files_table = soup.select_one("table#files")
        files = _parse_files_table(files_table) if files_table else []
        folders = _parse_folders(soup)

        return {
            "success": True,
            "folder_id": folder_id,
            "files": files,
            "folders": folders,
            "file_count": len(files),
            "folder_count": len(folders),
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to fetch dateispeicher: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Failed to parse dateispeicher: {exc}"}


def dateispeicher_get_root(self) -> Dict[str, Any]:
    """Fetch files and subfolders for the root dateispeicher folder."""
    return dateispeicher_get_node(self, folder_id=0)


def dateispeicher_search_files(self, query: str) -> Dict[str, Any]:
    """Search files by name in the dateispeicher.

    Args:
        query: Search string.

    Returns:
        Dict with search results.
    """
    if not self.logged_in:
        return {"success": False, "error": "Not logged in"}

    try:
        def _extract_json_payload(text: str) -> Optional[Any]:
            stripped = text.strip()
            if not stripped:
                return None
            for prefix in (")]}',", ")]}',\n"):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):].lstrip()
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass

            if "<" in stripped:
                soup = BeautifulSoup(stripped, "html.parser")
                pre = soup.find("pre")
                if pre:
                    pre_text = pre.get_text(" ", strip=True)
                    try:
                        return json.loads(pre_text)
                    except json.JSONDecodeError:
                        pass

            for start_char in ("[", "{"):
                idx = stripped.find(start_char)
                if idx == -1:
                    continue
                candidate = stripped[idx:]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    end_char = "]" if start_char == "[" else "}"
                    end_idx = candidate.rfind(end_char)
                    if end_idx != -1:
                        try:
                            return json.loads(candidate[: end_idx + 1])
                        except json.JSONDecodeError:
                            pass
            return None

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.BASE_START_URL}/dateispeicher.php",
        }
        # Preflight to establish dateispeicher context and cookies
        self.session.get(f"{self.BASE_START_URL}/dateispeicher.php")

        response = self.session.post(
            f"{self.BASE_START_URL}/dateispeicher.php",
            data={"q": query, "a": "searchFiles"},
            headers=headers,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        text = response.text.strip()
        if not text:
            return {
                "success": False,
                "error": "Dateispeicher search returned an empty response",
            }

        payload = _extract_json_payload(text)
        if payload is None and "application/json" not in content_type:
            response = self.session.get(
                f"{self.BASE_START_URL}/dateispeicher.php",
                params={"q": query, "a": "searchFiles"},
                headers=headers,
            )
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            text = response.text.strip()
            payload = _extract_json_payload(text)

        if payload is None:
            text_lower = text.lower()
            if "login" in text_lower or "anmelden" in text_lower:
                return {
                    "success": False,
                    "error": "Dateispeicher search returned HTML (session may have expired)",
                    "content_type": content_type,
                    "response_snippet": text[:500],
                }
            return {
                "success": False,
                "error": "Dateispeicher search returned a non-JSON response",
                "content_type": content_type,
                "response_snippet": text[:500],
            }

        results = payload[0] if isinstance(payload, list) and payload else payload
        return {"success": True, "query": query, "results": results}
    except requests.RequestException as exc:
        return {"success": False, "error": f"Failed to search dateispeicher: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Failed to parse dateispeicher search: {exc}"}
