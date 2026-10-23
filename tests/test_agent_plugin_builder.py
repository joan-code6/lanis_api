"""Validate portable source output without calling or publishing to a service."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest


@pytest.mark.parametrize(
    "registration",
    [
        "valid",
        "missing",
        "foreign",
        "string-scopes",
        "missing-scopes",
        "null-scopes",
        "empty-scope",
        "number-scope",
        "resource-list",
        "metadata-list",
        "null-servers",
        "null-pkce",
    ],
)
def test_source_archive_has_rooted_brand_assets_and_explains_installation(
    monkeypatch, tmp_path, capsys, registration
):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "builder", root / "scripts/build_agent_plugin.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    issuer = "https://school-api.example"
    resource = {"resource": issuer + "/mcp", "authorization_servers": [issuer]}
    metadata = {
        "issuer": issuer,
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["schoolwork:read"],
    }
    if registration != "missing":
        metadata["registration_endpoint"] = (
            issuer + "/oauth/register"
            if registration != "foreign"
            else "https://other.example/oauth/register"
        )
    if registration == "string-scopes":
        metadata["scopes_supported"] = "schoolwork:read"
    elif registration == "missing-scopes":
        del metadata["scopes_supported"]
    elif registration == "null-scopes":
        metadata["scopes_supported"] = None
    elif registration == "empty-scope":
        metadata["scopes_supported"] = [" "]
    elif registration == "number-scope":
        metadata["scopes_supported"] = [123]
    elif registration == "resource-list":
        resource = []
    elif registration == "metadata-list":
        metadata = []
    elif registration == "null-servers":
        resource["authorization_servers"] = None
    elif registration == "null-pkce":
        metadata["code_challenge_methods_supported"] = None
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["allow_redirects"] is False
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: resource if "protected-resource" in url else metadata,
        )

    monkeypatch.setattr(builder.requests, "Session", lambda: SimpleNamespace(get=get))
    output = tmp_path / "source.zip"
    monkeypatch.setattr(
        "sys.argv", ["builder", "--issuer", issuer, "--output", str(output)]
    )
    if registration != "valid":
        with pytest.raises(SystemExit):
            builder.main()
        assert not output.exists()
        return
    builder.main()
    assert len(calls) == 2
    with ZipFile(output) as archive:
        manifest = json.loads(archive.read("lanis/plugin.json"))
        branding = manifest["extensions"]["com.openai"]["interface"]
        for key in ["logo", "composerIcon"]:
            assert branding[key].startswith("./")
            assert archive.read("lanis/" + branding[key][2:])
        config = json.loads(archive.read("lanis/mcp.json"))
        assert config["mcpServers"]["lanis"]["url"] == issuer + "/mcp"
    message = capsys.readouterr().out
    assert "not installable via ChatGPT ZIP upload" in message
    assert "Add custom MCP server" in message
    assert "With MCP" in message
