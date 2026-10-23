"""Validate portable source output without calling or publishing to a service."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile


def test_source_archive_has_rooted_brand_assets_and_explains_installation(
    monkeypatch, tmp_path, capsys
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
