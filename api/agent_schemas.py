"""Explicit metadata results for the MCP connection and school app inventory."""

from typing import Any

from typing_extensions import Required, TypedDict


class ConnectionStatus(TypedDict):
    success: bool
    demo: bool
    writes_simulated: bool
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
    modules: list[SchoolApp] | None
    coverage: str | None
    error: str | None
    error_kind: str | None
    next_step: str | None
    demo: bool | None
    writes_simulated: bool | None
    details: Any
