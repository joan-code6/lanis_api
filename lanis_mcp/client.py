"""Small asynchronous client shared by the LANIS MCP tools."""

from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = "https://lanis-backend.joancode.dev"


class LanisAPIError(RuntimeError):
    """A response from the LANIS API was not successful."""

    def __init__(self, status_code: int, method: str, path: str, detail: str) -> None:
        super().__init__(f"LANIS API {method} {path} failed ({status_code}): {detail}")
        self.status_code = status_code
        self.method = method
        self.path = path
        self.detail = detail


class LanisClient:
    """Call one configured LANIS API deployment."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        access_token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("LANIS_API_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.access_token = access_token or os.getenv("LANIS_ACCESS_TOKEN")
        self.timeout = timeout
        self.transport = transport

    def resolve_token(self, access_token: str | None) -> str:
        token = access_token or self.access_token
        if not token:
            raise ValueError(
                "An access token is required. Pass access_token or set LANIS_ACCESS_TOKEN. "
                "Use lanis_login first if necessary."
            )
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        authenticated: bool = True,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        headers: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            headers["X-Session-Token"] = self.resolve_token(access_token)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = await client.request(
                method,
                path,
                headers=headers,
                params=params,
                json=json,
                data=data,
            )

        if response.is_error:
            try:
                payload = response.json()
                detail = (
                    payload.get("detail", payload)
                    if isinstance(payload, dict)
                    else payload
                )
            except ValueError:
                detail = response.text[:1000]
            raise LanisAPIError(response.status_code, method.upper(), path, str(detail))

        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return response.json()
        return response.text

    async def download_file(
        self,
        file_hash: str,
        *,
        access_token: str | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        token = access_token or self.access_token
        if token:
            headers["X-Session-Token"] = token

        path = f"/meinunterricht/file/{quote(file_hash, safe='')}"
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = await client.get(path, headers=headers)

        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text[:1000]
            raise LanisAPIError(response.status_code, "GET", path, str(detail))
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive or null")
        if max_bytes is not None and len(response.content) > max_bytes:
            raise ValueError(
                f"File is {len(response.content)} bytes, exceeding max_bytes={max_bytes}."
            )

        disposition = response.headers.get("content-disposition", "")
        filename = "download"
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        return {
            "filename": filename,
            "content_type": response.headers.get(
                "content-type", "application/octet-stream"
            ),
            "size": len(response.content),
            "content_base64": base64.b64encode(response.content).decode("ascii"),
        }

    def app_launch_url(self, app_name: str, access_token: str | None = None) -> str:
        token = self.resolve_token(access_token)
        return f"{self.base_url}/app/{quote(app_name, safe='')}?token={quote(token, safe='')}"
