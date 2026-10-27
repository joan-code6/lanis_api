"""Explicit metadata results for the MCP connection and school app inventory."""

from typing import Any

from typing_extensions import Required, TypedDict


class ConnectionStatus(TypedDict):
    success: bool
    connection_name: str
    scopes: list[str]
    current_time: str
    timezone: str
    settings_url: str


class SchoolApp(TypedDict):
    name: str
    integrated: bool


class SchoolAppInventory(TypedDict, total=False):
    success: Required[bool]
    modules: list[SchoolApp]
    coverage: str
    error: str
    error_kind: str | None
    next_step: str
    details: Any
