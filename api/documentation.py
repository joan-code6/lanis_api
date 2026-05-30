"""
Auto-generated API documentation pages.

Reads the live OpenAPI schema from the FastAPI app and renders
browsable HTML at /documentation and /documentation/{path}.
"""

import json
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory="api/templates")

EXCLUDED_PATHS = {
    "/documentation",
    "/documentation/{path}",
    "/openapi.json",
}

_URL_TAG_MAP = [
    ("/login", "Authentication"),
    ("/logout", "Authentication"),
    ("/dsb/", "DSBmobile"),
    ("/kalender", "Calendar"),
    ("/meinunterricht", "Courses"),
    ("/nachrichten", "Messages"),
    ("/vertretungsplan", "Plans"),
    ("/stundenplan", "Plans"),
    ("/dateispeicher", "File Storage"),
    ("/lerngruppen", "Study Groups"),
    ("/school-list", "School List"),
    ("/benutzer", "User Info"),
    ("/modules", "Modules & Apps"),
    ("/apps", "Modules & Apps"),
    ("/health", "System"),
    ("/metrics", "System"),
]


def _infer_tag(path: str, details: dict) -> str:
    """Infer a tag from the URL path, falling back to explicit tags."""
    explicit = details.get("tags")
    if explicit:
        return explicit[0]
    for prefix, tag in _URL_TAG_MAP:
        if path.startswith(prefix):
            return tag
    return "Other"


def _resolve_ref(schema: dict, ref: str) -> dict:
    """Resolve a $ref pointer within the OpenAPI schema."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    current = schema
    for part in parts:
        current = current.get(part, {})
    return current


def _resolve_schema(schema: dict, full_schema: dict) -> dict:
    """Resolve a schema that may be a $ref or inline."""
    if "$ref" in schema:
        return _resolve_ref(full_schema, schema["$ref"])
    return schema


def _is_auth_required(method_item: dict, full_schema: dict) -> bool:
    """Check if an endpoint requires the X-Session-Token header."""
    parameters = method_item.get("parameters", [])
    for p in parameters:
        resolved = _resolve_schema(p, full_schema)
        if resolved.get("name") == "X-Session-Token":
            return True
    return False


def _build_endpoint_list(schema: dict) -> list:
    """Extract all endpoints from the OpenAPI schema as flat list entries."""
    endpoints = []
    paths = schema.get("paths", {})

    for path, methods in paths.items():
        if path in EXCLUDED_PATHS or path.startswith("/documentation"):
            continue

        for method, details in methods.items():
            method_upper = method.upper()
            if method_upper not in ("GET", "POST"):
                continue

            encoded_path = quote(path.lstrip("/"), safe="/")
            tag = _infer_tag(path, details)
            summary = details.get("summary", "")
            auth_required = _is_auth_required(details, schema)

            endpoints.append(
                {
                    "path": path,
                    "display_path": path,
                    "encoded_path": encoded_path,
                    "method": method_upper,
                    "tag": tag,
                    "summary": summary,
                    "auth_required": auth_required,
                }
            )

    endpoints.sort(key=lambda e: (e["tag"], e["path"], e["method"]))
    return endpoints


def _extract_params(details: dict, full_schema: dict):
    """Extract path, query, and header parameters from an operation."""
    path_params = []
    query_params = []
    header_params = []

    for p in details.get("parameters", []):
        resolved = _resolve_schema(p, full_schema)
        p_in = resolved.get("in", "")
        param_schema = resolved.get("schema", {})
        param_schema = _resolve_schema(param_schema, full_schema)

        entry = {
            "name": resolved.get("name", ""),
            "type": _schema_type(param_schema),
            "required": resolved.get("required", False),
            "description": resolved.get("description", ""),
        }
        if p_in == "path":
            path_params.append(entry)
        elif p_in == "query":
            entry["default"] = _format_default(param_schema)
            query_params.append(entry)
        elif p_in == "header":
            header_params.append(entry)

    return path_params, query_params, header_params


def _build_endpoint_detail(schema: dict, target_path: str) -> dict | None:
    """Build detailed info for a single endpoint path."""
    paths = schema.get("paths", {})

    if target_path not in paths:
        return None

    methods = paths[target_path]
    method_names = [m for m in methods if m.upper() in ("GET", "POST")]
    if not method_names:
        return None

    primary = method_names[0]
    details = methods[primary]
    method_upper = primary.upper()
    auth_required = _is_auth_required(details, schema)

    path_params, query_params, header_params = _extract_params(details, schema)

    request_body = _build_request_body(details.get("requestBody"), schema)
    responses = _build_responses(details.get("responses", {}), schema)

    return {
        "path": target_path,
        "method": method_upper,
        "description": details.get("description", ""),
        "summary": details.get("summary", ""),
        "auth_required": auth_required,
        "path_params": path_params,
        "query_params": query_params,
        "header_params": header_params,
        "request_body": request_body,
        "responses": responses,
    }


def _build_request_body(request_body: dict | None, full_schema: dict) -> dict | None:
    if not request_body:
        return None
    content = request_body.get("content", {})
    ct = list(content.keys())[0] if content else "application/json"
    schema_info = content.get(ct, {}).get("schema", {})
    schema_info = _resolve_schema(schema_info, full_schema)

    properties = []
    if "properties" in schema_info:
        required_fields = schema_info.get("required", [])
        for name, prop in schema_info["properties"].items():
            prop = _resolve_schema(prop, full_schema)
            properties.append(
                {
                    "name": name,
                    "type": _schema_type(prop),
                    "required": name in required_fields,
                    "description": prop.get("description", prop.get("title", "")),
                }
            )

    example = _generate_example(schema_info, full_schema)

    return {
        "content_type": ct,
        "format": _schema_type(schema_info) if not properties else "object",
        "properties": properties,
        "example": example,
    }


def _build_responses(responses: dict, full_schema: dict) -> list:
    result = []
    for status_code, resp in responses.items():
        description = resp.get("description", "")
        example = None
        content = resp.get("content", {})
        if content:
            ct = list(content.keys())[0]
            schema = content[ct].get("schema", {})
            schema = _resolve_schema(schema, full_schema)
            example = _generate_example(schema, full_schema)
        result.append(
            {
                "status": status_code,
                "description": description,
                "example": example,
            }
        )
    return result


def _schema_type(schema: dict) -> str:
    """Extract a human-readable type string from a JSON schema dict."""
    if not schema:
        return "any"
    type_ = schema.get("type", "any")
    if type_ == "array":
        items = schema.get("items", {})
        return f"array[{_schema_type(items)}]"
    if type_ == "integer":
        return "integer"
    if type_ == "boolean":
        return "boolean"
    if type_ == "number":
        return "number"
    return type_


def _format_default(schema: dict) -> str:
    """Format the default value from a schema dict."""
    if "default" in schema:
        return str(schema["default"])
    return ""


def _generate_example(schema: dict, full_schema: dict) -> str | None:
    """Generate a JSON example from a schema definition."""
    if not schema:
        return None

    if "properties" in schema:
        obj = {}
        for name, prop in schema["properties"].items():
            prop = _resolve_schema(prop, full_schema)
            prop_type = prop.get("type", "string")
            desc = prop.get("description", "")
            if prop_type == "string":
                obj[name] = f"<{desc}>" if desc else f"<{name}>"
            elif prop_type in ("integer", "number"):
                obj[name] = 0
            elif prop_type == "boolean":
                obj[name] = True
            elif prop_type == "array":
                obj[name] = []
            else:
                obj[name] = f"<{name}>"
        return json.dumps(obj, indent=2)

    type_ = schema.get("type", "any")
    if type_ == "string":
        return '"<value>"'
    if type_ in ("integer", "number"):
        return "0"
    if type_ == "boolean":
        return "true"
    return None


@router.get("/documentation", response_class=HTMLResponse)
async def documentation_index(request: Request):
    schema = request.app.openapi()
    endpoints = _build_endpoint_list(schema)

    grouped: dict[str, list] = {}
    for ep in endpoints:
        grouped.setdefault(ep["tag"], []).append(ep)

    return templates.TemplateResponse(
        request,
        "documentation_index.html",
        {"grouped": grouped},
    )


@router.get("/documentation/{path:path}", response_class=HTMLResponse)
async def documentation_endpoint(request: Request, path: str):
    target = "/" + unquote(path.rstrip("/"))
    schema = request.app.openapi()
    ep = _build_endpoint_detail(schema, target)

    if not ep:
        raise HTTPException(status_code=404, detail=f"Endpoint '{target}' not found")

    return templates.TemplateResponse(
        request,
        "documentation_endpoint.html",
        {"ep": ep},
    )
